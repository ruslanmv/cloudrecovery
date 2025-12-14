from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import yaml

@dataclass
class AgentConfig:
    agent_id: str
    control_plane_url: str
    token: str
    env: str = "prod"
    autopilot_enabled: bool = False
    synthetics_url: Optional[str] = None
    poll_interval_s: float = 15.0
    openshift_enabled: bool = True
    host_enabled: bool = True

    @staticmethod
    def load(path: str) -> "AgentConfig":
        data = yaml.safe_load(open(path, "r", encoding="utf-8").read())
        return AgentConfig(**data)
