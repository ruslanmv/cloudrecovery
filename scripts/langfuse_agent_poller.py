"""Standalone Langfuse polling agent for CloudRecovery.

Why this exists as a separate script rather than code inside server.py:
CloudRecovery's control plane never actually starts any background
collector today — not even for the synthetics checker that already ships
in signals/synthetics.py. The only way evidence gets into the running
server is via POST /api/agent/evidence (the same endpoint the documented
"Linux Agent" daemon pushes to). So this script follows that existing
push model exactly, rather than inventing a second way for evidence to
enter the system.

Run this next to your agent-core (or anywhere with network access to both
your Langfuse instance and the CloudRecovery control plane):

    pip install "cloudrecovery[langfuse]"
    export LANGFUSE_PUBLIC_KEY=...
    export LANGFUSE_SECRET_KEY=...
    # LANGFUSE_HOST is only needed for a local / self-hosted Langfuse;
    # omit it to use Langfuse Cloud.
    export LANGFUSE_HOST=https://langfuse.your-domain.com
    export CLOUDRECOVERY_CONTROL_PLANE=http://127.0.0.1:8787
    python scripts/langfuse_agent_poller.py --once   # one poll, for testing
    python scripts/langfuse_agent_poller.py          # runs forever, polls every 60s
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

try:
    from cloudrecovery.signals.langfuse_signals import LangfuseCollectorConfig, check_langfuse
except ModuleNotFoundError:  # running from a checkout without `pip install -e .`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from cloudrecovery.signals.langfuse_signals import LangfuseCollectorConfig, check_langfuse


async def push_once(control_plane: str, cfg: LangfuseCollectorConfig) -> int:
    evidence = await check_langfuse(cfg)
    if not evidence:
        return 0
    payload = {"events": [e.model_dump(mode="json") for e in evidence]}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{control_plane}/api/agent/evidence", json=payload)
        resp.raise_for_status()
        received = resp.json().get("received", 0)
        print(f"pushed {len(evidence)} evidence item(s), server received {received}")
        return received


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Poll once and exit (for testing).")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--since-hours", type=float, default=1.0)
    parser.add_argument("--name", default=None, help="Filter to a specific trace name, e.g. ticket_turn")
    args = parser.parse_args()

    control_plane = os.environ.get("CLOUDRECOVERY_CONTROL_PLANE", "http://127.0.0.1:8787")
    cfg = LangfuseCollectorConfig(since_hours=args.since_hours, name_filter=args.name)

    if args.once:
        await push_once(control_plane, cfg)
        return

    while True:
        try:
            await push_once(control_plane, cfg)
        except Exception as e:
            print(f"poll failed: {e}")
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
