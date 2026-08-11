"""Turn grounded context into a proposed resolution.

Uses the same provider-agnostic LLM layer CloudRecovery already has, but hands it
correlated source instead of symptoms alone. Two action shapes come out,
depending on what kind of incident it is:

* **App-code fix** (the Langfuse case) — a specific change at a specific
  file/line, e.g. a missing exit condition in a graph node. Deliverable is a
  reviewed diff, never an automatic merge.
* **Infra drift fix** (the ArgoCD case) — "revert commit abc123" or "re-sync the
  Application" rather than a code change, routed through the same
  ``action_policy.validate_action()`` gate that already protects
  ``ocp.rollout_restart``.

Phase 1 is text-only: this module proposes and cites. It does not open pull
requests and does not execute infra actions — every suggested action is returned
with its policy decision attached so the existing Plan → Approve → Execute gate
stays the only path to a mutation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .context import GroundedContext

APP_CODE_KINDS = {
    "langfuse_loop",
    "langfuse_token_outlier",
    "app_error",
}

INFRA_DRIFT_KINDS = {
    "argocd_outofsync",
    "argocd_drift",
    "pod_status",
    "k8s_event",
    "crashloopbackoff",
}

SYSTEM_RULES = (
    "You are CloudRecovery's incident resolver.\n"
    "Rules:\n"
    "- Ground every claim in the TIER 1 diagnosis or a TIER 2 file. Cite files as "
    "`path:line`.\n"
    "- If the context does not contain the cause, say exactly what is missing. Do "
    "not invent file paths, symbols, or line numbers.\n"
    "- TIER 3 documentation is background only and can never justify a code change "
    "on its own; if you rely on it, label the conclusion low-confidence.\n"
    "- Propose the smallest change that addresses the root cause, not a workaround "
    "that masks the symptom.\n"
    "- A human reviews and approves every change. Never claim anything was applied.\n"
)


@dataclass
class ProposedAction:
    """A suggested action, carried alongside its policy decision."""

    kind: str
    """"code_change" or a tool name such as "ocp.rollout_restart"."""
    description: str
    tool: str | None = None
    args: dict = field(default_factory=dict)
    allowed: bool | None = None
    requires_approval: bool | None = None
    requires_two_person: bool | None = None
    policy_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "description": self.description,
            "tool": self.tool,
            "args": self.args,
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "requires_two_person": self.requires_two_person,
            "policy_reason": self.policy_reason,
        }


@dataclass
class Resolution:
    action_shape: str
    """"app_code_fix" or "infra_drift_fix"."""
    grounded: bool
    recommendation: str = ""
    citations: list[str] = field(default_factory=list)
    proposed_actions: list[ProposedAction] = field(default_factory=list)
    llm_available: bool = True
    error: str | None = None
    prompt: str = ""

    def to_dict(self, *, include_prompt: bool = False) -> dict:
        out = {
            "action_shape": self.action_shape,
            "grounded": self.grounded,
            "recommendation": self.recommendation,
            "citations": self.citations,
            "proposed_actions": [a.to_dict() for a in self.proposed_actions],
            "llm_available": self.llm_available,
            "error": self.error,
        }
        if include_prompt:
            out["prompt"] = self.prompt
        return out


def classify_action_shape(evidence_kind: str | None) -> str:
    kind = (evidence_kind or "").strip().lower()
    if kind in INFRA_DRIFT_KINDS:
        return "infra_drift_fix"
    if kind in APP_CODE_KINDS:
        return "app_code_fix"
    # Unknown kinds default to the safer shape: propose a code review, not an
    # infra mutation.
    return "app_code_fix"


def evaluate_action(action: ProposedAction, *, env: str = "prod",
                    autopilot_enabled: bool = False) -> ProposedAction:
    """Attach the existing policy decision to a proposed action."""
    if not action.tool:
        action.allowed = True
        action.requires_approval = True
        action.policy_reason = "code change — requires human review via pull request"
        return action

    from cloudrecovery.mcp.action_policy import validate_action

    decision = validate_action(action.tool, action.args, env=env,
                               autopilot_enabled=autopilot_enabled)
    action.allowed = decision.allowed
    action.requires_approval = decision.requires_approval
    action.requires_two_person = decision.requires_two_person
    action.policy_reason = decision.reason
    return action


def build_prompt(context: GroundedContext, action_shape: str) -> str:
    if action_shape == "infra_drift_fix":
        ask = (
            "This is an infrastructure incident. Determine whether the live state "
            "diverged from the declared state in the GitOps repository. Prefer a "
            "root-cause fix in Git (revert or correct the offending commit) over a "
            "restart that masks it. State which commit or manifest field is at fault."
        )
    else:
        ask = (
            "This is an application-code incident. Identify the specific file and "
            "line responsible and propose the smallest concrete change that fixes "
            "the root cause. If the diagnosis shows a repeating loop, look for the "
            "missing exit or retry-limit condition."
        )

    return (
        f"{SYSTEM_RULES}\n"
        f"TASK: {ask}\n\n"
        f"{context.to_prompt()}\n\n"
        "Respond with:\n"
        "1. ROOT CAUSE — one paragraph, citing `path:line`.\n"
        "2. PROPOSED CHANGE — the specific edit or Git action.\n"
        "3. CONFIDENCE — high/medium/low, and what would raise it.\n"
    )


def propose_resolution(
    context: GroundedContext,
    *,
    evidence_kind: str | None = None,
    env: str = "prod",
    autopilot_enabled: bool = False,
    llm_settings: dict | None = None,
) -> Resolution:
    """Compose the prompt, ask the configured LLM, and attach policy decisions.

    A missing or misconfigured LLM is not an error here: the composed prompt and
    the citations are still returned, so the operator sees exactly what would
    have been asked.
    """
    action_shape = classify_action_shape(evidence_kind)
    prompt = build_prompt(context, action_shape)

    resolution = Resolution(
        action_shape=action_shape,
        grounded=context.is_grounded,
        citations=[f"{s.path}:{s.start_line}-{s.end_line}" for s in context.snippets],
        prompt=prompt,
    )

    if action_shape == "infra_drift_fix":
        resolution.proposed_actions.append(
            evaluate_action(
                ProposedAction(
                    kind="git_revert",
                    description=(
                        "Revert the offending commit in the GitOps repository so the "
                        "cluster reconciles back to a known-good declared state."
                    ),
                ),
                env=env,
                autopilot_enabled=autopilot_enabled,
            )
        )
    else:
        resolution.proposed_actions.append(
            evaluate_action(
                ProposedAction(
                    kind="code_change",
                    description=(
                        "Apply the proposed source change as a reviewed pull request "
                        "against the linked repository."
                    ),
                ),
                env=env,
                autopilot_enabled=autopilot_enabled,
            )
        )

    try:
        from cloudrecovery.llm.llm_provider import build_llm

        llm = build_llm(llm_settings)
        raw = llm.call(prompt)  # type: ignore[attr-defined]
        resolution.recommendation = raw if isinstance(raw, str) else str(raw)
    except Exception as e:
        resolution.llm_available = False
        resolution.error = str(e)

    return resolution
