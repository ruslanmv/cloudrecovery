from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["info", "warning", "critical"]

class Evidence(BaseModel):
    """Normalized evidence event flowing into the Control Plane and UI."""
    ts: datetime = Field(default_factory=lambda: datetime.utcnow())
    source: str                    # e.g. "agent:ocp", "agent:host", "synthetics"
    kind: str                      # e.g. "alert", "k8s_event", "pod_status", "host_health"
    severity: Severity = "info"
    message: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    incident_id: Optional[str] = None
    agent_id: Optional[str] = None
