"""Render diagnostic findings to the terminal and to a Markdown report."""
from __future__ import annotations

from rich.console import Console
from rich.table import Table

console = Console()


def print_summary(loop_findings, token_findings, scanned: int) -> None:
    console.rule(f"[bold]CloudRecovery Langfuse scan — {scanned} traces")

    if loop_findings:
        table = Table(title="🔁 Repeated call loops")
        table.add_column("Trace ID")
        table.add_column("Cycle")
        table.add_column("Repeats", justify="right")
        table.add_column("Tokens in loop", justify="right")
        for f in loop_findings:
            table.add_row(f.trace_id, f.cycle_str, str(f.repeat_count), f"{f.total_tokens:,}")
        console.print(table)
    else:
        console.print("[green]No repeating call patterns detected.[/green]")

    if token_findings:
        table = Table(title="💸 Token budget outliers")
        table.add_column("Trace ID")
        table.add_column("Total tokens", justify="right")
        table.add_column("Baseline mean", justify="right")
        table.add_column("Z-score", justify="right")
        table.add_column("Hard limit?")
        for f in token_findings:
            table.add_row(
                f.trace_id,
                f"{f.total_tokens:,}",
                f"{f.baseline_mean:,.0f}",
                f"{f.z_score:.1f}",
                "⚠️ yes" if f.hard_limit_breached else "no",
            )
        console.print(table)
    else:
        console.print("[green]No token outliers detected.[/green]")


def to_markdown(loop_findings, token_findings) -> str:
    lines = ["# CloudRecovery Langfuse Report", ""]

    lines.append("## Repeated call loops")
    if loop_findings:
        lines.append("| Trace ID | Cycle | Repeats | Tokens in loop |")
        lines.append("|---|---|---|---|")
        for f in loop_findings:
            lines.append(f"| {f.trace_id} | {f.cycle_str} | {f.repeat_count} | {f.total_tokens:,} |")
    else:
        lines.append("_None detected._")

    lines.append("")
    lines.append("## Token budget outliers")
    if token_findings:
        lines.append("| Trace ID | Total tokens | Baseline mean | Z-score | Hard limit? |")
        lines.append("|---|---|---|---|---|")
        for f in token_findings:
            flag = "⚠️ yes" if f.hard_limit_breached else "no"
            lines.append(
                f"| {f.trace_id} | {f.total_tokens:,} | {f.baseline_mean:,.0f} | {f.z_score:.1f} | {flag} |"
            )
    else:
        lines.append("_None detected._")

    return "\n".join(lines)
