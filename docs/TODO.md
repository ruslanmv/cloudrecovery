# TODO — Finish the Langfuse + Sources work

Handoff notes for the next session. Branch: `claude/langfuse-cloudrecovery-integration-p49ur4`
(2 commits, 86 tests passing, ruff clean on all new files).

## What already works

- **Langfuse analysis** (`cloudrecovery/langfuse_analysis/`) — loop and token-outlier
  detection, read client, score annotation, `LoopGuard`, reporting. Vendored into this
  package; there is no dependency on an external `langfuse-recovery` distribution.
- **Tools** — `langfuse.status` / `.scan` / `.diagnose` / `.annotate`, registered in
  `ToolRegistry`, discoverable via `list_tools()`, gated by `action_policy`.
- **Collector** — `signals/langfuse_signals.py` emits `Evidence(source="agent:langfuse")`;
  `scripts/langfuse_agent_poller.py` pushes it to `POST /api/agent/evidence`.
- **CLI** — `cloudrecovery langfuse status|scan|diagnose`.
- **Sources of Truth** (`cloudrecovery/sources/`) — attach GitHub / GitLab / local /
  GitOps sources, read-only sync, path+symbol index, deterministic correlation,
  three-tier context, text-only grounded proposals. `/api/sources`, `/api/sources/{id}/sync`,
  `/api/sources/ground`, plus the **Sources** tab in the web UI.
- **Packaging** — optional extra `pip install "cloudrecovery[langfuse]"`; Langfuse works
  as cloud **or** local/self-hosted (`LANGFUSE_HOST` optional, defaults to cloud).

## What is left to do

### 1. Evidence → runbook auto-matching (highest value)

`cloudrecovery/incident_detector.py` is still a placeholder that returns an empty
hypothesis. Nothing matches an incoming `Evidence.kind` to a runbook's `triggers`, so
`langfuse_agent_loop.yaml` has to be picked by hand.

- Implement matching: `Evidence.kind` → `RunbookRegistry` packs whose `triggers` contain
  that kind. `RunbookRegistry.load()` already parses `triggers`.
- Surface the suggestion in the UI (the runbook name plus why it matched), keeping the
  existing Plan → Approve → Execute gate. Do not auto-execute.
- Tests to add: `langfuse_loop` matches `langfuse_agent_loop`; an unknown kind matches
  nothing; a kind matching two packs returns both, ranked.

### 2. Runbook executor: gates and rollback

`runbooks/executor.py` runs `steps` in order but never evaluates `gates` or
`rollback_steps`, even though the schema parses them and `crashloopbackoff_openshift.yaml`
defines them. Either wire them up or document the field as inert — right now a reader of
the YAML would reasonably assume rollback fires on failure, and it does not.

### 3. Real-time loop prevention is out of process (accepted boundary)

The Langfuse pipeline is reactive observation: it records that a loop happened and can
annotate the trace, but it cannot stop one mid-call. Only `LoopGuard`, embedded in the
agent process, can do that:

```python
from cloudrecovery.langfuse_analysis.guard import LoopGuard, RunawayLoopError
```

Optional follow-up: if agent-core exposes a pause/kill control endpoint, add a mutating
tool for it plus an `action_policy` entry, scoped to that specific control surface. That
is a new tool, not a change to the collector.

### 4. Sources engine — phases 2-4

Phase 1 is done. Remaining, in the order the design proposed:

- **Embedding search** for repos too large for symbol/path matching
  (`sources/index.py`, `sources/correlate.py`). Current matching is fine into the
  thousands of files, not millions.
- **Auto-suggested service → repo mapping** so links don't have to be typed by hand.
  Keep exact matching for *selection* — a wrong link produces confidently wrong citations.
- **Live ArgoCD drift diff** — GitOps sources attach and index today, but nothing compares
  live pod spec against Git HEAD. This is the piece that turns "CrashLoopBackOff" into
  "commit abc123 lowered the memory limit".
- **Draft-PR handoff** for the app-code fix path. Requires a write-scoped token; keep it
  least-privilege and behind the existing approval gate. Nothing writes to a repo today.
- **Closed-loop feedback** — annotate the outcome and track whether a fix actually stopped
  the loop from recurring.

### 5. Loose ends

- `tab-summary` and `tab-issues` buttons exist in the UI with no matching panels
  (pre-existing, unrelated to this work): clicking them shows an empty pane.
- `MANIFEST.in` still references a stale `clouddeploy/web` path.
- Repo-wide `ruff check .` reports ~350 pre-existing findings (mostly `typing.Dict` →
  `dict`). New files are clean; a separate sweep would fix the rest.
- The architecture diagram for these two subsystems is written but not published.

## How to verify before you start

```bash
pip install -e . && pip install pytest ruff
pytest tests/ -q                          # expect 86 passed
ruff check cloudrecovery/langfuse_analysis/ cloudrecovery/sources/ tests/

# no dependency on the external package — expect no output
grep -rn "langfuse_recovery" --include="*.py" --include="*.toml" .
```

## Useful entry points

| Area | File |
|---|---|
| Loop / token detection | `cloudrecovery/langfuse_analysis/loops.py`, `tokens.py` |
| Langfuse read + annotate | `cloudrecovery/langfuse_analysis/client.py`, `annotate.py` |
| Tool registration | `cloudrecovery/mcp/tools.py` (`_register_cloudrecovery_tools`) |
| Policy gate | `cloudrecovery/mcp/action_policy.py` |
| Evidence contract | `cloudrecovery/signals/models.py` |
| Incident → source join | `cloudrecovery/sources/registry.py` (`find_for_identity`) |
| Correlation ranking | `cloudrecovery/sources/correlate.py` |
| Context tiers + redaction | `cloudrecovery/sources/context.py` |
| API routes | `cloudrecovery/server.py` (end of file) |
| Sources UI | `cloudrecovery/web/index.html`, `app.js` |
