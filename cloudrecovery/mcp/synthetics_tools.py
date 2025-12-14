from __future__ import annotations

from typing import Any, Dict

from cloudrecovery.signals.synthetics import SyntheticsConfig, check_url

async def check(url: str) -> Dict[str, Any]:
    ev = await check_url(SyntheticsConfig(url=url))
    return ev.model_dump()
