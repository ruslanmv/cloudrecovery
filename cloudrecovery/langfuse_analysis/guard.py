"""Runtime circuit breaker — the actual self-repair mechanism.

Everything else in this package is post-hoc: it tells you a trace already
looped and already burned tokens. This module is the piece that stops it
*while it's happening*, inside your agent process, before the next LLM call
goes out. CloudRecovery's collector and tools observe Langfuse from the
outside and so can never do this; only code running inside your agent can.

Framework-agnostic on purpose: it doesn't know about LangGraph, CrewAI, or
your specific orchestrator. You call `guard.step(node_name, tokens_so_far)`
(or use it as a context manager) at the point in your loop where you're
about to re-enter a node/tool/LLM call, and it raises `RunawayLoopError`
once your thresholds are crossed — you decide what happens next (abort,
escalate, answer with what you have, page a human).

Example — wrapping a LangGraph-style dispatch loop:

    from cloudrecovery.langfuse_analysis.guard import LoopGuard, RunawayLoopError

    guard = LoopGuard(max_consecutive_repeats=3, max_total_tokens=30_000)

    while not done:
        node = plan_next_node(state)
        try:
            guard.step(node, tokens_used_so_far=state.total_tokens)
        except RunawayLoopError as e:
            state.outcome = "escalated"
            state.reason = f"loop_guard: {e}"
            break
        state = run_node(node, state)

Example — as a context manager around a single turn:

    with LoopGuard(max_consecutive_repeats=3, max_total_tokens=30_000) as guard:
        for node in agent_turn():
            guard.step(node.name, node.cumulative_tokens)
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


class RunawayLoopError(Exception):
    """Raised when the guard's thresholds are crossed."""


@dataclass
class LoopGuard:
    max_consecutive_repeats: int = 3
    """Abort once the same node name has been hit this many times in a row."""

    max_cycle_repeats: int = 3
    """Abort once a repeating block (any length up to max_cycle_length) repeats this many times."""

    max_cycle_length: int = 8
    """Longest block length to check for cyclic repetition."""

    max_total_tokens: int | None = 30_000
    """Absolute token ceiling for the whole guarded run. None disables this check."""

    on_trip: Callable[[str], None] | None = None
    """Optional callback invoked with a human-readable reason right before raising.
    Use this to fire a Langfuse annotation, push Evidence to a CloudRecovery
    control plane, or send a Slack alert, without coupling this module to any
    specific notification backend."""

    _history: list = field(default_factory=list, init=False, repr=False)

    def __enter__(self) -> LoopGuard:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def reset(self) -> None:
        self._history.clear()

    @property
    def history(self) -> list:
        """Steps recorded so far (copy), for logging or post-mortem evidence."""
        return list(self._history)

    def step(self, node_name: str, tokens_used_so_far: int = 0) -> None:
        """Record a step and raise RunawayLoopError if a threshold is crossed.

        Call this immediately before (or after) dispatching to `node_name`,
        passing the cumulative token count for the run so far.
        """
        self._history.append(node_name)

        if self.max_total_tokens is not None and tokens_used_so_far >= self.max_total_tokens:
            self._trip(
                f"token ceiling reached: {tokens_used_so_far:,} >= {self.max_total_tokens:,} "
                f"after {len(self._history)} steps"
            )

        if self._consecutive_repeats(node_name) >= self.max_consecutive_repeats:
            self._trip(
                f"node '{node_name}' repeated {self.max_consecutive_repeats}x in a row"
            )

        cycle = self._detect_cycle()
        if cycle is not None:
            block, repeats = cycle
            self._trip(
                f"cyclic loop detected: {' -> '.join(block)} repeated {repeats}x"
            )

    def _consecutive_repeats(self, node_name: str) -> int:
        count = 0
        for name in reversed(self._history):
            if name != node_name:
                break
            count += 1
        return count

    def _detect_cycle(self):
        n = len(self._history)
        for length in range(1, min(self.max_cycle_length, n // 2) + 1):
            tail = self._history[-length:]
            repeats = 1
            start = n - length
            while start - length >= 0 and self._history[start - length : start] == tail:
                repeats += 1
                start -= length
            if repeats >= self.max_cycle_repeats:
                return tail, repeats
        return None

    def _trip(self, reason: str) -> None:
        if self.on_trip is not None:
            self.on_trip(reason)
        raise RunawayLoopError(reason)
