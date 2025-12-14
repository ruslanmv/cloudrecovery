from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Literal

Risk = Literal["low", "medium", "high"]

@dataclass
class PolicyDecision:
    allowed: bool
    reason: str = ""
    requires_approval: bool = True
    requires_two_person: bool = False

def validate_action(
    tool: str,
    args: Dict[str, Any],
    *,
    env: str = "prod",
    autopilot_enabled: bool = False,
) -> PolicyDecision:
    """
    Production-safe defaults:
      - Read-only tools are allowed with no approval.
      - Mutating tools require approval in prod.
      - High-risk actions may require two-person approval in prod.
      - In non-prod, autopilot may run selected mutating actions (e.g., rollout_restart).
    """
    mutating = tool in {
        "ocp.rollout_restart",
        "ocp.rollout_undo",
        "ocp.scale_deployment",
        "host.systemd_restart",
    }

    if not mutating:
        return PolicyDecision(True, "read-only tool", requires_approval=False)

    if env == "prod":
        if tool in {"ocp.rollout_undo"}:
            return PolicyDecision(True, "high risk in prod", requires_approval=True, requires_two_person=True)
        return PolicyDecision(True, "mutating action in prod", requires_approval=True)

    if autopilot_enabled and tool in {"ocp.rollout_restart"}:
        return PolicyDecision(True, "autopilot allowed in non-prod", requires_approval=False)

    return PolicyDecision(True, "mutating action in non-prod", requires_approval=True)
