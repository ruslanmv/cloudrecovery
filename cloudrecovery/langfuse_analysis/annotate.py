"""Write diagnostic findings back into Langfuse as scores/comments, so they
show up next to the trace in the UI — the same "Annotate" / "Add comment"
panel you'd use by hand.

Scoring is the one *mutating* thing this feature does, and the SDK renamed
the method across versions: langfuse>=3 exposes ``client.create_score(...)``
while langfuse 2.x exposed ``client.score(...)``. ``write_score`` probes for
whichever exists instead of guessing, then flushes — the SDK batches score
events in a background queue, so a short-lived process (a runbook step, a
one-shot poll) would otherwise exit before anything reached the server.
"""
from __future__ import annotations

LOOP_SCORE_NAME = "cloudrecovery_loop_detected"
TOKEN_SCORE_NAME = "cloudrecovery_token_outlier"


def write_score(
    client,
    *,
    trace_id: str,
    name: str,
    value: float,
    comment: str | None = None,
) -> str:
    """Attach a score (with optional comment) to a trace. Returns the method used."""
    kwargs = {"trace_id": trace_id, "name": name, "value": value, "comment": comment}

    method_used = None
    for method_name in ("create_score", "score"):
        method = getattr(client, method_name, None)
        if callable(method):
            method(**kwargs)
            method_used = method_name
            break

    if method_used is None:
        raise AttributeError(
            "This Langfuse client exposes neither create_score() nor score(); "
            "cannot write the annotation back to Langfuse."
        )

    # Scores are queued and sent asynchronously; flush so short-lived callers
    # (a runbook step, `--once` poll) don't exit before the write lands.
    flush = getattr(client, "flush", None)
    if callable(flush):
        try:
            flush()
        except Exception:  # pragma: no cover - best effort
            pass

    return method_used


def annotate_loop_finding(client, finding, *, score_name: str = LOOP_SCORE_NAME) -> str:
    return write_score(
        client,
        trace_id=finding.trace_id,
        name=score_name,
        value=1,
        comment=(
            f"Repeating cycle detected: {finding.cycle_str} "
            f"x{finding.repeat_count} ({finding.total_tokens:,} tokens spent looping)."
        ),
    )


def annotate_token_finding(client, finding, *, score_name: str = TOKEN_SCORE_NAME) -> str:
    note = (
        f"Total tokens {finding.total_tokens:,} vs baseline mean "
        f"{finding.baseline_mean:,.0f} (z={finding.z_score:.1f})."
    )
    if finding.hard_limit_breached:
        note += " HARD LIMIT BREACHED."
    return write_score(
        client,
        trace_id=finding.trace_id,
        name=score_name,
        value=finding.total_tokens,
        comment=note,
    )
