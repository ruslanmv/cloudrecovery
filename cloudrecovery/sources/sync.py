"""Sync a source into an isolated, read-only workspace.

Two rules shape this module:

1. **Never touch a credential-bearing production checkout.** Remote sources are
   cloned shallowly into an ephemeral workspace under the app data dir. A token
   (read from the env var the source names) is injected into the clone URL
   in-memory only, and is scrubbed from every message that can reach a log, an
   API response, or an LLM prompt.
2. **Local sources are allow-listed, never arbitrary.** A `local` source must
   resolve inside ``CLOUDRECOVERY_SOURCE_ALLOWED_PATHS`` (os.pathsep-separated;
   defaults to the process working directory). Symlinks are resolved *before*
   the check, so a symlink inside an allowed directory cannot escape it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .models import REMOTE_KINDS, Source

CLONE_TIMEOUT_S = 300


@dataclass
class SyncResult:
    ok: bool
    path: Path | None = None
    commit: str | None = None
    error: str | None = None
    detail: str = ""


def workspace_root() -> Path:
    override = os.getenv("CLOUDRECOVERY_SOURCE_WORKSPACE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    base = os.getenv("CLOUDRECOVERY_DATA_DIR", "").strip()
    root = Path(base).expanduser().resolve() if base else (Path.home() / ".cloudrecovery")
    return (root / "sources_workspace").resolve()


def allowed_local_paths() -> list[Path]:
    raw = os.getenv("CLOUDRECOVERY_SOURCE_ALLOWED_PATHS", "").strip()
    if not raw:
        return [Path.cwd().resolve()]
    out = []
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if chunk:
            out.append(Path(chunk).expanduser().resolve())
    return out


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_local_path(local_path: str) -> Path:
    """Resolve and authorize a local source path, or raise PermissionError."""
    # strict=False so a missing path yields a clear "does not exist" below
    # rather than an OSError from resolve().
    candidate = Path(local_path).expanduser().resolve(strict=False)
    allowed = allowed_local_paths()

    if not any(_is_within(candidate, root) or candidate == root for root in allowed):
        raise PermissionError(
            f"local path '{candidate}' is outside the allow-list "
            f"({os.pathsep.join(str(p) for p in allowed)}). Set "
            "CLOUDRECOVERY_SOURCE_ALLOWED_PATHS to authorize it."
        )
    if not candidate.exists():
        raise FileNotFoundError(f"local path '{candidate}' does not exist")
    if not candidate.is_dir():
        raise NotADirectoryError(f"local path '{candidate}' is not a directory")
    return candidate


def _token_for(source: Source) -> str | None:
    if not source.credential_env:
        return None
    token = os.getenv(source.credential_env, "").strip()
    return token or None


def _authenticated_url(url: str, token: str | None) -> str:
    """Inject a token into an https clone URL, in-memory only."""
    if not token:
        return url
    parts = urlsplit(url)
    if parts.scheme != "https":
        # Never attach a token to ssh/git/file URLs.
        return url
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, f"x-access-token:{token}@{host}", parts.path,
                       parts.query, parts.fragment))


def scrub(text: str, token: str | None) -> str:
    """Remove a token (and any userinfo) from text before it escapes this module."""
    if not text:
        return ""
    if token:
        text = text.replace(token, "***")
    # git echoes the remote URL on failure; strip any embedded userinfo.
    return text.replace("x-access-token:***@", "").replace("x-access-token:", "")


def _run_git(args: list[str], *, cwd: Path | None = None,
             token: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Never let git block on an interactive credential prompt inside a server.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "true")
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=CLONE_TIMEOUT_S,
        check=False,
    )
    proc.stdout = scrub(proc.stdout, token)
    proc.stderr = scrub(proc.stderr, token)
    return proc


def checkout_dir(source: Source) -> Path:
    return workspace_root() / source.id


def sync_source(source: Source) -> SyncResult:
    """Materialize a source's files and return where they landed.

    `local` sources are used in place (never copied, never written to). Remote
    sources are shallow-cloned fresh: re-cloning is cheaper to reason about than
    reconciling a stale working tree, and guarantees the index reflects the ref
    that was asked for.
    """
    if source.kind == "local":
        try:
            path = resolve_local_path(source.local_path or "")
        except (PermissionError, FileNotFoundError, NotADirectoryError) as e:
            return SyncResult(ok=False, error=str(e))
        return SyncResult(ok=True, path=path, detail="local folder used in place")

    if source.kind not in REMOTE_KINDS:
        return SyncResult(ok=False, error=f"unsupported source kind: {source.kind}")

    if not source.url:
        return SyncResult(ok=False, error="source has no clone url")

    if shutil.which("git") is None:
        return SyncResult(ok=False, error="git is not installed on this host")

    token = _token_for(source)
    if source.credential_env and not token:
        return SyncResult(
            ok=False,
            error=(
                f"credential env var '{source.credential_env}' is unset or empty; "
                "export it where the control plane runs"
            ),
        )

    target = checkout_dir(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    args = ["clone", "--depth", "1", "--single-branch"]
    if source.ref:
        args += ["--branch", source.ref]
    args += [_authenticated_url(source.url, token), str(target)]

    try:
        proc = _run_git(args, token=token)
    except subprocess.TimeoutExpired:
        return SyncResult(ok=False, error=f"clone timed out after {CLONE_TIMEOUT_S}s")

    if proc.returncode != 0:
        return SyncResult(
            ok=False,
            error=(proc.stderr or proc.stdout or "git clone failed").strip().splitlines()[-1][:500],
        )

    commit = None
    rev = _run_git(["rev-parse", "HEAD"], cwd=target, token=token)
    if rev.returncode == 0:
        commit = rev.stdout.strip() or None

    return SyncResult(ok=True, path=target, commit=commit, detail="shallow clone")


def index_root(source: Source, checkout: Path) -> Path:
    """Apply `subpath`, refusing to escape the checkout."""
    if not source.subpath:
        return checkout
    candidate = (checkout / source.subpath).resolve()
    if not (_is_within(candidate, checkout.resolve()) or candidate == checkout.resolve()):
        raise PermissionError(f"subpath '{source.subpath}' escapes the checkout")
    return candidate


def purge_workspace(source: Source) -> None:
    """Delete a source's ephemeral checkout. Never touches `local` sources."""
    if source.kind == "local":
        return
    target = checkout_dir(source)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def touch_synced(source: Source, result: SyncResult) -> Source:
    """Record sync outcome on the source record."""
    source.last_synced_at = datetime.now(UTC)
    source.last_sync_error = None if result.ok else result.error
    if result.ok and result.commit:
        source.last_synced_commit = result.commit
    return source
