"""Persistent registry of attached sources, and the incident → source join.

Stored as JSON under the same app data dir as settings.json
(``CLOUDRECOVERY_DATA_DIR``, default ``~/.cloudrecovery``). No credentials are
written: a Source records the *name* of an env var, never its value.

``find_for_identity`` is the join everything downstream depends on. It matches
only on explicitly declared links — never on fuzzy name similarity, because
attaching the wrong repo to an incident produces confidently wrong file
citations, which is worse than returning nothing.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .models import AttachSourceRequest, Source

_LOCK = threading.Lock()


def _app_data_dir() -> Path:
    base = os.getenv("CLOUDRECOVERY_DATA_DIR", "").strip()
    if base:
        return Path(base).expanduser().resolve()
    return (Path.home() / ".cloudrecovery").resolve()


def sources_path() -> Path:
    override = os.getenv("CLOUDRECOVERY_SOURCES_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _app_data_dir() / "sources.json"


class SourceRegistry:
    """CRUD over attached sources, plus incident correlation lookup."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else sources_path()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _read_raw(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt store must not take down the control plane; an empty
            # registry degrades to "no sources attached".
            return []
        if isinstance(data, dict):
            data = data.get("sources", [])
        return data if isinstance(data, list) else []

    def _write_raw(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"sources": rows}, indent=2, default=str), "utf-8")
        tmp.replace(self.path)

    def list(self) -> list[Source]:
        out: list[Source] = []
        for row in self._read_raw():
            try:
                out.append(Source.model_validate(row))
            except Exception:
                # Skip rows written by a newer/older schema rather than failing
                # the whole listing.
                continue
        return out

    def get(self, source_id: str) -> Source | None:
        for src in self.list():
            if src.id == source_id:
                return src
        return None

    def add(self, request: AttachSourceRequest) -> Source:
        source = request.to_source()
        with _LOCK:
            rows = self._read_raw()
            if any(r.get("id") == source.id for r in rows):
                raise ValueError(f"a source named '{source.name}' already exists")
            rows.append(source.model_dump(mode="json"))
            self._write_raw(rows)
        return source

    def upsert(self, source: Source) -> Source:
        with _LOCK:
            rows = self._read_raw()
            replaced = False
            for i, row in enumerate(rows):
                if row.get("id") == source.id:
                    rows[i] = source.model_dump(mode="json")
                    replaced = True
                    break
            if not replaced:
                rows.append(source.model_dump(mode="json"))
            self._write_raw(rows)
        return source

    def delete(self, source_id: str) -> bool:
        with _LOCK:
            rows = self._read_raw()
            remaining = [r for r in rows if r.get("id") != source_id]
            if len(remaining) == len(rows):
                return False
            self._write_raw(remaining)
        return True

    # ------------------------------------------------------------------
    # Incident → source join
    # ------------------------------------------------------------------

    def find_for_identity(self, **identity: Any) -> list[Source]:
        """Return sources whose declared links match this incident identity.

        Accepts service_name / namespace / deployment / argocd_application.
        More specific links sort first, so a namespace+deployment link is
        preferred over a namespace-only catch-all.
        """
        identity = {k: v for k, v in identity.items() if v}
        if not identity:
            return []

        matched = [src for src in self.list() if src.links_match(**identity)]

        def specificity(src: Source) -> int:
            best = 0
            for link in src.service_links:
                if link.matches(**identity):
                    declared = sum(
                        1
                        for f in (
                            link.service_name,
                            link.namespace,
                            link.deployment,
                            link.argocd_application,
                        )
                        if f
                    )
                    best = max(best, declared)
            return best

        matched.sort(key=specificity, reverse=True)
        return matched

    def find_for_evidence(self, evidence: Any) -> list[Source]:
        """Resolve sources for an Evidence object or an equivalent dict."""
        return self.find_for_identity(**identity_from_evidence(evidence))


def identity_from_evidence(evidence: Any) -> dict:
    """Extract the join key from Evidence, wherever the producer happened to put it.

    Collectors are inconsistent about this by nature: the Langfuse collector
    knows a ``service.name``, the OpenShift collector knows namespace/deployment,
    an ArgoCD signal knows an Application. Rather than force one shape on every
    producer, read all the spellings that actually occur.
    """
    if evidence is None:
        return {}

    if isinstance(evidence, dict):
        payload = evidence.get("payload") or {}
    else:
        payload = getattr(evidence, "payload", None) or {}

    if not isinstance(payload, dict):
        payload = {}

    def pick(*keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # Nested metadata, e.g. {"metadata": {"service.name": "..."}}
        meta = payload.get("metadata")
        if isinstance(meta, dict):
            for key in keys:
                value = meta.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    return {
        "service_name": pick("service.name", "service_name", "service"),
        "namespace": pick("namespace", "ns"),
        "deployment": pick("deployment", "workload", "app"),
        "argocd_application": pick("argocd_application", "argocd_app", "application"),
    }
