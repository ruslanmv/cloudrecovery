"""Correlate an incident to the files that most likely produced it.

Input is Evidence that CloudRecovery already produces today — a Langfuse loop
carrying `cycle: [intake_gate, injection_check, ...]`, a CrashLoopBackOff
carrying a container and a message. Output is a ranked set of files from the
source linked to that service.

Two deliberate choices:

* **Terms come from the incident, not from the LLM.** Search terms are span
  names, error identifiers, and config keys lifted straight out of the evidence
  payload, so correlation is reproducible and auditable.
* **Callers are included, not just matches.** A resolver shown only
  `intake_gate.py` cannot see that the graph wiring has no exit condition. After
  ranking, we pull one structurally-adjacent file — a file that references the
  top match — so the resolver sees calling context rather than an isolated
  function.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .index import SourceIndex, language_for

# Words that would match half a repo and rank nothing usefully.
_STOPWORDS = {
    "error", "warning", "critical", "info", "true", "false", "none", "null",
    "trace", "span", "token", "tokens", "total", "count", "name", "type", "kind",
    "start", "end", "step", "steps", "run", "id", "unknown", "default", "value",
    "data", "http", "https", "get", "post", "the", "and", "for", "with", "from",
    "failed", "failure", "detected", "loop", "limit", "status",
}

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{2,}")

# Payload keys whose values are identifiers worth searching for.
_TERM_KEYS = (
    "cycle", "container", "deployment", "workload", "app", "service", "service.name",
    "service_name", "reason", "config_key", "node", "nodes", "tool", "tools",
    "observation_sequence", "argocd_application", "application",
)

MAX_TERMS = 24
MAX_CALLER_SCAN_FILES = 600
MAX_CALLER_SCAN_BYTES = 256 * 1024


@dataclass
class FileMatch:
    path: str
    score: float
    matched_terms: list[str] = field(default_factory=list)
    matched_symbols: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "score": round(self.score, 2),
            "matched_terms": self.matched_terms,
            "matched_symbols": self.matched_symbols,
            "reason": self.reason,
        }


@dataclass
class CorrelationResult:
    source_id: str
    terms: list[str] = field(default_factory=list)
    matches: list[FileMatch] = field(default_factory=list)
    adjacent: list[FileMatch] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "terms": self.terms,
            "matches": [m.to_dict() for m in self.matches],
            "adjacent": [m.to_dict() for m in self.adjacent],
            "note": self.note,
        }

    @property
    def all_paths(self) -> list[str]:
        return [m.path for m in self.matches] + [m.path for m in self.adjacent]


def _clean_term(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    term = raw.strip().strip("\"'`,.:;()[]{}")
    if len(term) < 3 or term.lower() in _STOPWORDS:
        return None
    if term.isdigit():
        return None
    return term


def extract_terms(evidence: Any) -> list[str]:
    """Lift search terms out of an Evidence object or dict.

    Ordered by trust: explicit payload identifiers (span names, container names)
    first, then identifiers scraped from the human-readable message.
    """
    if isinstance(evidence, dict):
        payload = evidence.get("payload") or {}
        message = evidence.get("message") or ""
    else:
        payload = getattr(evidence, "payload", None) or {}
        message = getattr(evidence, "message", "") or ""

    if not isinstance(payload, dict):
        payload = {}

    ordered: dict[str, None] = {}

    def add(value: Any) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        term = _clean_term(value)
        if term:
            ordered.setdefault(term, None)

    for key in _TERM_KEYS:
        if key in payload:
            add(payload[key])

    # Config-ish keys can themselves be the signal (e.g. "memory_limit").
    for key, value in payload.items():
        if isinstance(value, (str, int, float)) and key not in _TERM_KEYS:
            cleaned = _clean_term(key)
            if cleaned and "_" in cleaned:
                ordered.setdefault(cleaned, None)

    for match in _IDENTIFIER.finditer(str(message)):
        add(match.group(0))

    return list(ordered)[:MAX_TERMS]


def _score_file(entry, terms: list[str]) -> FileMatch | None:
    path_lower = entry.path.lower()
    stem = Path(entry.path).stem.lower()
    symbols_lower = {s.lower(): s for s in entry.symbols}

    score = 0.0
    matched_terms: dict[str, None] = {}
    matched_symbols: dict[str, None] = {}
    reasons: list[str] = []

    for term in terms:
        low = term.lower()
        hit = False

        if low in symbols_lower:
            score += 10
            matched_symbols.setdefault(symbols_lower[low], None)
            reasons.append(f"declares `{symbols_lower[low]}`")
            hit = True
        elif stem == low:
            score += 6
            reasons.append(f"filename matches `{term}`")
            hit = True
        elif low in path_lower:
            score += 3
            reasons.append(f"path contains `{term}`")
            hit = True
        else:
            for sym_low, sym in symbols_lower.items():
                if low in sym_low or sym_low in low:
                    score += 1
                    matched_symbols.setdefault(sym, None)
                    hit = True
                    break

        if hit:
            matched_terms.setdefault(term, None)

    if score <= 0:
        return None

    # A file hit by several distinct incident terms is far more likely to be the
    # real site than one hit repeatedly by a single common term.
    distinct = len(matched_terms)
    if distinct > 1:
        score += 2 * distinct

    return FileMatch(
        path=entry.path,
        score=score,
        matched_terms=list(matched_terms),
        matched_symbols=list(matched_symbols),
        reason="; ".join(dict.fromkeys(reasons))[:300],
    )


def rank_files(index: SourceIndex, terms: list[str], *, limit: int = 5) -> list[FileMatch]:
    if not terms:
        return []
    scored = [m for m in (_score_file(e, terms) for e in index.files) if m]
    # Stable tie-break on path so repeated runs give identical output.
    scored.sort(key=lambda m: (-m.score, m.path))
    return scored[:limit]


def find_callers(
    index: SourceIndex,
    target_path: str,
    symbols: list[str],
    *,
    limit: int = 1,
) -> list[FileMatch]:
    """Find files referencing the target's symbols or module name.

    Content is read on demand here rather than stored in the index, and the scan
    is bounded (`MAX_CALLER_SCAN_FILES`) so correlation stays cheap on a large
    repo. Files nearest the target are scanned first, since a caller usually
    lives next to what it calls.

    Candidates are ranked, not taken first-come: a file that *imports* the target
    module is far stronger evidence of a call relationship than one that merely
    mentions the name in a string. That ranking is what surfaces the graph-wiring
    file — where a missing exit condition actually lives — instead of a sibling
    node that happens to return the same name.
    """
    module = Path(target_path).stem
    needles = [s for s in dict.fromkeys([*symbols, module]) if s and len(s) > 2]
    if not needles:
        return []

    target_dir = str(Path(target_path).parent)
    candidates = [
        e for e in index.files
        if e.path != target_path and language_for(Path(e.path)) and e.size <= MAX_CALLER_SCAN_BYTES
    ]
    candidates.sort(key=lambda e: (not e.path.startswith(target_dir), e.path))
    candidates = candidates[:MAX_CALLER_SCAN_FILES]

    patterns = {n: re.compile(rf"\b{re.escape(n)}\b") for n in needles}
    # `import x`, `from a.b import x`, `require("…/x")`, `#include "x"`.
    import_pattern = re.compile(
        rf"^\s*(?:from\s+[\w.]*\b{re.escape(module)}\b|import\s+[\w.{{}}\s,*]*\b{re.escape(module)}\b"
        rf"|.*(?:require|import)\s*\(\s*['\"][^'\"]*{re.escape(module)})",
        re.M,
    )

    scored: list[FileMatch] = []
    for entry in candidates:
        try:
            text = index.read_file(entry.path)
        except (OSError, PermissionError, FileNotFoundError):
            continue

        hits = [n for n, pattern in patterns.items() if pattern.search(text)]
        if not hits:
            continue

        imports_target = bool(import_pattern.search(text))
        score = 2.0 * len(hits) + (3.0 if imports_target else 0.0)
        detail = "imports and references" if imports_target else "references"

        scored.append(
            FileMatch(
                path=entry.path,
                score=score,
                matched_terms=hits,
                reason=(
                    f"{detail} {', '.join(f'`{h}`' for h in hits[:3])} from {target_path}"
                ),
            )
        )

    scored.sort(key=lambda m: (-m.score, m.path))
    return scored[:limit]


def correlate(
    index: SourceIndex,
    evidence: Any,
    *,
    limit: int = 3,
    include_callers: bool = True,
) -> CorrelationResult:
    """Rank files for an incident, plus one structurally-adjacent file."""
    terms = extract_terms(evidence)
    result = CorrelationResult(source_id=index.source_id, terms=terms)

    if not terms:
        result.note = "No searchable identifiers in this evidence payload."
        return result

    result.matches = rank_files(index, terms, limit=limit)
    if not result.matches:
        result.note = (
            f"No file in this source matched any of {len(terms)} incident term(s). "
            "The source may be linked to the wrong service, or the code may live elsewhere."
        )
        return result

    if include_callers:
        top = result.matches[0]
        result.adjacent = find_callers(index, top.path, top.matched_symbols)

    return result
