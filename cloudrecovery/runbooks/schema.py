from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Risk = Literal["low", "medium", "high"]

class Step(BaseModel):
    id: str
    title: str
    risk: Risk = "low"
    type: Literal["cmd", "action"] = "cmd"
    cmd: Optional[str] = None
    tool: Optional[str] = None
    args: Dict[str, Any] = Field(default_factory=dict)
    why: str = ""

class Gate(BaseModel):
    id: str
    title: str
    type: Literal["synthetic_ok", "metric_threshold"] = "synthetic_ok"
    args: Dict[str, Any] = Field(default_factory=dict)

class Runbook(BaseModel):
    name: str
    description: str
    triggers: List[str] = Field(default_factory=list)
    steps: List[Step]
    gates: List[Gate] = Field(default_factory=list)
    rollback_steps: List[Step] = Field(default_factory=list)
