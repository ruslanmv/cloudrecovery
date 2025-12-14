from __future__ import annotations

import asyncio
import os
from typing import List

from cloudrecovery.mcp.tools import ToolRegistry
from cloudrecovery.signals.models import Evidence
from cloudrecovery.signals.synthetics import SyntheticsConfig, periodic_checks

from .config import AgentConfig
from .connection import ControlPlaneClient
from .collectors.host import collect as collect_host
from .collectors.openshift import collect as collect_ocp

async def _flush(client: ControlPlaneClient, buf: List[Evidence]) -> None:
    if not buf:
        return
    payload = [e.model_dump() for e in buf]
    await client.send_evidence(payload)
    buf.clear()

async def main_async(config_path: str) -> None:
    cfg = AgentConfig.load(config_path)
    client = ControlPlaneClient(cfg.control_plane_url, cfg.token, cfg.agent_id)

    # ToolRegistry kept for future agent-execution; monitoring does not require it.
    _ = ToolRegistry(command=os.getenv("CLOUDRECOVERY_AGENT_CMD", "bash"))

    await client.heartbeat({"env": cfg.env, "autopilot_enabled": cfg.autopilot_enabled})

    buf: List[Evidence] = []

    async def emit(ev: Evidence) -> None:
        ev.agent_id = cfg.agent_id
        buf.append(ev)

    if cfg.synthetics_url:
        asyncio.create_task(periodic_checks(SyntheticsConfig(url=cfg.synthetics_url), emit, interval_s=30.0))

    while True:
        try:
            if cfg.host_enabled:
                buf.extend(await collect_host())
        except Exception as e:
            buf.append(Evidence(source="agent:host", kind="collector_error", severity="warning", message=str(e)))

        try:
            if cfg.openshift_enabled:
                buf.extend(await collect_ocp())
        except Exception as e:
            buf.append(Evidence(source="agent:ocp", kind="collector_error", severity="warning", message=str(e)))

        try:
            await _flush(client, buf)
        except Exception as e:
            buf.append(Evidence(source="agent", kind="send_error", severity="warning", message=str(e)))

        await asyncio.sleep(cfg.poll_interval_s)

def main() -> None:
    path = os.getenv("CLOUDRECOVERY_AGENT_CONFIG", "/etc/cloudrecovery/agent.yaml")
    asyncio.run(main_async(path))

if __name__ == "__main__":
    main()
