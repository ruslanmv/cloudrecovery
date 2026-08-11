"""Langfuse signal collector for CloudRecovery.

Mirrors cloudrecovery/signals/synthetics.py: a periodic checker that emits
normalized Evidence objects into the same stream that agent:host, agent:ocp,
and synthetics already feed. This is the plug point that lets CloudRecovery's
existing AI copilot, timeline, and (eventually) runbook autopilot treat an
agent loop/token blowout exactly like a CrashLoopBackOff or site-down
incident — same UI, same policy engine, same autopilot tiers.

Requires the optional ``langfuse`` extra and a Langfuse instance — cloud or
self-hosted — reachable from wherever this collector runs (control plane host
or an agent). Loop/token detection logic is intentionally NOT reimplemented
here: it comes from cloudrecovery.langfuse_analysis, which the MCP tools and
the CLI share, so there is one source of truth for what counts as a loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .models import Evidence

DEFAULT_MIN_REPEATS = 3
DEFAULT_TOKEN_LIMIT = 30_000


@dataclass
class LangfuseCollectorConfig:
    since_hours: float = 1.0
    """Lookback window per poll. Should be >= poll interval to avoid gaps."""
    name_filter: str | None = None
    tags: list[str] | None = field(default_factory=list)
    min_repeats: int = DEFAULT_MIN_REPEATS
    token_limit: int = DEFAULT_TOKEN_LIMIT


def _collector_error(message: str) -> Evidence:
    return Evidence(
        source="agent:langfuse",
        kind="collector_error",
        severity="warning",
        message=message,
    )


async def check_langfuse(cfg: LangfuseCollectorConfig) -> list[Evidence]:
    """One poll cycle: scan recent traces, return Evidence for each finding.

    Returns an empty-of-findings list (an informational ``langfuse_scan``
    event) rather than an error when nothing is wrong — same contract as a
    clean synthetics check. Configuration and connectivity problems come back
    as ``collector_error`` Evidence so a misconfigured collector degrades
    visibly instead of taking down the caller.
    """
    try:
        from cloudrecovery.langfuse_analysis import scan_traces
    except Exception as e:  # pragma: no cover - import-time failure is unexpected
        return [_collector_error(f"Langfuse analysis unavailable: {e}")]

    try:
        result = await asyncio.to_thread(
            scan_traces,
            since_hours=cfg.since_hours,
            name=cfg.name_filter,
            tags=cfg.tags or None,
            min_repeats=cfg.min_repeats,
            token_limit=cfg.token_limit,
        )
    except OSError as e:
        # A missing SDK, missing credentials, or an unreachable host
        # (ConnectionError is an OSError subclass). All of these are problems
        # with the collector itself, not incidents in the observed agent, so
        # they must not be reported as loops.
        return [
            _collector_error(
                f"Langfuse collector unavailable — check the langfuse extra, "
                f"credentials, LANGFUSE_HOST, and connectivity: {e}"
            )
        ]

    evidence: list[Evidence] = []

    for finding in result.loop_findings:
        evidence.append(
            Evidence(
                source="agent:langfuse",
                kind="langfuse_loop",
                severity="critical",
                message=(
                    f"Repeating loop in trace {finding.trace_id}: {finding.cycle_str} "
                    f"x{finding.repeat_count} ({finding.total_tokens:,} tokens burned)"
                ),
                payload={
                    "trace_id": finding.trace_id,
                    "cycle": list(finding.cycle),
                    "repeat_count": finding.repeat_count,
                    "tokens_in_loop": finding.total_tokens,
                },
                incident_id=f"langfuse-loop-{finding.trace_id}",
            )
        )

    for finding in result.token_findings:
        evidence.append(
            Evidence(
                source="agent:langfuse",
                kind="langfuse_token_outlier",
                severity="critical" if finding.hard_limit_breached else "warning",
                message=(
                    f"Trace {finding.trace_id} used {finding.total_tokens:,} tokens "
                    f"(baseline {finding.baseline_mean:,.0f}, z={finding.z_score:.1f})"
                ),
                payload={
                    "trace_id": finding.trace_id,
                    "total_tokens": finding.total_tokens,
                    "baseline_mean": finding.baseline_mean,
                    "z_score": finding.z_score,
                    "hard_limit_breached": finding.hard_limit_breached,
                },
                incident_id=f"langfuse-tokens-{finding.trace_id}",
            )
        )

    if not evidence:
        evidence.append(
            Evidence(
                source="agent:langfuse",
                kind="langfuse_scan",
                severity="info",
                message=(
                    f"Scanned {result.scanned_traces} trace(s) from the last "
                    f"{cfg.since_hours}h, no loops or token outliers found."
                ),
                payload={"scanned_traces": result.scanned_traces},
            )
        )

    return evidence


async def periodic_checks(cfg: LangfuseCollectorConfig, emit, interval_s: float = 60.0) -> None:
    """emit(Evidence) callback — same signature as synthetics.periodic_checks."""
    while True:
        try:
            for ev in await check_langfuse(cfg):
                await emit(ev)
        except Exception as e:  # pragma: no cover - defensive; keep polling alive
            await emit(_collector_error(f"Langfuse collector poll failed: {e}"))
        await asyncio.sleep(interval_s)
