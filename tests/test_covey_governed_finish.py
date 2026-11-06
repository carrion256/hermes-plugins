"""Opt-in governed finish (covey-tasks #11): completion must be proven.

Nothing here shells out and nothing reaches the network: ``CoveyRuntime._call``
answers only the commands the runtime really issues, so a finish that was
refused before the transport is provable from the recorded argv, and a digest
that never reached the CLI fails instead of passing quietly.  The closure is
written through the host's own plugin-state facade, exactly as the continuation
plugin writes it, so the adapter is tested against the real store shape.
"""

import hashlib
import json
import sys
import time
from types import SimpleNamespace

import pytest

TOKEN = "session_a1b2c3"
SUBTASK = "subtask-1"
CLAIM = "claim-1"
SESSION = "sess-1"
SUMMARY = "widget factory repaired"
# Every capability probe asks for --help; one help text answers them all.
HELP_TEXT = ("--limit --offset --completion-policy --evidence-policy "
             "--routing-key --summary --evidence-digest")
BOUND = "pytest -q tests/test_widget.py"
UNBOUND = "pytest -q tests/test_shipping.py"


def new_agent(session_id):
    """A real (weakref-able) Hermes agent object, constructed without IO."""
    from run_agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = session_id
    agent.platform = "cli"
    agent._persist_disabled = False
    agent._interrupt_requested = False
    return agent


def now_ms():
    return int(time.time() * 1000)


def task_row(state):
    return {
        "subtask_id": SUBTASK, "meta_task_id": "meta-1", "title": "Repair the widget",
        "kind": "work", "priority": 100, "state": state, "completion_policy": "direct",
        "evidence_policy": "optional", "routing_key": "default",
        "created_at": 1, "updated_at": 1,
    }


def claim_row(lease_deadline_ms):
    return {"claim_id": CLAIM, "fence_seq": 1, "owner_session_token": TOKEN,
            "lease_deadline": lease_deadline_ms}


def succeeded_row():
    return {"claim_id": CLAIM, "fence_seq": 1, "outcome_kind": "succeeded",
            "summary": SUMMARY, "evidence_digest": None, "failure_code": None,
            "recorded_at": 1}


def fake_transport():
    """The subset of the CLI a finish needs, with the finish argv recorded."""
    state = {"started": False, "finished": []}

    def call(command_args):
        argv = [str(arg) for arg in command_args]
        if "--help" in argv:
            return {"ok": True, "data": {"help_text": HELP_TEXT}}
        if argv[:2] == ["session", "register"]:
            return {"ok": True, "data": {"session_token": TOKEN}}
        if argv[:2] == ["subtask", "claim"]:
            return {"ok": True, "data": {"claim_id": CLAIM, "fence_seq": 1,
                                         "lease_deadline": now_ms() + 300_000}}
        if argv[:2] == ["subtask", "start"]:
            state["started"] = True
            return {"ok": True, "data": {}}
        if argv[:2] == ["subtask", "status"]:
            if state["finished"]:
                return {"ok": True, "data": {"subtask": task_row("completed"), "claim": None,
                                             "attempt_outcomes": [succeeded_row()]}}
            if state["started"]:
                return {"ok": True, "data": {"subtask": task_row("in_progress"),
                                             "claim": claim_row(now_ms() + 300_000),
                                             "attempt_outcomes": []}}
            return {"ok": True, "data": {"subtask": task_row("available"), "claim": None,
                                         "attempt_outcomes": []}}
        if argv[:2] == ["subtask", "finish"]:
            state["finished"].append(argv)
            return {"ok": True, "data": {}}
        return {"ok": True, "data": {}}

    return call, state


def fake_call(call):
    def patched(self, command_args, *args, **kwargs):
        return call(list(command_args))
    return patched


