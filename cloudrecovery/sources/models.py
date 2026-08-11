"""Source-of-Truth data model.

Everything CloudRecovery reasoned about before this module was a *symptom*: a
repeating span name, a CrashLoopBackOff, a failed synthetic check. A Source
attaches the code and config that produced the symptom, so resolution can be
grounded in real files instead of pattern-matching on trace names.

The load-bearing field is ``service_links``: the join key that maps a source to
the identity an incident already carries — a ``service.name`` from trace
metadata, an OpenShift ``namespace/deployment``, or an ArgoCD Application name.
Everything downstream (correlate, context, resolve) depends on that mapping, so
a source without links can be stored but will never be selected for an incident.

Credentials are never stored here. A source records the *name* of an environment
variable holding its token (``credential_env``); the token is read at sync time
and injected into the clone URL in-memory only.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SourceKind = Literal["github", "gitlab", "local", "gitops"]

REMOTE_KINDS = {"github", "gitlab", "gitops"}

# Conservative: a source id becomes a directory name in the sync workspace.
_ID_SAFE = re.compile(r"[^a-z0-9._-]+")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def slugify_id(value: str) -> str:
    slug = _ID_SAFE.sub("-", value.strip().lower()).strip("-.")
    return slug or "source"


class ServiceLink(BaseModel):
    """How an incident is matched to this source.

    At least one field must be set. Matching is exact and case-insensitive;
    substring guessing would silently attach the wrong repo to an incident,
    which is worse than not correlating at all.
    """

    service_name: str | None = None
    """Matches `service.name` in trace metadata, e.g. "blue-agent-core"."""

    namespace: str | None = None
    """OpenShift/Kubernetes namespace."""

    deployment: str | None = None
    """OpenShift/Kubernetes deployment (scoped by namespace when both are set)."""

    argocd_application: str | None = None
    """ArgoCD Application name, for GitOps drift correlation."""

    def is_empty(self) -> bool:
        return not any(
            [self.service_name, self.namespace, self.deployment, self.argocd_application]
        )

    def matches(self, *, service_name=None, namespace=None, deployment=None,
                argocd_application=None) -> bool:
        """True when every field this link declares is satisfied by the incident.

        Declared fields are ANDed, so a link naming both namespace and
        deployment does not match a different deployment in that namespace.
        """
        if self.is_empty():
            return False

        def eq(mine: str | None, theirs: str | None) -> bool:
            return bool(mine) and bool(theirs) and mine.strip().lower() == theirs.strip().lower()

        checks = []
        if self.service_name:
            checks.append(eq(self.service_name, service_name))
        if self.namespace:
            checks.append(eq(self.namespace, namespace))
        if self.deployment:
            checks.append(eq(self.deployment, deployment))
        if self.argocd_application:
            checks.append(eq(self.argocd_application, argocd_application))
        return all(checks)


class IndexStats(BaseModel):
    """Summary of the last index build. Paths and symbols only — never content."""

    files_indexed: int = 0
    symbols_indexed: int = 0
    skipped_files: int = 0
    built_at: datetime | None = None
    truncated: bool = False
    """True when the repo exceeded the file cap and indexing stopped early."""


class Source(BaseModel):
    """An attached repository, GitOps repo, or local folder."""

    id: str
    name: str
    kind: SourceKind

    url: str | None = None
    """Clone URL for github/gitlab/gitops kinds."""

    ref: str | None = None
    """Branch, tag, or commit. Defaults to the remote's default branch."""

    subpath: str | None = None
    """Restrict indexing to this directory inside the repo."""

    local_path: str | None = None
    """Filesystem path for `local` kind. Must fall inside the allow-list."""

    credential_env: str | None = None
    """Name of the env var holding the token. The token itself is never stored."""

    docs_url: str | None = None
    """Optional documentation URL — tier-3, lowest-trust context. Off by default."""

    service_links: list[ServiceLink] = Field(default_factory=list)

    write_enabled: bool = False
    """Read-only by default. Write scope is only for the draft-PR path."""

    created_at: datetime = Field(default_factory=_utcnow)
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None
    last_synced_commit: str | None = None
    index_stats: IndexStats = Field(default_factory=IndexStats)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        slug = slugify_id(v)
        if slug != v:
            raise ValueError(f"source id must be slug-safe (got {v!r}, expected {slug!r})")
        return v

    def links_match(self, **identity: Any) -> bool:
        return any(link.matches(**identity) for link in self.service_links)

    def public_dict(self) -> dict:
        """Serialization for the API/UI. Contains no secrets by construction."""
        return self.model_dump(mode="json")


class AttachSourceRequest(BaseModel):
    """Payload for POST /api/sources."""

    name: str
    kind: SourceKind
    url: str | None = None
    ref: str | None = None
    subpath: str | None = None
    local_path: str | None = None
    credential_env: str | None = None
    docs_url: str | None = None
    service_links: list[ServiceLink] = Field(default_factory=list)
    write_enabled: bool = False

    def to_source(self) -> Source:
        if self.kind in REMOTE_KINDS and not self.url:
            raise ValueError(f"kind '{self.kind}' requires a clone url")
        if self.kind == "local" and not self.local_path:
            raise ValueError("kind 'local' requires a local_path")

        links = [link for link in self.service_links if not link.is_empty()]
        return Source(
            id=slugify_id(self.name),
            name=self.name,
            kind=self.kind,
            url=self.url,
            ref=self.ref,
            subpath=self.subpath,
            local_path=self.local_path,
            credential_env=self.credential_env,
            docs_url=self.docs_url,
            service_links=links,
            write_enabled=self.write_enabled,
        )
