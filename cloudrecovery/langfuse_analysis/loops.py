"""Detect repeating call patterns (agent loops) within a trace's observation sequence.

The core idea: if your agent gets stuck (e.g. a LangGraph node keeps
re-routing back to itself, or a retry wrapper keeps re-calling the same
chain), the trace's observation names will show the SAME block repeating
back-to-back — exactly like:

    ticket_turn -> intake_gate -> injection_check -> extract -> classify
    ticket_turn -> intake_gate -> injection_check -> extract -> classify
    ticket_turn -> intake_gate -> injection_check -> extract -> classify

This module finds such repeating blocks and reports how many times each
repeated and how many tokens were burned inside the loop.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class LoopFinding:
    trace_id: str
    cycle: tuple
    repeat_count: int
    start_index: int
    total_tokens: int

    @property
    def cycle_str(self) -> str:
        return " -> ".join(self.cycle)


def detect_cycles(
    names: Sequence[str],
    tokens: Sequence[int],
    trace_id: str,
    *,
    min_repeats: int = 3,
    max_cycle_length: int = 8,
) -> list:
    """Find runs of a repeating block of observation names.

    Looks for any window length L (1..max_cycle_length) where the same
    block of L consecutive names repeats at least `min_repeats` times
    back to back. Overlapping detections are de-duplicated so a single
    loop doesn't get reported multiple times at different granularities.
    """
    names = list(names)
    tokens = list(tokens or [])
    n = len(names)
    if n == 0 or min_repeats < 2:
        return []

    findings = []
    covered = [False] * n

    for length in range(1, max_cycle_length + 1):
        i = 0
        while i + length * min_repeats <= n:
            if covered[i]:
                i += 1
                continue
            block = tuple(names[i : i + length])
            repeats = 1
            j = i + length
            while j + length <= n and tuple(names[j : j + length]) == block:
                repeats += 1
                j += length
            if repeats >= min_repeats and not any(covered[i:j]):
                # tokens may be shorter than names (or absent); slicing keeps
                # this total best-effort rather than raising.
                span_tokens = sum(
                    t for t in tokens[i:j] if isinstance(t, (int, float)) and not isinstance(t, bool)
                )
                findings.append(
                    LoopFinding(
                        trace_id=trace_id,
                        cycle=block,
                        repeat_count=repeats,
                        start_index=i,
                        total_tokens=int(span_tokens),
                    )
                )
                for k in range(i, j):
                    covered[k] = True
                i = j
            else:
                i += 1

    # Report the costliest / longest-running loops first.
    findings.sort(key=lambda f: (f.total_tokens, f.repeat_count), reverse=True)
    return findings