@pytest.fixture
def bridge(load_plugin, monkeypatch, tmp_path):
    """A loaded covey-tasks plugin whose runtime talks to a faked transport."""
    loaded = []

    def build():
        harness_namespace = load_plugin("covey-tasks", {
            "binary": "covey", "db_path": str(tmp_path / "covey.db")})
        runtime_mod = sys.modules.get("_covey_tasks_runtime")
        assert runtime_mod is not None, "the plugin never registered runtime.py"
        call, transport = fake_transport()
        monkeypatch.setattr(runtime_mod, "resolve_covey_binary", lambda binary: str(binary))
        monkeypatch.setattr(runtime_mod.CoveyRuntime, "_call", fake_call(call))
        harness = SimpleNamespace(plugin=harness_namespace, module=harness_namespace.module,
                                 runtime_mod=runtime_mod, transport=transport,
                                 agent=new_agent(SESSION), session_id=SESSION)
        loaded.append(harness)
        return harness

    yield build
    # Teardown runs while the transport patch is still live, so close()'s exit
    # call is answered by the fake instead of spawning a subprocess.
    for harness in reversed(loaded):
        harness.plugin.manager.unload("covey-tasks")


def criterion(identity, *, bound=True):
    record = {"check_kind": "terminal", "check_identity": identity}
    if bound:
        record["evidence"] = {"id": "evidence-" + identity[:8]}
    return record


def closure_record(*, verdict="clean", criteria=None, blockers=(), findings=()):
    """The durable record shape ``plugins/continuation/closure.py`` reads."""
    return {
        "review": {"verdict": verdict, "identity": "review:widget-1",
                   "paths": ["src/widget.py", "tests/test_widget.py"]},
        "acceptance": {"tests": criterion(BOUND)} if criteria is None else criteria,
        "blockers": list(blockers),
        "findings": list(findings),
        "rounds": 2,
    }


def seed_closure(record):
    """Write one session's closure through the host's own state facade."""
    from hermes_cli.plugins_state import PluginState
    PluginState("continuation").set("session:" + SESSION, record)


