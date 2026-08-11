"""Lightweight code index: file paths and symbol names only.

Deliberately *not* a content store. The index holds relative paths plus the
symbols declared in each file; file contents are re-read from the checkout on
demand when a snippet is actually needed. That keeps the persisted artifact
small and limits secret sprawl — a secret sitting in a config file never gets
copied into an index that outlives the checkout.

Symbol extraction is regex-based per language rather than AST-based on purpose:
it must never fail on a syntax error, a half-written file, or a language nobody
anticipated. A missed symbol degrades ranking slightly; a parser exception would
take out correlation entirely.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .models import IndexStats

MAX_FILES = 20_000
MAX_FILE_BYTES = 512 * 1024

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
    ".terraform", "vendor", ".idea", ".vscode", "target", ".tox", "site-packages",
    ".gradle", "coverage", ".cache", "eggs", ".eggs",
}

SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".pdf", ".zip",
    ".gz", ".tgz", ".bz2", ".xz", ".7z", ".jar", ".war", ".class", ".so", ".dylib",
    ".dll", ".exe", ".bin", ".o", ".a", ".pyc", ".pyo", ".woff", ".woff2", ".ttf",
    ".eot", ".mp4", ".mp3", ".wav", ".mov", ".lock", ".pack", ".idx",
}

_SYMBOL_PATTERNS: dict[str, list[re.Pattern]] = {
    "python": [
        re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", re.M),
        re.compile(r"^\s*class\s+([A-Za-z_]\w*)", re.M),
    ],
    "js": [
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M),
        re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", re.M),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", re.M),
    ],
    "go": [
        re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", re.M),
        re.compile(r"^\s*type\s+([A-Za-z_]\w*)", re.M),
    ],
    "java": [
        re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?class\s+([A-Za-z_]\w*)", re.M),
        re.compile(r"\b(?:public|private|protected)\s+[\w<>\[\]]+\s+([A-Za-z_]\w*)\s*\(", re.M),
    ],
    "yaml": [
        re.compile(r"^([A-Za-z_][\w.-]*):", re.M),
        re.compile(r"^\s*name:\s*[\"']?([A-Za-z_][\w.-]*)", re.M),
        re.compile(r"^\s*(?:image|kind):\s*[\"']?([A-Za-z_][\w./:-]*)", re.M),
    ],
    "terraform": [
        re.compile(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"', re.M),
        re.compile(r'^\s*module\s+"([^"]+)"', re.M),
    ],
    "shell": [re.compile(r"^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\)\s*\{", re.M)],
}

_LANG_BY_SUFFIX = {
    ".py": "python", ".pyi": "python",
    ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js", ".cjs": "js",
    ".go": "go",
    ".java": "java", ".kt": "java", ".scala": "java",
    ".yaml": "yaml", ".yml": "yaml",
    ".tf": "terraform", ".hcl": "terraform",
    ".sh": "shell", ".bash": "shell",
}


def language_for(path: Path) -> str | None:
    return _LANG_BY_SUFFIX.get(path.suffix.lower())


def extract_symbols(text: str, language: str | None) -> list[str]:
    """Return declared symbol names, de-duplicated, order preserved."""
    if not language:
        return []
    seen: dict[str, None] = {}
    for pattern in _SYMBOL_PATTERNS.get(language, []):
        for match in pattern.finditer(text):
            for group in match.groups():
                if group and len(group) > 1:
                    seen.setdefault(group, None)
    return list(seen)


@dataclass
class FileEntry:
    path: str
    """Path relative to the index root, POSIX separators."""
    symbols: list[str] = field(default_factory=list)
    size: int = 0

    def to_dict(self) -> dict:
        return {"path": self.path, "symbols": self.symbols, "size": self.size}

    @classmethod
    def from_dict(cls, row: dict) -> FileEntry:
        return cls(
            path=str(row.get("path", "")),
            symbols=[str(s) for s in row.get("symbols", []) or []],
            size=int(row.get("size", 0) or 0),
        )


@dataclass
class SourceIndex:
    source_id: str
    root: str
    files: list[FileEntry] = field(default_factory=list)
    stats: IndexStats = field(default_factory=IndexStats)

    def symbol_map(self) -> dict[str, list[str]]:
        """lowercased symbol -> file paths declaring it."""
        out: dict[str, list[str]] = {}
        for entry in self.files:
            for symbol in entry.symbols:
                out.setdefault(symbol.lower(), []).append(entry.path)
        return out

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "root": self.root,
            "files": [f.to_dict() for f in self.files],
            "stats": self.stats.model_dump(mode="json"),
        }

    @classmethod
    def from_dict(cls, data: dict) -> SourceIndex:
        return cls(
            source_id=str(data.get("source_id", "")),
            root=str(data.get("root", "")),
            files=[FileEntry.from_dict(r) for r in data.get("files", []) or []],
            stats=IndexStats.model_validate(data.get("stats") or {}),
        )

    def read_file(self, relative_path: str, *, max_bytes: int = MAX_FILE_BYTES) -> str:
        """Read a file's current content from the checkout, refusing to escape root."""
        root = Path(self.root).resolve()
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as e:
            raise PermissionError(f"path '{relative_path}' escapes the index root") from e
        if not candidate.is_file():
            raise FileNotFoundError(relative_path)
        return candidate.read_text("utf-8", errors="replace")[:max_bytes]


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            yield path


