"""Source-of-Truth Engine — the orchestrator the API and UI talk to.

Wires the pieces into one pipeline:

    attach → sync (isolated checkout) → index (paths + symbols)
           → correlate (incident terms → ranked files)
           → compose (three trust tiers) → resolve (grounded proposal)

Everything before ``resolve`` is deterministic and offline-verifiable: given the
same evidence and the same commit, correlation returns the same files. Only the
last step involves an LLM, which is what makes the citations auditable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .context import GroundedContext, compose_context
from .correlate import CorrelationResult, correlate
from .index import SourceIndex, build_index, drop_index, load_index, save_index
from .models import AttachSourceRequest, Source
from .registry import SourceRegistry, identity_from_evidence
from .resolve import Resolution, propose_resolution
from .sync import index_root, purge_workspace, sync_source, touch_synced, workspace_root


@dataclass
class SyncReport:
    source_id: str
    ok: bool
    error: str | None = None
    commit: str | None = None
    files_indexed: int = 0
    symbols_indexed: int = 0
    truncated: bool = False
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "ok": self.ok,
            "error": self.error,
            "commit": self.commit,
            "files_indexed": self.files_indexed,
            "symbols_indexed": self.symbols_indexed,
            "truncated": self.truncated,
            "detail": self.detail,
        }


@dataclass
class GroundedAnalysis:
    """Full result of grounding one incident in source."""

    matched_sources: list[str] = field(default_factory=list)
    identity: dict = field(default_factory=dict)
    correlation: CorrelationResult | None = None
    context: GroundedContext | None = None
    resolution: Resolution | None = None
    note: str = ""

    def to_dict(self, *, include_prompt: bool = False) -> dict:
        return {
            "matched_sources": self.matched_sources,
            "identity": {k: v for k, v in self.identity.items() if v},
            "correlation": self.correlation.to_dict() if self.correlation else None,
            "context": self.context.to_dict() if self.context else None,
            "resolution": (
                self.resolution.to_dict(include_prompt=include_prompt)
                if self.resolution
                else None
            ),
            "note": self.note,
        }


class SourceEngine:
    def __init__(self, registry: SourceRegistry | None = None,
                 workspace: Any | None = None) -> None:
        self.registry = registry or SourceRegistry()
        self.workspace = workspace or workspace_root()

    # ------------------------------------------------------------------
    # Attach / detach
    # ------------------------------------------------------------------

    def list_sources(self) -> list[Source]:
        return self.registry.list()

    def attach(self, request: AttachSourceRequest) -> Source:
        return self.registry.add(request)

    def detach(self, source_id: str) -> bool:
        source = self.registry.get(source_id)
        if source is None:
            return False
        purge_workspace(source)
        drop_index(source_id, self.workspace)
        return self.registry.delete(source_id)

    # ------------------------------------------------------------------
    # Sync + index
    # ------------------------------------------------------------------

    def sync(self, source_id: str) -> SyncReport:
        source = self.registry.get(source_id)
        if source is None:
            return SyncReport(source_id=source_id, ok=False, error="unknown source")

        result = sync_source(source)
        source = touch_synced(source, result)

        if not result.ok or result.path is None:
            self.registry.upsert(source)
            return SyncReport(source_id=source_id, ok=False, error=result.error)

        try:
            root = index_root(source, result.path)
        except PermissionError as e:
            source.last_sync_error = str(e)
            self.registry.upsert(source)
            return SyncReport(source_id=source_id, ok=False, error=str(e))

        index = build_index(source_id, root)
        save_index(index, self.workspace)

        source.index_stats = index.stats
        self.registry.upsert(source)

        return SyncReport(
            source_id=source_id,
            ok=True,
            commit=result.commit,
            files_indexed=index.stats.files_indexed,
            symbols_indexed=index.stats.symbols_indexed,
            truncated=index.stats.truncated,
            detail=result.detail,
        )

    def sync_all(self) -> list[SyncReport]:
        return [self.sync(src.id) for src in self.list_sources()]

    def get_index(self, source_id: str) -> SourceIndex | None:
        return load_index(source_id, self.workspace)

    # ------------------------------------------------------------------
    # Correlate + resolve
    # ------------------------------------------------------------------

    def analyze(
        self,
        evidence: Any,
        *,
        resolve: bool = False,
        env: str = "prod",
        autopilot_enabled: bool = False,
        limit: int = 3,
        llm_settings: dict | None = None,
    ) -> GroundedAnalysis:
        """Ground one incident in source, optionally asking the LLM to resolve it.

        Returns a useful result at every level of degradation: no linked source,
        linked but never synced, synced but nothing matched. Each case carries a
        note explaining what to do about it rather than an empty response.
        """
        identity = identity_from_evidence(evidence)
        analysis = GroundedAnalysis(identity=identity)

        sources = self.registry.find_for_identity(**identity)
        analysis.matched_sources = [s.id for s in sources]

        index = None
        chosen: Source | None = None
        for source in sources:
            candidate = self.get_index(source.id)
            if candidate is not None:
                index, chosen = candidate, source
                break

        if not sources:
            declared = {k: v for k, v in identity.items() if v}
            analysis.note = (
                "No attached source is linked to this incident's identity "
                f"({declared or 'no service identity in the evidence payload'}). "
                "Attach a source and set its service link to ground this analysis in code."
            )
        elif index is None:
            analysis.note = (
                f"Source '{sources[0].id}' is linked but has no index yet — "
                "run a sync before correlating."
            )
        else:
            analysis.correlation = correlate(index, evidence, limit=limit)

        analysis.context = compose_context(
            evidence,
            index,
            analysis.correlation,
            docs_url=chosen.docs_url if chosen else None,
        )
        if analysis.note:
            analysis.context.notes.insert(0, analysis.note)

        if resolve:
            kind = (
                evidence.get("kind")
                if isinstance(evidence, dict)
                else getattr(evidence, "kind", None)
            )
            analysis.resolution = propose_resolution(
                analysis.context,
                evidence_kind=kind,
                env=env,
                autopilot_enabled=autopilot_enabled,
                llm_settings=llm_settings,
            )

        return analysis
