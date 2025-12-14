from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from .schema import Runbook, Step

class RunbookExecutor:
    """
    Executes runbook steps. The Control Plane decides whether approval is required.
    This executor is used by both server-side sessions and agents.
    """
    def __init__(
        self,
        run_cmd: Callable[[str], Awaitable[None]],
        run_action: Callable[[str, dict], Awaitable[dict]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._run_cmd = run_cmd
        self._run_action = run_action
        self._sleep = sleep

    async def execute_step(self, step: Step) -> dict:
        if step.type == "cmd":
            if not step.cmd:
                raise ValueError(f"step {step.id} missing cmd")
            await self._run_cmd(step.cmd)
            return {"ok": True}
        if step.type == "action":
            if not step.tool:
                raise ValueError(f"step {step.id} missing tool")
            return await self._run_action(step.tool, step.args)
        raise ValueError(f"unknown step type: {step.type}")

    async def execute(self, runbook: Runbook, on_step: Optional[Callable[[Step], Awaitable[None]]] = None) -> None:
        for step in runbook.steps:
            if on_step:
                await on_step(step)
            await self.execute_step(step)
            await self._sleep(0.1)