def expected_digest(record):
    """Re-derive the digest independently: canonical JSON, then sha256."""
    canonical = {
        "review_identity": record["review"]["identity"],
        "review_paths": record["review"]["paths"],
        "acceptance": [[cid, crit.get("check_identity", "")] for cid, crit in
                       sorted(record["acceptance"].items(), key=lambda item: item[0])],
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def start_claim(harness):
    """Claim and start the task through the real tool dispatch."""
    from agent.subagent_lifecycle import bind_subagent_parent
    with bind_subagent_parent(harness.agent):
        result = json.loads(harness.plugin.tool(
            "covey_tasks", {"action": "start", "subtask_id": SUBTASK}, task_id="t1"))
    assert result.get("ok") is True, result
    return result


def finish(harness, **over):
    from agent.subagent_lifecycle import bind_subagent_parent
    arguments = {"action": "finish", "subtask_id": SUBTASK, "summary": SUMMARY}
    arguments.update(over)
    with bind_subagent_parent(harness.agent):
        return json.loads(harness.plugin.tool("covey_tasks", arguments, task_id="t2"))


def status(harness):
    from agent.subagent_lifecycle import bind_subagent_parent
    with bind_subagent_parent(harness.agent):
        return json.loads(harness.plugin.tool(
            "covey_tasks", {"action": "status"}, task_id="t3"))["data"]


def option_of(argv, flag):
    for token in argv:
        if token.startswith(flag + "="):
            return token[len(flag) + 1:]
    raise AssertionError(f"flag {flag} missing from {argv}")


def test_governed_finish_refuses_when_the_review_is_not_clean(bridge):
    harness = bridge()
    start_claim(harness)
    seed_closure(closure_record(verdict="findings", findings=["the fix hides the cause"]))
    result = finish(harness, governed=True)
    assert result["ok"] is False and result["code"] == "not_governed_closed", result
    assert result["data"]["verdict"] == "findings"
    assert result["data"]["unbound_criteria"] == [] and result["data"]["blockers"] == []
    assert "findings" in result["message"]
    assert harness.transport["finished"] == []  # refused before the transport


def test_governed_finish_refuses_an_unbound_criterion(bridge):
    harness = bridge()
    start_claim(harness)
    seed_closure(closure_record(criteria={
        "tests": criterion(BOUND),
        "shipping": criterion(UNBOUND, bound=False),
    }))
    result = finish(harness, governed=True)
    assert result["ok"] is False and result["code"] == "not_governed_closed", result
    assert result["data"]["unbound_criteria"] == ["shipping"]
    assert "unbound acceptance criteria: shipping" in result["message"]
    assert harness.transport["finished"] == []


def test_governed_finish_refuses_outstanding_blockers(bridge):
    harness = bridge()
    start_claim(harness)
    seed_closure(closure_record(blockers=["evidence drifted from the reviewed paths"]))
    result = finish(harness, governed=True)
    assert result["ok"] is False and result["code"] == "not_governed_closed", result
    assert result["data"]["blockers"] == ["evidence drifted from the reviewed paths"]
    assert result["data"]["verdict"] == "clean"
    assert harness.transport["finished"] == []


def test_governed_finish_records_the_derived_digest(bridge):
    harness = bridge()
    start_claim(harness)
    record = closure_record(criteria={
        "shipping": criterion(UNBOUND),
        "tests": criterion(BOUND),
    })
    seed_closure(record)
    result = finish(harness, governed=True)
    assert result["ok"] is True, result
    assert len(harness.transport["finished"]) == 1
    argv = harness.transport["finished"][0]
    assert option_of(argv, "--evidence-digest") == expected_digest(record)
    assert option_of(argv, "--summary") == (
        "governed: review clean; 2 criteria bound; " + SUMMARY)


def test_governed_finish_refuses_a_conflicting_caller_digest(bridge):
    harness = bridge()
    start_claim(harness)
    record = closure_record()
    seed_closure(record)
    supplied = "sha256:" + "cd" * 32
    result = finish(harness, governed=True, evidence_digest=supplied)
    assert result["ok"] is False and result["code"] == "evidence_mismatch", result
    assert result["data"]["supplied"] == supplied
    assert result["data"]["expected"] == expected_digest(record)
    assert harness.transport["finished"] == []


def test_governed_finish_accepts_the_digest_it_would_derive(bridge):
    harness = bridge()
    start_claim(harness)
    record = closure_record()
    seed_closure(record)
    result = finish(harness, governed=True, evidence_digest=expected_digest(record))
    assert result["ok"] is True, result
    assert option_of(harness.transport["finished"][0], "--evidence-digest") == (
        expected_digest(record))


def test_governed_finish_refuses_without_a_closure_record(bridge):
    harness = bridge()
    start_claim(harness)
    result = finish(harness, governed=True)  # nothing was ever written
    assert result["ok"] is False and result["code"] == "governance_unavailable", result
    assert harness.transport["finished"] == []


def test_governed_finish_refuses_an_unreadable_closure_store(bridge):
    harness = bridge()
    start_claim(harness)
    seed_closure(closure_record())
    from hermes_cli.plugins_state import PluginState
    PluginState("continuation").path.write_text("{ not json", encoding="utf-8")
    result = finish(harness, governed=True)
    assert result["ok"] is False and result["code"] == "governance_unavailable", result
    assert harness.transport["finished"] == []


def test_ungoverned_finish_never_reads_the_closure(bridge, monkeypatch):
    harness = bridge()
    start_claim(harness)

    def never():
        raise AssertionError("the default finish must not read governance")

    monkeypatch.setattr(harness.runtime_mod, "_load_closure_module", never)
    result = finish(harness)  # no governed key at all: the direct path
    assert result["ok"] is True, result
    assert len(harness.transport["finished"]) == 1
    argv = harness.transport["finished"][0]
    assert option_of(argv, "--summary") == SUMMARY  # untouched, unmarked
    assert "--evidence-digest" not in " ".join(argv)


def test_governed_false_is_the_untouched_direct_path(bridge, monkeypatch):
    harness = bridge()
    start_claim(harness)

    def never():
        raise AssertionError("governed=false must not read governance")

    monkeypatch.setattr(harness.runtime_mod, "_load_closure_module", never)
    result = finish(harness, governed=False)
    assert result["ok"] is True, result
    assert option_of(harness.transport["finished"][0], "--summary") == SUMMARY


def test_status_reports_governed_finish_availability(bridge):
    harness = bridge()
    report = status(harness)
    assert report["governed_finish"] == {"available": True}
