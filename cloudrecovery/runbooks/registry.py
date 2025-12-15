from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import yaml

from .schema import Runbook


def discover_runbooks(packs_dir: Optional[Path] = None) -> List[dict]:
    """
    Discover all runbooks in the packs directory and return them as a list of dicts.

    This function is used by health checks to verify runbook discovery is working.
    """
    registry = RunbookRegistry(packs_dir)
    runbooks = []
    for name in registry.list():
        try:
            runbook = registry.load(name)
            runbooks.append({
                "name": runbook.name if hasattr(runbook, 'name') else name,
                "id": name,
            })
        except Exception:
            # If a runbook fails to load, skip it but continue discovery
            continue
    return runbooks


class RunbookRegistry:
    def __init__(self, packs_dir: Optional[Path] = None) -> None:
        self.packs_dir = packs_dir or (Path(__file__).parent / "packs")

    def list(self) -> List[str]:
        if not self.packs_dir.exists():
            return []
        names = []
        for p in sorted(self.packs_dir.glob("*.yaml")):
            names.append(p.stem)
        for p in sorted(self.packs_dir.glob("*.json")):
            names.append(p.stem)
        return sorted(set(names))

    def load(self, name: str) -> Runbook:
        y = self.packs_dir / f"{name}.yaml"
        j = self.packs_dir / f"{name}.json"
        if y.exists():
            data = yaml.safe_load(y.read_text("utf-8"))
            return Runbook.model_validate(data)
        if j.exists():
            data = json.loads(j.read_text("utf-8"))
            return Runbook.model_validate(data)
        raise FileNotFoundError(f"runbook not found: {name}")
