from __future__ import annotations

from typing import List

from cloudrecovery.signals.models import Evidence
from cloudrecovery.mcp.host import health

async def collect() -> List[Evidence]:
    h = health()
    sev = "critical" if (h.get("cpu_percent", 0) > 95 or h.get("disk_percent", 0) > 95) else "info"
    return [Evidence(source="agent:host", kind="host_health", severity=sev, message="Host health sample", payload=h)]
