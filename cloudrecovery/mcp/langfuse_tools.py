"""Langfuse tools for CloudRecovery's shared tool layer.

Mirrors cloudrecovery/mcp/synthetics_tools.py and cloudrecovery/mcp/openshift.py:
thin async wrappers around cloudrecovery.langfuse_analysis, registered into
ToolRegistry the same way OpenShift/host/synthetics tools are (see tools.py).
This is what makes langfuse.* usable from both the web autopilot UI and any
external MCP client, through the exact same policy-gated dispatch path.

Read-only tools (langfuse.scan, langfuse.diagnose) need no approval, matching
action_policy.py's default. langfuse.annotate is mutating but low-risk (it
only writes a score/comment to Langfuse, no production system is touched) —
still routed through validate_action() so it's auditable and consistent with
how every other mutating tool in this codebase is handled.

The Langfuse SDK is an optional extra. When it is missing or unconfigured,
every tool returns a structured ``error``/``hint`` dict rather than raising,
so adding this feature cannot break an existing CloudRecovery deployment.
The underlying calls are synchronous and network-bound, so they run in a
worker thread to keep the event loop responsive.
"""

from __future__ import annotations

import asyncio
from typing import Any

from cloudrecovery.langfuse_analysis import (
    DEFAULT_MIN_REPEATS,
    DEFAULT_TOKEN_LIMIT,
    build_client,
    describe_target,
    diagnose_trace,
    scan_traces,
    write_score,
)

DEFAULT_ANNOTATION_SCORE = "cloudrecovery_annotation"


def _error(exc: Exception) -> dict[str, Any]:
    """Normalize a configuration/connectivity failure into a tool result."""
    target = describe_target()
    return {
        "error": str(exc),
        "hint": (
            'Install the optional extra (pip install "cloudrecovery[langfuse]") and set '
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY. For a local or self-hosted "
            "Langfuse also set LANGFUSE_HOST (e.g. http://localhost:3000)."
        ),
        "langfuse_target": target,
    }


async def status() -> dict[str, Any]:
    """Report whether Langfuse analysis is usable, and against which deployment."""
    target = describe_target()
    ready = target["sdk_available"] and target["credentials_present"]
    return {"ready": ready, **target}


async def scan(
    since_hours: float = 24,
    name: str | None = None,
    tags: list[str] | None = None,
    min_repeats: int = DEFAULT_MIN_REPEATS,
    token_limit: int = DEFAULT_TOKEN_LIMIT,
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            scan_traces,
            since_hours=since_hours,
            name=name,
            tags=tags,
            min_repeats=min_repeats,
            token_limit=token_limit,
        )
    except Exception as e:
        return _error(e)
    return result.to_dict()


async def diagnose(trace_id: str, min_repeats: int = DEFAULT_MIN_REPEATS) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            diagnose_trace, trace_id=trace_id, min_repeats=min_repeats
        )
    except Exception as e:
        return _error(e)


async def annotate(
    trace_id: str,
    note: str,
    score_name: str = DEFAULT_ANNOTATION_SCORE,
    value: float = 1,
) -> dict[str, Any]:
    def _annotate() -> str:
        client = build_client()
        return write_score(
            client, trace_id=trace_id, name=score_name, value=value, comment=note
        )

    try:
        method = await asyncio.to_thread(_annotate)
    except Exception as e:
        return _error(e)

    return {
        "trace_id": trace_id,
        "annotated": True,
        "score_name": score_name,
        "sdk_method": method,
    }
