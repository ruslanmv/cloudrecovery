"""Langfuse trace analysis for CloudRecovery — loops, token blowouts, annotations.

CloudRecovery treats Langfuse as one more observable backend alongside
OpenShift, hosts, and synthetic checks: an external system it reads and
records findings from. Langfuse may be **cloud** or **local / self-hosted** —
see ``client.describe_target()``.

This is a purely additive feature. Nothing here is imported at CloudRecovery
startup, and the ``langfuse`` SDK is an optional extra
(``pip install "cloudrecovery[langfuse]"``): with it absent, every consumer
degrades to a clear "not configured" result instead of failing.

Layout:

* ``loops`` / ``tokens`` — pure-Python analyzers, no dependencies, always usable.
* ``client``   — read-side SDK wrapper (list traces, fetch observations, tokens).
* ``annotate`` — the only mutating call: write a score/comment back to a trace.
* ``guard``    — ``LoopGuard``, the in-process circuit breaker for your agent.
* ``report``   — terminal + Markdown rendering of findings.

``scan_traces`` and ``diagnose_trace`` below are the shared entry points used by
the MCP tools, the signal collector, and the CLI, so all three agree on what a
scan means.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .annotate import annotate_loop_finding, annotate_token_finding, write_score
from .client import (
    build_client,
    describe_target,
    get_observations_for_trace,
    iter_traces,
    observation_token_total,
    sdk_available,
)
from .guard import LoopGuard, RunawayLoopError
from .loops import LoopFinding, detect_cycles
from .tokens import TokenFinding, flag_token_outliers

__all__ = [
    "LoopFinding",
    "LoopGuard",
    "RunawayLoopError",
    "ScanResult",
    "TokenFinding",
    "annotate_loop_finding",
    "annotate_token_finding",
    "build_client",
    "describe_target",
    "detect_cycles",
    "diagnose_trace",
    "flag_token_outliers",
    "get_observations_for_trace",
    "iter_traces",
    "observation_token_total",
    "scan_traces",
    "sdk_available",
    "write_score",
]

DEFAULT_MIN_REPEATS = 3
DEFAULT_TOKEN_LIMIT = 30_000


@dataclass
class ScanResult:
    scanned_traces: int = 0
    loop_findings: list[LoopFinding] = field(default_factory=list)
    token_findings: list[TokenFinding] = field(default_factory=list)
    trace_token_totals: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_traces": self.scanned_traces,
            "loop_findings": [
                {
                    "trace_id": f.trace_id,
                    "cycle": list(f.cycle),
                    "repeat_count": f.repeat_count,
                    "tokens_in_loop": f.total_tokens,
                }
                for f in self.loop_findings
            ],
            "token_findings": [
                {
                    "trace_id": f.trace_id,
                    "total_tokens": f.total_tokens,
                    "baseline_mean": f.baseline_mean,
                    "z_score": f.z_score,
                    "hard_limit_breached": f.hard_limit_breached,
                }
                for f in self.token_findings
            ],
        }


def _observation_sequence(client, trace_id: str):
    """Return (names, tokens) for a trace, index-aligned in execution order."""
    observations = get_observations_for_trace(client, trace_id)
    names = [getattr(o, "name", None) or getattr(o, "type", "unknown") for o in observations]
    tokens = [observation_token_total(o) for o in observations]
    return names, tokens


def scan_traces(
    client=None,
    *,
    since_hours: float = 24,
    name: str | None = None,
    tags: Sequence[str] | None = None,
    min_repeats: int = DEFAULT_MIN_REPEATS,
    token_limit: int = DEFAULT_TOKEN_LIMIT,
) -> ScanResult:
    """Scan recent traces for repeating loops and token outliers.

    Synchronous and network-bound (it talks to Langfuse). Async callers should
    offload it, e.g. ``await asyncio.to_thread(scan_traces, ...)``.
    """
    client = client or build_client()

    result = ScanResult()
    for trace in iter_traces(client, since_hours=since_hours, tags=tags or None, name=name):
        result.scanned_traces += 1
        names, tokens = _observation_sequence(client, trace.id)
        result.trace_token_totals[trace.id] = sum(tokens)
        result.loop_findings.extend(
            detect_cycles(names, tokens, trace.id, min_repeats=min_repeats)
        )

    result.token_findings = flag_token_outliers(
        result.trace_token_totals, hard_limit=token_limit
    )
    return result


def diagnose_trace(
    client=None,
    *,
    trace_id: str,
    min_repeats: int = DEFAULT_MIN_REPEATS,
) -> dict[str, Any]:
    """Return the full step-by-step picture for a single trace."""
    client = client or build_client()
    names, tokens = _observation_sequence(client, trace_id)
    loop_findings = detect_cycles(names, tokens, trace_id, min_repeats=min_repeats)

    return {
        "trace_id": trace_id,
        "step_count": len(names),
        "total_tokens": sum(tokens),
        "observation_sequence": names,
        "loop_findings": [
            {
                "cycle": list(f.cycle),
                "repeat_count": f.repeat_count,
                "tokens_in_loop": f.total_tokens,
            }
            for f in loop_findings
        ],
    }
