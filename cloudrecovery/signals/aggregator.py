from __future__ import annotations

from collections import deque
from typing import Deque, List

from .models import Evidence

class EvidenceBuffer:
    """In-memory ring buffer. Production deployments can swap to Redis/Postgres later."""
    def __init__(self, maxlen: int = 5000) -> None:
        self._buf: Deque[Evidence] = deque(maxlen=maxlen)

    def add(self, ev: Evidence) -> None:
        self._buf.append(ev)

    def tail(self, limit: int = 200) -> List[Evidence]:
        if limit <= 0:
            return []
        return list(self._buf)[-limit:]
