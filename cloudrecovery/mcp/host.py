from __future__ import annotations

import subprocess
from typing import Any, Dict

import psutil

def health() -> Dict[str, Any]:
    disk = psutil.disk_usage("/")
    mem = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=0.2)
    return {
        "cpu_percent": cpu,
        "mem_percent": mem.percent,
        "disk_percent": disk.percent,
        "loadavg": list(psutil.getloadavg()) if hasattr(psutil, "getloadavg") else None,
    }

def systemd_status(service: str) -> Dict[str, Any]:
    p = subprocess.run(["systemctl", "is-active", service], capture_output=True, text=True)
    return {"service": service, "active": p.stdout.strip(), "rc": p.returncode}

def systemd_restart(service: str) -> Dict[str, Any]:
    p = subprocess.run(["systemctl", "restart", service], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or f"failed to restart {service}")
    return {"service": service, "ok": True}
