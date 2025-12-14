from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, Optional

def _run_oc(args: list[str], timeout_s: int = 20) -> str:
    p = subprocess.run(["oc", *args], check=False, capture_output=True, text=True, timeout=timeout_s)
    if p.returncode != 0:
        raise RuntimeError(f"oc failed: {' '.join(args)}\n{p.stderr.strip()}")
    return p.stdout

def list_namespaces() -> Dict[str, Any]:
    out = _run_oc(["get", "ns", "-o", "json"])
    return json.loads(out)

def get_pods(namespace: Optional[str] = None) -> Dict[str, Any]:
    args = ["get", "pods"]
    if namespace:
        args += ["-n", namespace]
    else:
        args += ["-A"]
    args += ["-o", "json"]
    out = _run_oc(args)
    return json.loads(out)

def get_events(namespace: Optional[str] = None) -> Dict[str, Any]:
    args = ["get", "events"]
    if namespace:
        args += ["-n", namespace]
    else:
        args += ["-A"]
    args += ["-o", "json"]
    out = _run_oc(args)
    return json.loads(out)

def rollout_status(namespace: str, deployment: str) -> Dict[str, Any]:
    out = _run_oc(["rollout", "status", f"deploy/{deployment}", "-n", namespace, "--timeout=20s"])
    return {"output": out}

def rollout_restart(namespace: str, deployment: str) -> Dict[str, Any]:
    out = _run_oc(["rollout", "restart", f"deploy/{deployment}", "-n", namespace])
    return {"output": out}

def rollout_undo(namespace: str, deployment: str) -> Dict[str, Any]:
    out = _run_oc(["rollout", "undo", f"deploy/{deployment}", "-n", namespace])
    return {"output": out}

def scale_deployment(namespace: str, deployment: str, replicas: int) -> Dict[str, Any]:
    out = _run_oc(["scale", f"deploy/{deployment}", "-n", namespace, f"--replicas={replicas}"])
    return {"output": out}
