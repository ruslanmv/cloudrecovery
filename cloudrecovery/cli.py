from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn


def _default_script_path() -> str:
    """Prefer packaged script if present, otherwise fallback to ./scripts."""
    here = Path(__file__).resolve().parent
    candidate = (here.parent / "scripts" / "monitor_anything.sh").resolve()
    if candidate.exists():
        return str(candidate)
    local = (Path.cwd() / "scripts" / "monitor_anything.sh").resolve()
    return str(local)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cloudrecovery")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # --- UI ---
    ui = sub.add_parser("ui", help="Start the CloudRecovery web workspace (terminal + AI sidecar)")
    ui.add_argument("--host", default="127.0.0.1", help="Bind host")
    ui.add_argument("--port", type=int, default=8787, help="Bind port")
    ui.add_argument(
        "--cmd",
        dest="run_cmd",
        default=_default_script_path(),
        help="Command to run inside the terminal session (default: scripts/monitor_anything.sh)",
    )
    ui.add_argument("--title", default="CloudRecovery Enterprise Workspace", help="UI title")

    # --- MCP ---
    mcp = sub.add_parser("mcp", help="Run CloudRecovery as an MCP server over stdio")
    mcp.add_argument(
        "--cmd",
        dest="run_cmd",
        required=True,
        help="Command to run inside the PTY session (e.g., ./scripts/monitor_anything.sh)",
    )

    # --- Optional: settings/models ---
    sub.add_parser("settings", help="Print current LLM settings")
    sub.add_parser("models", help="List models for the active LLM provider")

    # --- Langfuse (LLM-agent observability: cloud or local/self-hosted) ---
    langfuse = sub.add_parser(
        "langfuse",
        help="Analyze Langfuse traces for agent loops and token blowouts",
    )
    lf_sub = langfuse.add_subparsers(dest="langfuse_command", required=True)

    lf_sub.add_parser("status", help="Show which Langfuse deployment is configured")

    lf_scan = lf_sub.add_parser("scan", help="Scan recent traces for loops and token outliers")
    lf_scan.add_argument("--since-hours", type=float, default=24.0, help="Lookback window")
    lf_scan.add_argument("--name", default=None, help="Filter by trace name, e.g. ticket_turn")
    lf_scan.add_argument("--tag", action="append", dest="tags", help="Filter by tag (repeatable)")
    lf_scan.add_argument("--min-repeats", type=int, default=3, help="Repeats that count as a loop")
    lf_scan.add_argument("--token-limit", type=int, default=30_000, help="Hard per-trace ceiling")
    lf_scan.add_argument(
        "--annotate",
        action="store_true",
        help="Write findings back to Langfuse as scores/comments (mutates Langfuse only)",
    )
    lf_scan.add_argument("--markdown-out", default=None, help="Write a Markdown report here")
    lf_scan.add_argument("--json", action="store_true", help="Emit JSON instead of tables")

    lf_diag = lf_sub.add_parser("diagnose", help="Show the step-by-step breakdown of one trace")
    lf_diag.add_argument("trace_id")
    lf_diag.add_argument("--min-repeats", type=int, default=3)

    return parser


def _run_langfuse(args) -> int:
    """Handle `cloudrecovery langfuse ...`.

    Imported lazily so the optional `langfuse` extra never affects other
    subcommands, and so a missing/misconfigured SDK reports a clear message
    instead of a traceback.
    """
    import json

    from cloudrecovery.langfuse_analysis import describe_target

    if args.langfuse_command == "status":
        print(json.dumps(describe_target(), indent=2))
        return 0

    from cloudrecovery.langfuse_analysis import build_client, diagnose_trace, scan_traces

    try:
        client = build_client()
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.langfuse_command == "diagnose":
        print(
            json.dumps(
                diagnose_trace(
                    client, trace_id=args.trace_id, min_repeats=args.min_repeats
                ),
                indent=2,
            )
        )
        return 0

    if args.langfuse_command == "scan":
        result = scan_traces(
            client,
            since_hours=args.since_hours,
            name=args.name,
            tags=args.tags,
            min_repeats=args.min_repeats,
            token_limit=args.token_limit,
        )

        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            from cloudrecovery.langfuse_analysis.report import print_summary

            print_summary(result.loop_findings, result.token_findings, result.scanned_traces)

        if args.annotate:
            from cloudrecovery.langfuse_analysis import (
                annotate_loop_finding,
                annotate_token_finding,
            )

            for f in result.loop_findings:
                annotate_loop_finding(client, f)
            for f in result.token_findings:
                annotate_token_finding(client, f)
            print(
                f"Annotated {len(result.loop_findings)} loop finding(s) and "
                f"{len(result.token_findings)} token finding(s) back to Langfuse."
            )

        if args.markdown_out:
            from cloudrecovery.langfuse_analysis.report import to_markdown

            with open(args.markdown_out, "w", encoding="utf-8") as fh:
                fh.write(to_markdown(result.loop_findings, result.token_findings))
            print(f"Markdown report written to {args.markdown_out}")

        # Non-zero exit when something was found, so CI/cron can gate on it.
        return 1 if (result.loop_findings or result.token_findings) else 0

    return 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "ui":
        # env vars consumed by server.py
        os.environ["CLOUDRECOVERY_RUN_CMD"] = args.run_cmd
        os.environ["CLOUDRECOVERY_UI_TITLE"] = args.title

        uvicorn.run(
            "cloudrecovery.server:app",
            host=args.host,
            port=args.port,
            reload=False,
            log_level="info",
        )
        return 0

    if args.subcommand == "mcp":
        # Lazy import so UI mode never breaks due to MCP changes
        from cloudrecovery.mcp.mcp_server import run_stdio_server

        run_stdio_server(command=args.run_cmd)
        return 0

    if args.subcommand == "settings":
        from cloudrecovery.llm.settings import get_settings

        s = get_settings()
        print(s.model_dump_json(indent=2))
        return 0

    if args.subcommand == "langfuse":
        return _run_langfuse(args)

    if args.subcommand == "models":
        from cloudrecovery.llm.model_catalog import list_models_for_provider
        from cloudrecovery.llm.settings import get_settings

        s = get_settings()
        models_list, err = list_models_for_provider(s.provider, s)
        if err:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1
        for m in models_list:
            print(m)
        return 0

    parser.print_help()
    return 2
