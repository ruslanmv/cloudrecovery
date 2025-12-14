from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

class ControlPlaneClient:
    def __init__(self, base_url: str, token: str, agent_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.agent_id = agent_id

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "X-CloudRecovery-Agent": self.agent_id}

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(min=1, max=10))
    async def heartbeat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{self.base_url}/api/agent/heartbeat", json=payload, headers=self._headers())
            r.raise_for_status()
            return r.json()

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(min=1, max=10))
    async def send_evidence(self, events: list[dict]) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{self.base_url}/api/agent/evidence", json={"events": events}, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def command_stream(self) -> AsyncIterator[Dict[str, Any]]:
        async with httpx.AsyncClient(timeout=20) as c:
            while True:
                r = await c.get(f"{self.base_url}/api/agent/commands", headers=self._headers())
                if r.status_code == 200:
                    data = r.json()
                    for cmd in data.get("commands", []):
                        yield cmd
                await asyncio.sleep(3.0)
