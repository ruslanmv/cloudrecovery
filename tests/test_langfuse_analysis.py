"""Tests for the Langfuse analysis feature.

Covers the analyzers (pure logic), the SDK wrapper against fake client shapes,
the Evidence contract of the signal collector, the policy classification of the
langfuse.* tools, the runbook pack, and ToolRegistry wiring.

Nothing here touches the network: the Langfuse client is faked, so these run
identically whether or not the optional `langfuse` extra is installed.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from cloudrecovery.langfuse_analysis import scan_traces
from cloudrecovery.langfuse_analysis.client import (
    describe_target,
    get_observations_for_trace,
    iter_traces,
    observation_token_total,
)
from cloudrecovery.langfuse_analysis.guard import LoopGuard, RunawayLoopError
from cloudrecovery.langfuse_analysis.loops import detect_cycles
from cloudrecovery.langfuse_analysis.tokens import flag_token_outliers

LOOP_NAMES = [
    "ticket_turn", "intake_gate", "injection_check", "extract", "classify",
    "ticket_turn", "intake_gate", "injection_check", "extract", "classify",
    "ticket_turn", "intake_gate", "injection_check", "extract", "classify",
]


# ---------------------------------------------------------------------------
# Analyzers
# ---------------------------------------------------------------------------

def test_detects_repeating_agent_loop():
    tokens = [1000] * len(LOOP_NAMES)

    findings = detect_cycles(LOOP_NAMES, tokens, trace_id="test-trace", min_repeats=3)

    assert findings, "expected at least one loop finding"
    top = findings[0]
    assert top.repeat_count == 3
    assert top.cycle == (
        "ticket_turn", "intake_gate", "injection_check", "extract", "classify",
    )
    assert top.total_tokens == sum(tokens)
    assert top.cycle_str.startswith("ticket_turn -> intake_gate")


def test_no_false_positive_on_non_looping_trace():
    names = ["ticket_turn", "intake_gate", "injection_check", "extract", "classify", "respond"]
    assert detect_cycles(names, [500] * len(names), "healthy", min_repeats=3) == []


def test_ignores_repeats_below_threshold():
    names = ["ticket_turn", "intake_gate"] * 2  # 2 repeats, threshold is 3
    assert detect_cycles(names, [500] * len(names), "borderline", min_repeats=3) == []


def test_detects_single_node_self_loop():
    names = ["retry"] * 5
    findings = detect_cycles(names, [10] * 5, "self-loop", min_repeats=3)
    assert findings and findings[0].cycle == ("retry",)
    assert findings[0].repeat_count == 5


def test_detect_cycles_tolerates_missing_tokens():
    """tokens shorter than names must not raise — totals stay best-effort."""
    findings = detect_cycles(["a", "b"] * 3, [], "no-tokens", min_repeats=3)
    assert findings and findings[0].total_tokens == 0


def test_detect_cycles_empty_input():
    assert detect_cycles([], [], "empty") == []


def test_token_outlier_hard_limit_fires_without_history():
    findings = flag_token_outliers({"t1": 60_000}, hard_limit=30_000)
    assert len(findings) == 1
    assert findings[0].hard_limit_breached is True
    assert findings[0].total_tokens == 60_000


def test_token_outlier_ignores_normal_traces():
    totals = {f"t{i}": 1_000 for i in range(10)}
    assert flag_token_outliers(totals, hard_limit=30_000) == []


def test_token_outlier_flags_statistical_outlier_under_hard_limit():
    totals = {f"t{i}": 1_000 for i in range(20)}
    totals["spike"] = 20_000
    findings = flag_token_outliers(totals, hard_limit=100_000)
    assert [f.trace_id for f in findings] == ["spike"]
    assert findings[0].hard_limit_breached is False
    assert findings[0].z_score > 3.0


def test_token_outlier_hard_limit_disabled():
    assert flag_token_outliers({"t1": 999_999}, hard_limit=0) == []


# ---------------------------------------------------------------------------
# Fake Langfuse SDK shapes
# ---------------------------------------------------------------------------

class FakeObservation:
    def __init__(self, name, tokens=None, start_time=None, usage_details=None, type_="SPAN"):
        self.name = name
        self.type = type_
        self.start_time = start_time
        if usage_details is not None:
            self.usage_details = usage_details
        elif tokens is not None:
            self.usage_details = {"input": tokens // 2, "output": tokens - tokens // 2}


class FakeTrace:
    def __init__(self, trace_id):
        self.id = trace_id


class FakeMeta:
    def __init__(self, page=1, total_pages=1, cursor=None):
        self.page = page
        self.total_pages = total_pages
        self.cursor = cursor


class FakePage:
    def __init__(self, data, meta=None):
        self.data = data
        self.meta = meta or FakeMeta()


class FakeTraceApi:
    """Mimics langfuse>=3 `client.api.trace`: page-based, from_timestamp kwargs."""

    def __init__(self, traces, page_size=50):
        self.traces = traces
        self.page_size = page_size
        self.calls = []

    def list(self, **kwargs):
        if "cursor" in kwargs or "start_time" in kwargs:
            # The real generated client rejects these; so must the fake.
            raise TypeError("unexpected keyword argument")
        self.calls.append(kwargs)
        page = kwargs.get("page", 1)
        limit = kwargs.get("limit", self.page_size)
        chunk = self.traces[(page - 1) * limit : page * limit]
        total_pages = max(1, (len(self.traces) + limit - 1) // limit)
        return FakePage(chunk, FakeMeta(page=page, total_pages=total_pages))


class FakeObservationsApi:
    """Mimics `client.api.observations`: cursor-based pagination."""

    def __init__(self, by_trace, page_size=100):
        self.by_trace = by_trace
        self.page_size = page_size

    def get_many(self, **kwargs):
        observations = self.by_trace.get(kwargs["trace_id"], [])
        offset = int(kwargs.get("cursor") or 0)
        limit = kwargs.get("limit", self.page_size)
        chunk = observations[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(observations) else None
        return FakePage(chunk, FakeMeta(cursor=next_cursor))


class FakeApi:
    def __init__(self, traces, observations_by_trace):
        self.trace = FakeTraceApi(traces)
        self.observations = FakeObservationsApi(observations_by_trace)


class FakeClient:
    def __init__(self, traces=(), observations_by_trace=None):
        self.api = FakeApi(list(traces), observations_by_trace or {})
        self.scores = []
        self.flushed = 0

    def create_score(self, **kwargs):
        self.scores.append(kwargs)

    def flush(self):
        self.flushed += 1


# ---------------------------------------------------------------------------
# Client wrapper
# ---------------------------------------------------------------------------

def test_observation_token_total_across_sdk_shapes():
    assert observation_token_total(FakeObservation("a", usage_details={"total": 512})) == 512
    assert observation_token_total(FakeObservation("b", usage_details={"input": 10, "output": 5})) == 15
    assert observation_token_total(
        FakeObservation("c", usage_details={"promptTokens": 7, "completionTokens": 3})
    ) == 10
    assert observation_token_total(FakeObservation("d", usage_details={})) == 0
    assert observation_token_total(FakeObservation("e")) == 0


def test_observation_token_total_prefers_total_over_summing():
    obs = FakeObservation("f", usage_details={"input": 10, "output": 5, "total": 15})
    assert observation_token_total(obs) == 15


def test_iter_traces_paginates_by_page_not_cursor():
    client = FakeClient(traces=[FakeTrace(f"t{i}") for i in range(120)])
    seen = list(iter_traces(client, since_hours=1, limit_per_page=50))

    assert [t.id for t in seen] == [f"t{i}" for i in range(120)]
    # Timestamp filter must be passed on the wire, not silently dropped.
    assert "from_timestamp" in client.api.trace.calls[0]
    assert [c["page"] for c in client.api.trace.calls] == [1, 2, 3]


def test_iter_traces_respects_max_traces():
    client = FakeClient(traces=[FakeTrace(f"t{i}") for i in range(500)])
    assert len(list(iter_traces(client, since_hours=1, limit_per_page=50, max_traces=60))) == 60


def test_get_observations_follows_cursor_and_sorts_by_start_time():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        FakeObservation(f"step{i}", tokens=100, start_time=base + timedelta(seconds=250 - i))
        for i in range(250)
    ]
    client = FakeClient(observations_by_trace={"t1": observations})

    result = get_observations_for_trace(client, "t1", limit_per_page=100)

    assert len(result) == 250, "cursor pagination must fetch every page"
    times = [o.start_time for o in result]
    assert times == sorted(times), "observations must come back in execution order"


def test_get_observations_tolerates_missing_start_time():
    client = FakeClient(
        observations_by_trace={"t1": [FakeObservation("a"), FakeObservation("b")]}
    )
    # A None start_time must not raise when sorting.
    assert len(get_observations_for_trace(client, "t1")) == 2


def test_describe_target_reports_cloud_by_default(monkeypatch):
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")

    target = describe_target()
    assert target["deployment"] == "cloud"
    assert target["credentials_present"] is True


def test_describe_target_reports_local_when_host_set(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
    assert describe_target()["deployment"] == "local"


# ---------------------------------------------------------------------------
# scan_traces / annotate
# ---------------------------------------------------------------------------

def _looping_client():
    return FakeClient(
        traces=[FakeTrace("loop-trace")],
        observations_by_trace={
            "loop-trace": [FakeObservation(n, tokens=1000) for n in LOOP_NAMES]
        },
    )


def test_scan_traces_finds_loop_end_to_end():
    result = scan_traces(_looping_client(), since_hours=1, token_limit=30_000)

    assert result.scanned_traces == 1
    assert len(result.loop_findings) == 1
    assert result.loop_findings[0].repeat_count == 3
    assert result.trace_token_totals["loop-trace"] == 15_000

    payload = result.to_dict()
    assert payload["loop_findings"][0]["cycle"][0] == "ticket_turn"
    assert payload["loop_findings"][0]["tokens_in_loop"] == 15_000


def test_annotate_uses_create_score_and_flushes():
    from cloudrecovery.langfuse_analysis.annotate import annotate_loop_finding

    client = _looping_client()
    finding = scan_traces(client, since_hours=1).loop_findings[0]

    method = annotate_loop_finding(client, finding)

    assert method == "create_score"
    assert client.scores[0]["trace_id"] == "loop-trace"
    assert client.scores[0]["name"] == "cloudrecovery_loop_detected"
    assert "Repeating cycle detected" in client.scores[0]["comment"]
    assert client.flushed == 1, "scores are queued; a short-lived caller must flush"


def test_annotate_falls_back_to_legacy_score_method():
    from cloudrecovery.langfuse_analysis.annotate import write_score

    class LegacyClient:
        def __init__(self):
            self.scores = []

        def score(self, **kwargs):
            self.scores.append(kwargs)

    client = LegacyClient()
    assert write_score(client, trace_id="t1", name="n", value=1) == "score"
    assert client.scores[0]["trace_id"] == "t1"


# ---------------------------------------------------------------------------
# LoopGuard (in-process circuit breaker)
# ---------------------------------------------------------------------------

def test_loop_guard_trips_on_consecutive_repeats():
    guard = LoopGuard(max_consecutive_repeats=3, max_total_tokens=None)
    guard.step("plan")
    guard.step("plan")
    with pytest.raises(RunawayLoopError, match="repeated 3x in a row"):
        guard.step("plan")


def test_loop_guard_trips_on_token_ceiling():
    guard = LoopGuard(max_total_tokens=1_000)
    with pytest.raises(RunawayLoopError, match="token ceiling"):
        guard.step("plan", tokens_used_so_far=1_000)


def test_loop_guard_trips_on_cyclic_pattern_and_calls_on_trip():
    reasons = []
    guard = LoopGuard(max_consecutive_repeats=99, max_total_tokens=None, on_trip=reasons.append)
    with pytest.raises(RunawayLoopError):
        for node in ["a", "b"] * 3:
            guard.step(node)
    assert reasons and "cyclic loop detected" in reasons[0]


def test_loop_guard_allows_healthy_run():
    guard = LoopGuard(max_total_tokens=10_000)
    for node in ["intake", "extract", "classify", "respond"]:
        guard.step(node, tokens_used_so_far=100)
    assert guard.history == ["intake", "extract", "classify", "respond"]


# ---------------------------------------------------------------------------
# Signal collector: Evidence contract
# ---------------------------------------------------------------------------

def test_collector_emits_critical_evidence_for_loop(monkeypatch):
    from cloudrecovery.signals import langfuse_signals

    monkeypatch.setattr(
        "cloudrecovery.langfuse_analysis.build_client", lambda: _looping_client()
    )

    cfg = langfuse_signals.LangfuseCollectorConfig(since_hours=1)
    evidence = asyncio.run(langfuse_signals.check_langfuse(cfg))

    loops = [e for e in evidence if e.kind == "langfuse_loop"]
    assert len(loops) == 1
    ev = loops[0]
    assert ev.source == "agent:langfuse"
    assert ev.severity == "critical"
    assert ev.payload["trace_id"] == "loop-trace"
    assert ev.payload["repeat_count"] == 3
    assert ev.incident_id == "langfuse-loop-loop-trace"
    # Must serialize for POST /api/agent/evidence.
    assert ev.model_dump(mode="json")["kind"] == "langfuse_loop"


def test_collector_emits_info_evidence_when_clean(monkeypatch):
    from cloudrecovery.signals import langfuse_signals

    monkeypatch.setattr(
        "cloudrecovery.langfuse_analysis.build_client",
        lambda: FakeClient(traces=[], observations_by_trace={}),
    )

    evidence = asyncio.run(
        langfuse_signals.check_langfuse(langfuse_signals.LangfuseCollectorConfig())
    )

    assert len(evidence) == 1
    assert evidence[0].kind == "langfuse_scan"
    assert evidence[0].severity == "info"


def test_collector_degrades_to_warning_when_unconfigured(monkeypatch):
    from cloudrecovery.signals import langfuse_signals

    def _boom():
        raise OSError("Missing required environment variables: LANGFUSE_PUBLIC_KEY")

    monkeypatch.setattr("cloudrecovery.langfuse_analysis.build_client", _boom)

    evidence = asyncio.run(
        langfuse_signals.check_langfuse(langfuse_signals.LangfuseCollectorConfig())
    )

    assert len(evidence) == 1
    assert evidence[0].kind == "collector_error"
    assert evidence[0].severity == "warning"


# ---------------------------------------------------------------------------
# Policy classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["langfuse.scan", "langfuse.diagnose", "langfuse.status"])
def test_langfuse_read_only_tools_need_no_approval(tool):
    from cloudrecovery.mcp.action_policy import validate_action

    decision = validate_action(tool, {}, env="prod")
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_langfuse_annotate_is_mutating_but_needs_no_approval_in_prod():
    from cloudrecovery.mcp.action_policy import validate_action

    decision = validate_action("langfuse.annotate", {}, env="prod")
    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.requires_two_person is False


def test_prod_mutating_tools_still_gated():
    """The langfuse exemption must not have loosened the existing gates."""
    from cloudrecovery.mcp.action_policy import validate_action

    assert validate_action("ocp.rollout_restart", {}, env="prod").requires_approval is True
    assert validate_action("ocp.rollout_undo", {}, env="prod").requires_two_person is True
    assert validate_action("host.systemd_restart", {}, env="prod").requires_approval is True


# ---------------------------------------------------------------------------
# Runbook pack + ToolRegistry wiring
# ---------------------------------------------------------------------------

def test_langfuse_runbook_pack_loads_and_validates():
    from cloudrecovery.runbooks.registry import RunbookRegistry

    registry = RunbookRegistry()
    assert "langfuse_agent_loop" in registry.list()

    runbook = registry.load("langfuse_agent_loop")
    assert runbook.triggers == ["langfuse_loop"]
    assert [s.tool for s in runbook.steps] == [
        "langfuse.scan",
        "langfuse.diagnose",
        "langfuse.annotate",
    ]
    assert all(s.type == "action" for s in runbook.steps)


def test_runbook_trigger_matches_collector_evidence_kind():
    """The pack's trigger must equal the `kind` the collector actually emits."""
    from cloudrecovery.runbooks.registry import RunbookRegistry

    triggers = RunbookRegistry().load("langfuse_agent_loop").triggers
    assert "langfuse_loop" in triggers


