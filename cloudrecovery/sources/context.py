"""Compose grounded context for an incident, in three tiers by trust level.

The tiers exist so the resolver — and the human reading its output — can tell
what a conclusion rests on:

1. **Primary — the diagnosis.** The evidence CloudRecovery collected itself
   (cycle, repeat count, tokens, pod reason). Highest trust: measured, not inferred.
2. **Secondary — correlated source.** Snippets from the linked repo, each labeled
   with source and file path, so an answer can cite `agents/intake_gate.py`
   instead of guessing.
3. **Tertiary — documentation.** Off by default, and only consulted when tiers 1–2
   found nothing. Never the sole basis for a proposed code change, and always
   labeled lower-confidence in the output.

Everything that goes into an LLM prompt passes through ``redact_source``,
which layers source-code-specific secret patterns on top of the same
``redact_text`` the terminal output already uses.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cloudrecovery.redact import redact_text

from .correlate import CorrelationResult
from .index import SourceIndex, language_for

MAX_SNIPPET_LINES = 120
SNIPPET_CONTEXT_LINES = 25
MAX_SNIPPETS = 4
DOCS_FETCH_TIMEOUT_S = 8.0
MAX_DOCS_CHARS = 4_000


# The shared redact_text() targets terminal output — `api_key=…`, `Bearer …`,
# dotenv lines. Source files hide secrets differently: a constant assignment with
# spaces around `=`, or a bare literal key with no telltale variable name at all.
# These patterns close that gap for anything lifted out of a repository, and run
# in addition to redact_text() rather than instead of it.
_SECRETISH_NAME = (
    r"[A-Za-z0-9_.\-]*"
    r"(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL"
    r"|CLIENT_?SECRET|AUTH)"
    r"[A-Za-z0-9_.\-]*"
)

_SOURCE_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    # NAME = "value" / NAME: 'value' / "NAME": "value"
    (
        re.compile(
            rf"(?i)([\"']?\b{_SECRETISH_NAME}\b[\"']?\s*[:=]\s*)"
            r"([\"'][^\"'\n]{4,}[\"']|[^\s,;)\]}}\n]{8,})"
        ),
        r"\1***REDACTED***",
    ),
    # Literal key shapes, which need no variable name to be dangerous.
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***REDACTED***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "***REDACTED***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "***REDACTED***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "***REDACTED***"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.S,
        ),
        "-----BEGIN PRIVATE KEY----- ***REDACTED*** -----END PRIVATE KEY-----",
    ),
]


def redact_source(text: str) -> str:
    """Redact secrets from repository content before it reaches an LLM or the UI."""
    out = redact_text(text)
    for pattern, replacement in _SOURCE_SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def docs_fetch_enabled() -> bool:
    """Tier-3 doc fetching is opt-in via env, and off unless explicitly set."""
    return os.getenv("CLOUDRECOVERY_DOCS_FETCH_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass
class Snippet:
    source_id: str
    path: str
    content: str
    start_line: int = 1
    end_line: int = 1
    language: str | None = None
    reason: str = ""
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "language": self.language,
            "reason": self.reason,
            "truncated": self.truncated,
            "content": self.content,
        }


@dataclass
class DocReference:
    url: str
    fetched: bool = False
    content: str = ""

    def to_dict(self) -> dict:
        return {"url": self.url, "fetched": self.fetched, "content": self.content}


@dataclass
class GroundedContext:
    incident: dict = field(default_factory=dict)
    snippets: list[Snippet] = field(default_factory=list)
    docs: list[DocReference] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_grounded(self) -> bool:
        """True when at least one real source file backs this context."""
        return bool(self.snippets)

    def to_dict(self) -> dict:
        return {
            "incident": self.incident,
            "snippets": [s.to_dict() for s in self.snippets],
            "docs": [d.to_dict() for d in self.docs],
            "notes": self.notes,
            "is_grounded": self.is_grounded,
        }

    def to_prompt(self) -> str:
        """Render the tiers as a labeled prompt block."""
        lines: list[str] = []

        lines.append("### TIER 1 — DIAGNOSIS (highest trust: measured by CloudRecovery)")
        for key, value in self.incident.items():
            lines.append(f"- {key}: {value}")
        lines.append("")

        lines.append("### TIER 2 — CORRELATED SOURCE (from the repository linked to this service)")
        if self.snippets:
            for snippet in self.snippets:
                header = f"{snippet.path}:{snippet.start_line}-{snippet.end_line}"
                lines.append(f"--- {header} (source: {snippet.source_id}) ---")
                if snippet.reason:
                    lines.append(f"# selected because: {snippet.reason}")
                lines.append(f"```{snippet.language or ''}")
                lines.append(snippet.content)
                lines.append("```")
                if snippet.truncated:
                    lines.append("# (truncated)")
                lines.append("")
        else:
            lines.append("(no source files correlated)")
            lines.append("")

        if self.docs:
            lines.append(
                "### TIER 3 — DOCUMENTATION (LOWEST TRUST — background only, "
                "never sufficient on its own to justify a code change)"
            )
            for doc in self.docs:
                lines.append(f"- {doc.url}{'' if doc.fetched else ' (not fetched; reference only)'}")
                if doc.content:
                    lines.append(doc.content)
            lines.append("")

        if self.notes:
            lines.append("### NOTES")
            lines.extend(f"- {n}" for n in self.notes)

        return "\n".join(lines)


def _incident_summary(evidence: Any) -> dict:
    if isinstance(evidence, dict):
        raw = evidence
    else:
        raw = {
            "source": getattr(evidence, "source", None),
            "kind": getattr(evidence, "kind", None),
            "severity": getattr(evidence, "severity", None),
            "message": getattr(evidence, "message", None),
            "payload": getattr(evidence, "payload", None),
            "incident_id": getattr(evidence, "incident_id", None),
        }

    summary = {}
    for key in ("source", "kind", "severity", "incident_id", "message"):
        value = raw.get(key)
        if value:
            summary[key] = redact_source(str(value))
    payload = raw.get("payload")
    if isinstance(payload, dict) and payload:
        summary["payload"] = redact_source(str(payload))
    return summary


def extract_snippet(
    index: SourceIndex,
    path: str,
    *,
    symbols: list[str] | None = None,
    reason: str = "",
    max_lines: int = MAX_SNIPPET_LINES,
) -> Snippet | None:
    """Read a window around the matched symbols, redacted.

    Whole files are avoided: a 2,000-line module would crowd out every other
    tier. When no symbol anchors the window, the head of the file is used, since
    imports and wiring at the top are usually the useful part.
    """
    try:
        text = index.read_file(path)
    except (OSError, PermissionError, FileNotFoundError):
        return None

    lines = text.splitlines()
    if not lines:
        return None

    anchor = None
    for symbol in symbols or []:
        for i, line in enumerate(lines):
            if symbol in line:
                anchor = i
                break
        if anchor is not None:
            break

    if anchor is None:
        start, end = 0, min(len(lines), max_lines)
    else:
        start = max(0, anchor - SNIPPET_CONTEXT_LINES)
        end = min(len(lines), anchor + max_lines - SNIPPET_CONTEXT_LINES)

    window = lines[start:end]
    return Snippet(
        source_id=index.source_id,
        path=path,
        content=redact_source("\n".join(window)),
        start_line=start + 1,
        end_line=end,
        language=language_for(Path(path)),
        reason=reason,
        truncated=(end - start) < len(lines),
    )


def fetch_docs(url: str) -> DocReference:
    """Fetch a single registered docs URL. No crawling, no link-following.

    Requires both the per-call opt-in (the caller passing a docs URL) and the
    ``CLOUDRECOVERY_DOCS_FETCH_ENABLED`` env flag. Without the flag the URL is
    returned as a bare reference so the operator still sees what *would* be read.
    """
    if not docs_fetch_enabled():
        return DocReference(url=url, fetched=False)

    try:
        import httpx

        response = httpx.get(url, timeout=DOCS_FETCH_TIMEOUT_S, follow_redirects=True)
        response.raise_for_status()
        text = response.text
    except Exception as e:
        return DocReference(url=url, fetched=False, content=f"(fetch failed: {e})")

    # Crude tag strip; this is background context, not a rendering pipeline.
    plain = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    return DocReference(url=url, fetched=True, content=redact_source(plain[:MAX_DOCS_CHARS]))


def compose_context(
    evidence: Any,
    index: SourceIndex | None = None,
    correlation: CorrelationResult | None = None,
    *,
    docs_url: str | None = None,
    max_snippets: int = MAX_SNIPPETS,
) -> GroundedContext:
    """Build the three-tier context for one incident."""
    context = GroundedContext(incident=_incident_summary(evidence))

    if index is not None and correlation is not None:
        for match in correlation.matches[:max_snippets]:
            snippet = extract_snippet(
                index, match.path, symbols=match.matched_symbols, reason=match.reason
            )
            if snippet:
                context.snippets.append(snippet)

        for match in correlation.adjacent:
            if len(context.snippets) >= max_snippets + 1:
                break
            snippet = extract_snippet(
                index, match.path, symbols=match.matched_terms, reason=match.reason
            )
            if snippet:
                context.snippets.append(snippet)

        if correlation.note:
            context.notes.append(correlation.note)
    else:
        context.notes.append(
            "No source is linked to this service, so this analysis is based on "
            "symptoms only. Attach a source in the Sources tab to ground it in code."
        )

    # Tier 3 only as a last resort, and only if a docs URL was registered.
    if docs_url and not context.is_grounded:
        context.docs.append(fetch_docs(docs_url))
        context.notes.append(
            "Tiers 1-2 produced no source match; documentation was consulted as "
            "lower-confidence background and must not be treated as evidence "
            "about your code."
        )

    return context
