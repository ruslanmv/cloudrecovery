"""Tests for the Source-of-Truth Engine.

Uses a real on-disk mini-repo and a real local git repo (created in tmp_path) so
sync, index, correlate, and context are exercised end to end rather than mocked.
No network access and no LLM calls.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types

import pytest

from cloudrecovery.sources import (
    AttachSourceRequest,
    ServiceLink,
    SourceEngine,
    SourceRegistry,
    correlate,
    extract_terms,
    identity_from_evidence,
)
from cloudrecovery.sources.context import compose_context
from cloudrecovery.sources.index import build_index, extract_symbols
from cloudrecovery.sources.resolve import classify_action_shape, propose_resolution
from cloudrecovery.sources.sync import resolve_local_path

LOOP_EVIDENCE = {
    "source": "agent:langfuse",
    "kind": "langfuse_loop",
    "severity": "critical",
    "message": "Repeating loop in trace abc: intake_gate -> injection_check x3",
    "payload": {
        "service.name": "blue-agent-core",
        "trace_id": "abc",
        "cycle": ["intake_gate", "injection_check", "extract", "classify"],
        "repeat_count": 3,
        "tokens_in_loop": 15000,
    },
    "incident_id": "langfuse-loop-abc",
}


@pytest.fixture
def mini_repo(tmp_path):
    """A small service tree resembling a LangGraph-style agent."""
    root = tmp_path / "agent-core"
    (root / "src" / "nodes").mkdir(parents=True)
    (root / "docs").mkdir()

    (root / "src" / "nodes" / "intake_gate.py").write_text(
        "MAX_RETRIES = 99\n\n\n"
        "def intake_gate(state):\n"
        "    '''Validate an inbound ticket.'''\n"
        "    if not state.valid:\n"
        "        return 'injection_check'\n"
        "    return 'extract'\n",
        "utf-8",
    )
    (root / "src" / "nodes" / "injection_check.py").write_text(
        "def injection_check(state):\n    return 'intake_gate'\n", "utf-8"
    )
    (root / "src" / "graph.py").write_text(
        "from .nodes.intake_gate import intake_gate\n"
        "from .nodes.injection_check import injection_check\n\n\n"
        "def build_graph():\n"
        "    graph = {}\n"
        "    graph['intake_gate'] = intake_gate\n"
        "    graph['injection_check'] = injection_check\n"
        "    return graph\n",
        "utf-8",
    )
    (root / "src" / "unrelated.py").write_text("def send_email(to):\n    return to\n", "utf-8")
    (root / "docs" / "logo.png").write_bytes(b"\x89PNG fake")
    (root / "deploy.yaml").write_text(
        "kind: Deployment\nname: checkout-service\nmemory_limit: 128Mi\n", "utf-8"
    )
    return root


@pytest.fixture
def registry(tmp_path):
    return SourceRegistry(path=tmp_path / "sources.json")


@pytest.fixture
def engine(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDRECOVERY_SOURCE_ALLOWED_PATHS", str(tmp_path))
    return SourceEngine(registry=registry, workspace=tmp_path / "workspace")


def attach_local(engine, path, **kwargs):
    request = AttachSourceRequest(
        name=kwargs.pop("name", "agent-core"),
        kind="local",
        local_path=str(path),
        service_links=[ServiceLink(service_name="blue-agent-core")],
        **kwargs,
    )
    return engine.attach(request)


# ---------------------------------------------------------------------------
# Models + registry
# ---------------------------------------------------------------------------

def test_attach_requires_url_for_remote_kinds():
    with pytest.raises(ValueError, match="requires a clone url"):
        AttachSourceRequest(name="x", kind="github").to_source()


def test_attach_requires_path_for_local_kind():
    with pytest.raises(ValueError, match="requires a local_path"):
        AttachSourceRequest(name="x", kind="local").to_source()


def test_attach_slugifies_id():
    source = AttachSourceRequest(
        name="Blue Agent Core!", kind="github", url="https://example.com/r.git"
    ).to_source()
    assert source.id == "blue-agent-core"


def test_registry_round_trip_and_duplicate_rejection(registry, tmp_path):
    request = AttachSourceRequest(
        name="repo-a", kind="github", url="https://example.com/a.git",
        service_links=[ServiceLink(service_name="svc-a")],
    )
    registry.add(request)

    assert [s.id for s in registry.list()] == ["repo-a"]
    with pytest.raises(ValueError, match="already exists"):
        registry.add(request)

    assert registry.delete("repo-a") is True
    assert registry.list() == []
    assert registry.delete("repo-a") is False


def test_registry_never_persists_a_token(registry, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "ghp_supersecret")
    registry.add(
        AttachSourceRequest(
            name="repo", kind="github", url="https://example.com/r.git",
            credential_env="MY_TOKEN",
            service_links=[ServiceLink(service_name="svc")],
        )
    )
    raw = registry.path.read_text("utf-8")
    assert "MY_TOKEN" in raw, "the env var name is what gets stored"
    assert "ghp_supersecret" not in raw, "the token value must never be persisted"


def test_registry_survives_corrupt_store(registry):
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.path.write_text("{not json", "utf-8")
    assert registry.list() == []


def test_source_link_matching_is_exact_and_anded():
    link = ServiceLink(namespace="prod", deployment="checkout")
    assert link.matches(namespace="prod", deployment="checkout") is True
    assert link.matches(namespace="prod", deployment="other") is False
    assert link.matches(namespace="PROD", deployment="Checkout") is True
    assert link.matches(namespace="prod") is False


def test_empty_link_never_matches():
    assert ServiceLink().matches(service_name="anything") is False


def test_find_for_identity_prefers_more_specific_link(registry):
    registry.add(
        AttachSourceRequest(
            name="broad", kind="github", url="https://e.com/b.git",
            service_links=[ServiceLink(namespace="prod")],
        )
    )
    registry.add(
        AttachSourceRequest(
            name="narrow", kind="github", url="https://e.com/n.git",
            service_links=[ServiceLink(namespace="prod", deployment="checkout")],
        )
    )

    found = registry.find_for_identity(namespace="prod", deployment="checkout")
    assert [s.id for s in found] == ["narrow", "broad"]


def test_find_for_identity_returns_nothing_without_identity(registry):
    assert registry.find_for_identity() == []
    assert registry.find_for_identity(service_name=None) == []


def test_identity_from_evidence_reads_all_spellings():
    assert identity_from_evidence(LOOP_EVIDENCE)["service_name"] == "blue-agent-core"
    assert identity_from_evidence(
        {"payload": {"namespace": "prod", "deployment": "checkout"}}
    ) == {
        "service_name": None, "namespace": "prod",
        "deployment": "checkout", "argocd_application": None,
    }
    assert identity_from_evidence(
        {"payload": {"metadata": {"service.name": "nested-svc"}}}
    )["service_name"] == "nested-svc"
    assert identity_from_evidence(None) == {}


# ---------------------------------------------------------------------------
# Sync: allow-list and git
# ---------------------------------------------------------------------------

def test_local_path_outside_allowlist_is_refused(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("CLOUDRECOVERY_SOURCE_ALLOWED_PATHS", str(allowed))

    with pytest.raises(PermissionError, match="outside the allow-list"):
        resolve_local_path(str(outside))

    assert resolve_local_path(str(allowed)) == allowed.resolve()


def test_local_symlink_cannot_escape_allowlist(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (allowed / "escape").symlink_to(secret)
    monkeypatch.setenv("CLOUDRECOVERY_SOURCE_ALLOWED_PATHS", str(allowed))

    # The symlink resolves outside the allow-list, so it must be refused even
    # though its path appears to sit inside an allowed directory.
    with pytest.raises(PermissionError):
        resolve_local_path(str(allowed / "escape"))


def test_sync_reports_missing_local_path(engine, tmp_path):
    attach_local(engine, tmp_path / "does-not-exist")
    report = engine.sync("agent-core")
    assert report.ok is False
    assert "does not exist" in report.error


def test_scrub_removes_token_from_git_output():
    from cloudrecovery.sources.sync import scrub

    message = "fatal: could not read https://x-access-token:ghp_secret@github.com/o/r.git"
    cleaned = scrub(message, "ghp_secret")
    assert "ghp_secret" not in cleaned
    assert "x-access-token" not in cleaned


def test_authenticated_url_never_tokenizes_ssh():
    from cloudrecovery.sources.sync import _authenticated_url

    assert _authenticated_url("git@github.com:o/r.git", "tok") == "git@github.com:o/r.git"
    assert "x-access-token:tok@" in _authenticated_url("https://github.com/o/r.git", "tok")


def test_sync_requires_credential_when_env_var_is_unset(engine, monkeypatch):
    monkeypatch.delenv("ABSENT_TOKEN", raising=False)
    engine.attach(
        AttachSourceRequest(
            name="remote", kind="github", url="https://example.invalid/r.git",
            credential_env="ABSENT_TOKEN",
            service_links=[ServiceLink(service_name="svc")],
        )
    )
    report = engine.sync("remote")
    assert report.ok is False
    assert "ABSENT_TOKEN" in report.error


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_sync_clones_and_indexes_a_real_git_repo(engine, mini_repo, tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=mini_repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=mini_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=mini_repo, check=True,
    )

    engine.attach(
        AttachSourceRequest(
            name="cloned", kind="github", url=str(mini_repo),
            service_links=[ServiceLink(service_name="blue-agent-core")],
        )
    )
    report = engine.sync("cloned")

    assert report.ok is True, report.error
    assert report.commit and len(report.commit) == 40
    assert report.files_indexed > 0

    index = engine.get_index("cloned")
    assert any(f.path == "src/nodes/intake_gate.py" for f in index.files)
    # The clone must be an isolated workspace copy, not the origin tree.
    assert str(mini_repo) not in index.root


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def test_extract_symbols_python_and_yaml():
    assert extract_symbols("def foo():\n    pass\nclass Bar:\n    pass\n", "python") == [
        "foo", "Bar",
    ]
    assert "checkout-service" in extract_symbols("name: checkout-service\n", "yaml")
    assert extract_symbols("def foo(): pass", None) == []


def test_extract_symbols_never_raises_on_garbage():
    assert extract_symbols("def \x00 (((", "python") == []


def test_build_index_records_paths_and_symbols_but_no_content(mini_repo):
    index = build_index("mini", mini_repo)

    paths = {f.path for f in index.files}
    assert "src/nodes/intake_gate.py" in paths
    assert "deploy.yaml" in paths

    entry = next(f for f in index.files if f.path == "src/nodes/intake_gate.py")
    assert "intake_gate" in entry.symbols

    # Binary assets are skipped, and no file content is retained anywhere.
    assert "docs/logo.png" not in paths
    serialized = json.dumps(index.to_dict())
    assert "MAX_RETRIES" not in serialized
    assert index.stats.files_indexed == len(index.files)


def test_index_read_file_refuses_traversal(mini_repo):
    index = build_index("mini", mini_repo)
    assert "intake_gate" in index.read_file("src/nodes/intake_gate.py")
    with pytest.raises(PermissionError, match="escapes the index root"):
        index.read_file("../../../etc/passwd")


def test_index_persists_and_reloads(engine, mini_repo, tmp_path):
    attach_local(engine, mini_repo)
    report = engine.sync("agent-core")
    assert report.ok is True

    # Drop the in-process cache to force a disk read.
    from cloudrecovery.sources import index as index_module

    index_module._CACHE.clear()
    reloaded = engine.get_index("agent-core")
    assert reloaded is not None
    assert reloaded.stats.files_indexed == report.files_indexed


# ---------------------------------------------------------------------------
# Correlate
# ---------------------------------------------------------------------------

def test_extract_terms_uses_cycle_span_names():
    terms = extract_terms(LOOP_EVIDENCE)
    assert "intake_gate" in terms
    assert "injection_check" in terms
    # Noise words must not become search terms.
    assert "loop" not in terms
    assert "trace" not in terms


def test_extract_terms_empty_for_bare_evidence():
    assert extract_terms({"kind": "langfuse_scan", "payload": {}, "message": ""}) == []


def test_correlate_finds_the_offending_node_and_its_caller(mini_repo):
    index = build_index("mini", mini_repo)
    result = correlate(index, LOOP_EVIDENCE, limit=3)

    top_paths = [m.path for m in result.matches]
    assert "src/nodes/intake_gate.py" in top_paths
    assert "src/nodes/injection_check.py" in top_paths
    assert "src/unrelated.py" not in top_paths

    # The graph wiring is what explains a missing exit condition, so it must
    # come along as structural context — and it must outrank a sibling node that
    # merely mentions the same name, because it actually imports it.
    assert result.adjacent, "expected a structurally-adjacent caller"
    assert result.adjacent[0].path == "src/graph.py"
    assert "imports" in result.adjacent[0].reason


def test_correlate_reports_when_nothing_matches(mini_repo):
    index = build_index("mini", mini_repo)
    result = correlate(
        index,
        {"kind": "langfuse_loop", "payload": {"cycle": ["zzz_nonexistent_node"]}},
        limit=3,
    )
    assert result.matches == []
    assert "No file in this source matched" in result.note


def test_correlate_handles_evidence_without_terms(mini_repo):
    index = build_index("mini", mini_repo)
    result = correlate(index, {"kind": "x", "payload": {}, "message": ""})
    assert result.matches == []
    assert "No searchable identifiers" in result.note


# ---------------------------------------------------------------------------
# Context tiers
# ---------------------------------------------------------------------------

def test_context_tiers_are_labeled_and_grounded(mini_repo):
    index = build_index("mini", mini_repo)
    correlation = correlate(index, LOOP_EVIDENCE, limit=2)
    context = compose_context(LOOP_EVIDENCE, index, correlation)

    assert context.is_grounded is True
    prompt = context.to_prompt()
    assert "TIER 1 — DIAGNOSIS" in prompt
    assert "TIER 2 — CORRELATED SOURCE" in prompt
    assert "src/nodes/intake_gate.py" in prompt
    assert "MAX_RETRIES" in prompt, "the snippet should carry real file content"


def test_context_without_source_says_so():
    context = compose_context(LOOP_EVIDENCE)
    assert context.is_grounded is False
    assert any("No source is linked" in n for n in context.notes)
    assert "(no source files correlated)" in context.to_prompt()


def test_docs_tier_is_reference_only_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("CLOUDRECOVERY_DOCS_FETCH_ENABLED", raising=False)
    context = compose_context(LOOP_EVIDENCE, docs_url="https://docs.example.com/x")

    assert len(context.docs) == 1
    assert context.docs[0].fetched is False
    assert context.docs[0].content == ""
    prompt = context.to_prompt()
    assert "LOWEST TRUST" in prompt
    assert "not fetched; reference only" in prompt


def test_docs_tier_skipped_when_source_grounded(mini_repo):
    """Docs are a last resort — never consulted when real code was found."""
    index = build_index("mini", mini_repo)
    correlation = correlate(index, LOOP_EVIDENCE, limit=2)
    context = compose_context(
        LOOP_EVIDENCE, index, correlation, docs_url="https://docs.example.com/x"
    )
    assert context.docs == []


def test_snippet_content_is_redacted(tmp_path):
    repo = tmp_path / "secretive"
    repo.mkdir()
    (repo / "config.py").write_text(
        'def load():\n    AWS_SECRET_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"\n    return 1\n',
        "utf-8",
    )
    index = build_index("s", repo)
    context = compose_context(
        {"kind": "app_error", "payload": {"cycle": ["load"]}},
        index,
        correlate(index, {"kind": "app_error", "payload": {"cycle": ["load"]}}),
    )
    joined = "\n".join(s.content for s in context.snippets)
    assert "AKIAIOSFODNN7EXAMPLE" not in joined


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------

def test_action_shape_classification():
    assert classify_action_shape("langfuse_loop") == "app_code_fix"
    assert classify_action_shape("argocd_outofsync") == "infra_drift_fix"
    assert classify_action_shape("pod_status") == "infra_drift_fix"
    # Unknown kinds must default to the safer shape, not an infra mutation.
    assert classify_action_shape("something_new") == "app_code_fix"
    assert classify_action_shape(None) == "app_code_fix"


def _install_llm(monkeypatch, build_llm):
    """Swap in a fake provider module.

    resolve.py imports cloudrecovery.llm.llm_provider lazily inside the call, and
    that module needs crewai at import time — so injecting into sys.modules keeps
    these tests independent of whether the heavy LLM stack is installed.
    """
    module = types.SimpleNamespace(build_llm=build_llm)
    monkeypatch.setitem(sys.modules, "cloudrecovery.llm.llm_provider", module)


def _raise_no_llm(*_a, **_k):
    raise RuntimeError("no provider configured")


def test_resolution_returns_prompt_and_citations_without_an_llm(mini_repo, monkeypatch):
    _install_llm(monkeypatch, _raise_no_llm)
    index = build_index("mini", mini_repo)
    correlation = correlate(index, LOOP_EVIDENCE, limit=2)
    context = compose_context(LOOP_EVIDENCE, index, correlation)

    resolution = propose_resolution(context, evidence_kind="langfuse_loop")

    assert resolution.llm_available is False
    assert resolution.error
    assert resolution.grounded is True
    assert any("intake_gate.py" in c for c in resolution.citations)
    assert "ROOT CAUSE" in resolution.prompt


def test_resolution_uses_the_configured_llm_when_available(mini_repo, monkeypatch):
    seen = {}

    class FakeLLM:
        def call(self, prompt):
            seen["prompt"] = prompt
            return "ROOT CAUSE — src/nodes/intake_gate.py:1 has MAX_RETRIES = 99."

    _install_llm(monkeypatch, lambda *a, **k: FakeLLM())

    index = build_index("mini", mini_repo)
    context = compose_context(LOOP_EVIDENCE, index, correlate(index, LOOP_EVIDENCE, limit=2))
    resolution = propose_resolution(context, evidence_kind="langfuse_loop")

    assert resolution.llm_available is True
    assert "MAX_RETRIES" in resolution.recommendation
    # The model must be handed the grounded tiers, not just the symptom.
    assert "TIER 2 — CORRELATED SOURCE" in seen["prompt"]
    assert "src/nodes/intake_gate.py" in seen["prompt"]


def test_proposed_infra_action_carries_a_policy_decision(monkeypatch):
    _install_llm(monkeypatch, _raise_no_llm)
    context = compose_context({"kind": "argocd_outofsync", "payload": {}})
    resolution = propose_resolution(context, evidence_kind="argocd_outofsync", env="prod")

    assert resolution.action_shape == "infra_drift_fix"
    action = resolution.proposed_actions[0]
    assert action.requires_approval is True, "no mutation may bypass the approval gate"


def test_resolution_does_not_leak_prompt_by_default(monkeypatch):
    _install_llm(monkeypatch, _raise_no_llm)
    resolution = propose_resolution(compose_context(LOOP_EVIDENCE), evidence_kind="x")
    assert "prompt" not in resolution.to_dict()
    assert "prompt" in resolution.to_dict(include_prompt=True)


# ---------------------------------------------------------------------------
# Engine end to end
# ---------------------------------------------------------------------------

def test_engine_grounds_incident_end_to_end(engine, mini_repo):
    attach_local(engine, mini_repo)
    assert engine.sync("agent-core").ok is True

    analysis = engine.analyze(LOOP_EVIDENCE)

    assert analysis.matched_sources == ["agent-core"]
    assert analysis.identity["service_name"] == "blue-agent-core"
    assert "src/nodes/intake_gate.py" in analysis.correlation.all_paths
    assert analysis.context.is_grounded is True

    payload = analysis.to_dict()
    assert payload["correlation"]["matches"][0]["path"]
    assert payload["resolution"] is None, "resolve must be opt-in"


def test_engine_explains_when_no_source_is_linked(engine):
    analysis = engine.analyze(LOOP_EVIDENCE)
    assert analysis.matched_sources == []
    assert "No attached source is linked" in analysis.note
    assert analysis.context.is_grounded is False


def test_engine_explains_when_source_linked_but_never_synced(engine, mini_repo):
    attach_local(engine, mini_repo)
    analysis = engine.analyze(LOOP_EVIDENCE)
    assert analysis.matched_sources == ["agent-core"]
    assert "no index yet" in analysis.note


def test_engine_ignores_sources_linked_to_another_service(engine, mini_repo):
    engine.attach(
        AttachSourceRequest(
            name="other", kind="local", local_path=str(mini_repo),
            service_links=[ServiceLink(service_name="some-other-service")],
        )
    )
    assert engine.sync("other").ok is True
    assert engine.analyze(LOOP_EVIDENCE).matched_sources == []


def test_engine_detach_removes_index(engine, mini_repo):
    attach_local(engine, mini_repo)
    engine.sync("agent-core")
    assert engine.get_index("agent-core") is not None

    assert engine.detach("agent-core") is True
    assert engine.get_index("agent-core") is None
    assert engine.list_sources() == []
    assert engine.detach("agent-core") is False


def test_engine_detach_never_deletes_a_local_source_tree(engine, mini_repo):
    attach_local(engine, mini_repo)
    engine.sync("agent-core")
    engine.detach("agent-core")
    assert (mini_repo / "src" / "nodes" / "intake_gate.py").exists(), (
        "a local source is the user's own working tree and must survive detach"
    )


def test_engine_correlates_openshift_style_evidence(engine, mini_repo):
    engine.attach(
        AttachSourceRequest(
            name="gitops", kind="local", local_path=str(mini_repo),
            service_links=[ServiceLink(namespace="prod", deployment="checkout-service")],
        )
    )
    assert engine.sync("gitops").ok is True

    analysis = engine.analyze(
        {
            "source": "agent:ocp",
            "kind": "pod_status",
            "message": "CrashLoopBackOff",
            "payload": {
                "namespace": "prod",
                "deployment": "checkout-service",
                "memory_limit": "128Mi",
            },
        }
    )

    assert analysis.matched_sources == ["gitops"]
    assert "deploy.yaml" in analysis.correlation.all_paths