def test_existing_runbook_packs_still_load():
    from cloudrecovery.runbooks.registry import RunbookRegistry

    names = RunbookRegistry().list()
    assert "crashloopbackoff_openshift" in names
    assert "site_down_basic" in names


def test_tool_registry_registers_and_advertises_langfuse_tools():
    from cloudrecovery.mcp.tools import ToolRegistry

    registry = ToolRegistry(command="/bin/true")

    for name in ("langfuse.scan", "langfuse.diagnose", "langfuse.annotate", "langfuse.status"):
        assert name in registry.tools

    advertised = {t["name"] for t in registry.list_tools()}
    assert {"langfuse.scan", "langfuse.diagnose", "langfuse.annotate"} <= advertised
    # The pre-existing PTY tools must still be advertised.
    assert {"session.start", "cli.read", "policy.describe"} <= advertised


def test_tool_registry_dispatches_async_tool_from_sync_context(monkeypatch):
    from cloudrecovery.mcp.tools import ToolRegistry

    monkeypatch.setattr(
        "cloudrecovery.langfuse_analysis.build_client", lambda: _looping_client()
    )

    registry = ToolRegistry(command="/bin/true")
    result = registry.call("langfuse.scan", {"since_hours": 1})

    assert result["scanned_traces"] == 1
    assert result["loop_findings"][0]["repeat_count"] == 3


def test_tool_registry_dispatches_async_tool_from_running_event_loop(monkeypatch):
    """server.py calls tools.call() inside async handlers — must not deadlock."""
    from cloudrecovery.mcp.tools import ToolRegistry

    monkeypatch.setattr(
        "cloudrecovery.langfuse_analysis.build_client", lambda: _looping_client()
    )

    registry = ToolRegistry(command="/bin/true")

    async def call_from_loop():
        return registry.call("langfuse.scan", {"since_hours": 1})

    assert asyncio.run(call_from_loop())["scanned_traces"] == 1


def test_unknown_tool_still_raises():
    from cloudrecovery.mcp.tools import ToolRegistry

    with pytest.raises(ValueError, match="Unknown tool"):
        ToolRegistry(command="/bin/true").call("langfuse.nope", {})
