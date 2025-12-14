from __future__ import annotations

from typing import List

from cloudrecovery.signals.models import Evidence
from cloudrecovery.mcp.openshift import get_events, get_pods

def _pod_severity(pod_item: dict) -> str:
    status = pod_item.get("status", {})
    cs = status.get("containerStatuses") or []
    for c in cs:
        st = c.get("state") or {}
        waiting = st.get("waiting")
        if waiting and waiting.get("reason") in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"}:
            return "critical"
    return "info"

async def collect() -> List[Evidence]:
    events = get_events(None)
    pods = get_pods(None)
    out: List[Evidence] = []
    out.append(Evidence(source="agent:ocp", kind="k8s_events", severity="info",
                        message="OpenShift events sample", payload={"count": len(events.get("items", []))}))
    for p in pods.get("items", []):
        sev = _pod_severity(p)
        if sev != "info":
            meta = p.get("metadata", {})
            out.append(Evidence(source="agent:ocp", kind="pod_status", severity=sev,
                                message=f"Pod issue: {meta.get('namespace')}/{meta.get('name')}",
                                payload={"namespace": meta.get("namespace"), "name": meta.get("name"), "status": p.get("status", {})}))
    return out