def build_index(source_id: str, root: Path, *, max_files: int = MAX_FILES) -> SourceIndex:
    """Walk `root` and index paths + symbols. Content is never retained."""
    root = Path(root).resolve()
    index = SourceIndex(source_id=source_id, root=str(root))
    symbol_count = 0
    skipped = 0
    truncated = False

    for path in _iter_files(root):
        if len(index.files) >= max_files:
            truncated = True
            break

        if path.suffix.lower() in SKIP_SUFFIXES:
            skipped += 1
            continue

        try:
            size = path.stat().st_size
        except OSError:
            skipped += 1
            continue

        relative = path.relative_to(root).as_posix()

        if size > MAX_FILE_BYTES:
            # Still worth knowing the file exists — path matching can hit it.
            index.files.append(FileEntry(path=relative, symbols=[], size=size))
            continue

        language = language_for(path)
        symbols: list[str] = []
        if language:
            try:
                text = path.read_text("utf-8", errors="ignore")
            except OSError:
                skipped += 1
                continue
            symbols = extract_symbols(text, language)
            symbol_count += len(symbols)

        index.files.append(FileEntry(path=relative, symbols=symbols, size=size))

    index.stats = IndexStats(
        files_indexed=len(index.files),
        symbols_indexed=symbol_count,
        skipped_files=skipped,
        built_at=datetime.now(UTC),
        truncated=truncated,
    )
    return index


# ---------------------------------------------------------------------------
# Persistence + in-process cache
# ---------------------------------------------------------------------------

# Keyed by (workspace, source_id), not source_id alone: two engines pointed at
# different workspaces must not serve each other's indexes.
_CACHE: dict[tuple[str, str], SourceIndex] = {}


def _cache_key(source_id: str, workspace: Path) -> tuple[str, str]:
    return (str(Path(workspace).resolve()), source_id)


def index_path(source_id: str, workspace: Path) -> Path:
    return Path(workspace) / f"{source_id}.index.json"


def save_index(index: SourceIndex, workspace: Path) -> Path:
    path = index_path(index.source_id, workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index.to_dict(), indent=2, default=str), "utf-8")
    _CACHE[_cache_key(index.source_id, workspace)] = index
    return path


def load_index(source_id: str, workspace: Path) -> SourceIndex | None:
    key = _cache_key(source_id, workspace)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    path = index_path(source_id, workspace)
    if not path.exists():
        return None
    try:
        index = SourceIndex.from_dict(json.loads(path.read_text("utf-8")))
    except (json.JSONDecodeError, OSError):
        return None
    _CACHE[key] = index
    return index


def drop_index(source_id: str, workspace: Path) -> None:
    _CACHE.pop(_cache_key(source_id, workspace), None)
    path = index_path(source_id, workspace)
    if path.exists():
        try:
            path.unlink()
        except OSError:  # pragma: no cover - best effort
            pass
