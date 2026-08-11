"""Source-of-Truth Engine: attach the code and config behind a running system.

Everything else in CloudRecovery reasons about *symptoms* — a repeating span
name, a CrashLoopBackOff, a failed synthetic check. None of it has ever seen the
code that produced them. This package attaches real repositories, GitOps repos,
and local folders so resolution is grounded in actual files instead of
pattern-matching on trace names.

Pipeline:

    attach → sync → index → correlate → compose context → resolve

Governance is additive to what already exists, never a bypass of it: read-only
tokens by default, credentials stored as env-var *names* only, local sources
confined to an allow-list, documentation fetching off by default and never
sufficient on its own, and every proposed mutation carrying the decision from the
same ``action_policy.validate_action()`` gate that protects ``ocp.*`` today.

Entry point is ``SourceEngine``; ``cloudrecovery.server`` exposes it under
``/api/sources``.
"""
from __future__ import annotations

from .context import GroundedContext, compose_context
from .correlate import CorrelationResult, correlate, extract_terms
from .engine import GroundedAnalysis, SourceEngine, SyncReport
from .index import SourceIndex, build_index
from .models import AttachSourceRequest, ServiceLink, Source
from .registry import SourceRegistry, identity_from_evidence
from .resolve import Resolution, propose_resolution

__all__ = [
    "AttachSourceRequest",
    "CorrelationResult",
    "GroundedAnalysis",
    "GroundedContext",
    "Resolution",
    "ServiceLink",
    "Source",
    "SourceEngine",
    "SourceIndex",
    "SourceRegistry",
    "SyncReport",
    "build_index",
    "compose_context",
    "correlate",
    "extract_terms",
    "identity_from_evidence",
    "propose_resolution",
]
