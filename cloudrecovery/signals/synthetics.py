from __future__ import annotations

import asyncio
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Dict
from urllib.parse import urlparse

import httpx

from .models import Evidence

@dataclass
class SyntheticsConfig:
    url: str
    timeout_s: float = 5.0

async def check_url(cfg: SyntheticsConfig) -> Evidence:
    """
    Lightweight "site down assistant" primitives:
      - DNS resolve
      - TLS handshake (for https)
      - HTTP GET status/latency
    """
    parsed = urlparse(cfg.url)
    host = parsed.hostname or ""
    scheme = parsed.scheme or "http"
    port = parsed.port or (443 if scheme == "https" else 80)

    payload: Dict[str, object] = {
        "url": cfg.url,
        "dns_ok": False,
        "tls_ok": None,
        "http_ok": False,
        "status_code": None,
        "latency_ms": None,
    }

    # DNS
    try:
        socket.getaddrinfo(host, port)
        payload["dns_ok"] = True
    except Exception as e:
        return Evidence(
            source="synthetics",
            kind="site_check",
            severity="critical",
            message=f"DNS resolution failed for {host}: {e}",
            payload=payload,
        )

    # TLS handshake (optional)
    if scheme == "https":
        payload["tls_ok"] = False
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=cfg.timeout_s) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    payload["tls_ok"] = True
        except Exception as e:
            return Evidence(
                source="synthetics",
                kind="site_check",
                severity="critical",
                message=f"TLS handshake failed for {host}: {e}",
                payload=payload,
            )

    # HTTP
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_s, follow_redirects=True) as client:
            r = await client.get(cfg.url)
        payload["status_code"] = r.status_code
        payload["latency_ms"] = int((time.time() - t0) * 1000)
        if 200 <= r.status_code < 400:
            payload["http_ok"] = True
            return Evidence(source="synthetics", kind="site_check", severity="info",
                            message=f"OK {r.status_code} {cfg.url}", payload=payload)
        sev = "critical" if r.status_code >= 500 else "warning"
        return Evidence(source="synthetics", kind="site_check", severity=sev,
                        message=f"HTTP {r.status_code} for {cfg.url}", payload=payload)
    except Exception as e:
        payload["latency_ms"] = int((time.time() - t0) * 1000)
        return Evidence(
            source="synthetics",
            kind="site_check",
            severity="critical",
            message=f"HTTP request failed for {cfg.url}: {e}",
            payload=payload,
        )

async def periodic_checks(cfg: SyntheticsConfig, emit, interval_s: float = 30.0) -> None:
    """emit(Evidence) callback."""
    while True:
        ev = await check_url(cfg)
        await emit(ev)
        await asyncio.sleep(interval_s)
