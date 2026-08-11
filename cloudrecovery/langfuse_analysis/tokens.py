"""Flag traces whose total token usage is anomalous.

Two independent triggers, either of which flags a trace:
  1. `hard_limit` — an absolute ceiling (e.g. your model's practical context
     budget). This catches a blowout immediately, with zero history required.
  2. Statistical outlier — z-score vs. the mean/stdev of the other traces
     scanned in the same run, for catching creeping regressions even when
     no single trace breaches a hard number yet.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass
class TokenFinding:
    trace_id: str
    total_tokens: int
    baseline_mean: float
    baseline_stdev: float
    z_score: float
    hard_limit_breached: bool


def flag_token_outliers(
    trace_token_totals: dict,
    *,
    hard_limit: int | None = 30_000,
    z_threshold: float = 3.0,
) -> list:
    totals = list(trace_token_totals.values())
    if len(totals) < 2:
        mean = totals[0] if totals else 0
        stdev = 0.0
    else:
        mean = statistics.mean(totals)
        stdev = statistics.pstdev(totals)

    findings = []
    for trace_id, total in trace_token_totals.items():
        z = (total - mean) / stdev if stdev > 0 else 0.0
        breached = hard_limit is not None and hard_limit > 0 and total >= hard_limit
        if breached or z >= z_threshold:
            findings.append(
                TokenFinding(
                    trace_id=trace_id,
                    total_tokens=total,
                    baseline_mean=mean,
                    baseline_stdev=stdev,
                    z_score=z,
                    hard_limit_breached=breached,
                )
            )
    findings.sort(key=lambda f: f.total_tokens, reverse=True)
    return findings
