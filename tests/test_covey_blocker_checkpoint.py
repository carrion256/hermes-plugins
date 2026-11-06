"""Delivered loop-guard blocks become durable checkpoints (covey-tasks #12).

The guard's directive text, the host's authoritative delivered notification and
the Covey transport are all supplied here, so nothing shells out and nothing
reaches the network: `CoveyRuntime._call` answers only the commands the runtime
really issues, which means a checkpoint that never reaches the CLI fails instead
of passing quietly.  The claim itself is taken through the real tool path.
"""

import json
import sys
import time
from types import SimpleNamespace

import pytest

TOKEN = "session_a1b2c3"
SUBTASK = "subtask-1"
CLAIM = "claim-1"
TITLE = "Repair the widget factory"
INVARIANT = "identical call and result repeated"
NEXT_STEP = "loop_recovery hypothesis: name the assumption and one exact check."
BLOCKER = {
    "invariant": INVARIANT,
    "approaches": [
        {"tool": "terminal", "argument_digest": "sha256:aaaa", "check_status": "failed"},
        {"tool": "read_file", "argument_digest": "sha256:bbbb", "check_status": None},
    ],
    "next_step": NEXT_STEP,
}
# Every capability probe asks for --help; one help text answers them all.
HELP_TEXT = ("--limit --offset --completion-policy --evidence-policy "
             "--routing-key --summary --evidence-digest")


def new_agent(session_id):
    """A real (weakref-able) Hermes agent object, constructed without IO."""
    from run_agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = session_id
    agent.platform = "cli"
    agent._persist_disabled = False
    agent._interrupt_requested = False
    return agent


def task_row(state):
    return {
        "subtask_id": SUBTASK, "meta_task_id": "meta-1", "title": TITLE, "kind": "work",
        "priority": 100, "state": state, "completion_policy": "direct",
        "evidence_policy": "optional", "routing_key": "default",
        "created_at": 1, "updated_at": 1,
    }


def claim_row(lease_deadline_ms):
    return {"claim_id": CLAIM, "fence_seq": 1, "owner_session_token": TOKEN,
            "lease_deadline": lease_deadline_ms}


def fake_transport():
    """The subset of the CLI this runtime actually calls, with recorded retries."""
    state = {"started": False, "retries": []}

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
            if state["retries"]:
                return {"ok": True, "data": {
                    "subtask": task_row("available"), "claim": None,
                    "attempt_outcomes": [{"claim_id": CLAIM, "fence_seq": 1,
                                          "outcome_kind": "retryable_failure"}]}}
            if state["started"]:
                return {"ok": True, "data": {"subtask": task_row("in_progress"),
                                             "claim": claim_row(now_ms() + 300_000),
                                             "attempt_outcomes": []}}
            return {"ok": True, "data": {"subtask": task_row("available"), "claim": None,
                                         "attempt_outcomes": []}}
        if argv[:2] == ["subtask", "retry"]:
            state["retries"].append(argv)
            return {"ok": True, "data": {}}
        return {"ok": True, "data": {}}

    return call, state


def now_ms():
    return int(time.time() * 1000)


def fake_call(call):
    def patched(self, command_args, *args, **kwargs):
        return call(list(command_args))
    return patched


@pytest.fixture
def bridge(load_plugin, monkeypatch, tmp_path):
    """A loaded covey-tasks plugin whose runtime talks to a faked transport."""
    loaded = []

    def build(mode="exhausted", *, cap=None):
        harness_namespace = load_plugin("covey-tasks", {
            "binary": "covey", "db_path": str(tmp_path / "covey.db"),
            "blocker_checkpoint": mode})
        runtime_mod = sys.modules.get("_covey_tasks_runtime")
        assert runtime_mod is not None, "the plugin never registered runtime.py"
        call, transport = fake_transport()
        monkeypatch.setattr(runtime_mod, "resolve_covey_binary",
                            lambda binary: str(binary))
        monkeypatch.setattr(runtime_mod.CoveyRuntime, "_call", fake_call(call))
        if cap is not None:
            monkeypatch.setattr(harness_namespace.module, "_loop_guard_redirect_cap",
                                lambda: cap)
        harness = SimpleNamespace(plugin=harness_namespace, module=harness_namespace.module,
                                  runtime_mod=runtime_mod, transport=transport,
                                  agent=new_agent("sess-1"), session_id="sess-1")
        loaded.append(harness)
        return harness

    yield build
    # Teardown runs while the transport patch is still live, so close()'s exit
    # call is answered by the fake instead of spawning a subprocess.
    for harness in reversed(loaded):
        harness.plugin.manager.unload("covey-tasks")


def start_claim(harness):
    """Claim and start the task through the real tool dispatch."""
    from agent.subagent_lifecycle import bind_subagent_parent
    with bind_subagent_parent(harness.agent):
        result = json.loads(harness.plugin.tool(
            "covey_tasks", {"action": "start", "subtask_id": SUBTASK}, task_id="t1"))
    assert result.get("ok") is True, result
    return result


def status(harness):
    from agent.subagent_lifecycle import bind_subagent_parent
    with bind_subagent_parent(harness.agent):
        return json.loads(harness.plugin.tool(
            "covey_tasks", {"action": "status"}, task_id="t1"))["data"]


def deliver(harness, directive_id, *, delivered=True, blocker=BLOCKER, message=None,
            other_selected=False):
    """Fire the host's authoritative post_tool_directive notification."""
    text = (message if message is not None
            else harness.runtime_mod.BLOCKER_PREFIX + " " + json.dumps(blocker))
    guard = {"directive_id": directive_id, "action": "block", "message": text,
             "outcome": "delivered" if delivered and not other_selected else "suppressed"}
    directives = [guard]
    selected = directive_id
    if other_selected:
        directives.append({"directive_id": "other:1", "action": "block",
                           "message": "SOME OTHER GUARD: nope", "outcome": "delivered"})
        selected = "other:1"
    return harness.plugin.hook(
        "post_tool_directive", session_id=harness.session_id, tool_name="terminal",
        tool_input={"command": "make test"}, modified_args=None, action="block",
        delivered=delivered, approval_gate=None, selected_directive_id=selected,
        selected_id_supplied=True, selected_disambiguated_from=None,
        suppressed_directive_ids=[] if delivered and not other_selected else [directive_id],
        directives=directives, task_id="t1", turn_id="turn-1", tool_call_id="call-1",
        api_request_id="api-1")


def deliver_blockers(harness, count, *, prefix="loop"):
    from agent.subagent_lifecycle import bind_subagent_parent
    with bind_subagent_parent(harness.agent):
        for index in range(count):
            deliver(harness, f"{prefix}:{index}")


def capture_checkpoints(harness, monkeypatch, *, raises=None):
    """Intercept the runtime's normal execute path and record what it was asked to do.

    Only `checkpoint` is intercepted: everything else still runs for real, so a
    test can read `status` through the same patched object.
    """
    calls = []
    original = harness.runtime_mod.CoveyRuntime.execute

    def fake_execute(self, arguments):
        if not isinstance(arguments, dict) or arguments.get("action") != "checkpoint":
            return original(self, arguments)
        calls.append(arguments)
        if raises is not None:
            raise raises
        return {"ok": True, "data": {"subtask_id": SUBTASK, "state": "available"}}

    monkeypatch.setattr(harness.runtime_mod.CoveyRuntime, "execute", fake_execute)
    return calls


def summary_of(argv, flag):
    for token in argv:
        if token.startswith(flag + "="):
            return token[len(flag) + 1:]
    raise AssertionError(f"flag {flag} missing from {argv}")


def test_off_mode_counts_delivered_blocks_but_never_checkpoints(bridge):
    harness = bridge("off")
    start_claim(harness)
    deliver_blockers(harness, 3)
    assert harness.transport["retries"] == []
    report = status(harness)
    assert report["blocker_checkpoints"] == []
    assert report["blocker_directives"]["sess-1"] == {"delivered": 3, "malformed": 0}


def test_first_mode_checkpoints_on_the_first_delivered_block(bridge):
    harness = bridge("first")
    start_claim(harness)
    deliver_blockers(harness, 1)
    assert len(harness.transport["retries"]) == 1
    assert summary_of(harness.transport["retries"][0], "--failure-code") == "checkpointed"
    assert status(harness)["blocker_checkpoints"] == [
        {"directive_id": "loop:0", "subtask_id": SUBTASK, "ok": True, "error": None}]


def test_exhausted_waits_until_the_guard_budget_is_spent(bridge):
    harness = bridge()  # default mode, default cap 2
    start_claim(harness)
    deliver_blockers(harness, 1)
    assert harness.transport["retries"] == []
    assert status(harness)["blocker_directives"]["sess-1"] == {"delivered": 1, "malformed": 0}
    deliver_blockers(harness, 1, prefix="loop2")
    assert len(harness.transport["retries"]) == 1


def test_exhausted_follows_the_configured_loop_guard_budget(bridge):
    harness = bridge(cap=3)
    start_claim(harness)
    deliver_blockers(harness, 2)
    assert harness.transport["retries"] == []
    deliver_blockers(harness, 1, prefix="loop2")
    assert len(harness.transport["retries"]) == 1


def test_suppressed_and_unselected_directives_are_never_counted(bridge):
    harness = bridge("first")
    start_claim(harness)
    from agent.subagent_lifecycle import bind_subagent_parent
    with bind_subagent_parent(harness.agent):
        deliver(harness, "loop:suppressed", delivered=False)
        deliver(harness, "loop:unselected", delivered=True, other_selected=True)
        assert status(harness)["blocker_directives"] == {}
        deliver(harness, "loop:real")
    assert len(harness.transport["retries"]) == 1
    assert status(harness)["blocker_directives"]["sess-1"] == {"delivered": 1, "malformed": 0}


def test_no_live_claim_is_a_counted_skip_with_its_reason(bridge):
    harness = bridge()
    status(harness)  # materialise the runtime without claiming anything
    deliver_blockers(harness, 2)
    assert harness.transport["retries"] == []
    assert status(harness)["blocker_checkpoints"] == [
        {"directive_id": "loop:1", "subtask_id": None, "ok": False, "error": "no_live_claim"}]


def test_malformed_blockers_are_counted_and_ignored(bridge):
    harness = bridge("first")
    start_claim(harness)
    bad = ["not json at all", json.dumps({"invariant": INVARIANT, "approaches": []}),
           json.dumps({"invariant": INVARIANT, "next_step": NEXT_STEP}),
           json.dumps({"invariant": INVARIANT, "next_step": NEXT_STEP, "approaches": {}})]
    from agent.subagent_lifecycle import bind_subagent_parent
    with bind_subagent_parent(harness.agent):
        for index, payload in enumerate(bad):
            deliver(harness, f"loop:bad:{index}", message=harness.runtime_mod.BLOCKER_PREFIX + " " + payload)
        deliver(harness, "loop:foreign", message="LOOP GUARD OF OTHERS: " + json.dumps(BLOCKER))
        report = status(harness)
    assert harness.transport["retries"] == []
    assert report["blocker_directives"]["sess-1"] == {"delivered": 0, "malformed": 4}
    assert report["blocker_checkpoints"] == []


def test_one_directive_never_checkpoints_twice(bridge, monkeypatch):
    harness = bridge("exhausted", cap=1)  # every NEW directive would checkpoint
    start_claim(harness)
    calls = capture_checkpoints(harness, monkeypatch)
    from agent.subagent_lifecycle import bind_subagent_parent
    with bind_subagent_parent(harness.agent):
        for _ in range(3):  # the same directive id, three authoritative notices
            deliver(harness, "loop:once")
        assert len(calls) == 1
        deliver(harness, "loop:different")  # control: a new directive does fire
    assert [call["request_id"] for call in calls] == [
        harness.runtime_mod.blocker_request_id("loop:once"),
        harness.runtime_mod.blocker_request_id("loop:different")]


def test_handoff_carries_the_recorded_attempts_and_validates(bridge, monkeypatch):
    harness = bridge("first")
    start_claim(harness)
    calls = capture_checkpoints(harness, monkeypatch)
    deliver_blockers(harness, 1)
    assert len(calls) == 1
    arguments = calls[0]
    assert arguments["action"] == "checkpoint"
    assert arguments["subtask_id"] == SUBTASK
    # One directive is one request: the id is derived from it, and the tool's own
    # parser (which admits only UUIDs) must accept every field we send.
    assert arguments["request_id"] == harness.runtime_mod.blocker_request_id("loop:0")
    assert arguments["request_id"] != harness.runtime_mod.blocker_request_id("loop:1")
    parsed = harness.runtime_mod.parse_action(dict(arguments))
    assert isinstance(parsed, harness.runtime_mod.CheckpointAction)
    assert parsed.request_id == arguments["request_id"]
    handoff = harness.runtime_mod._validate_handoff(arguments["handoff"])
    assert handoff["objective"] == TITLE
    assert handoff["changes"] == [] and handoff["verification_refs"] == []
    assert handoff["failed_approaches"] == [
        "terminal#sha256:aaaa (failed)", "read_file#sha256:bbbb (unverified)", INVARIANT]
    assert handoff["remaining"] == [NEXT_STEP]


def test_objective_falls_back_to_the_task_id_without_a_title(bridge, monkeypatch):
    harness = bridge("first")
    start_claim(harness)
    calls = capture_checkpoints(harness, monkeypatch)
    original = harness.runtime_mod._blocker_objective
    monkeypatch.setattr(harness.runtime_mod, "_blocker_objective",
                        lambda task, subtask_id: original({"title": "   "}, subtask_id))
    deliver_blockers(harness, 1)
    assert calls[0]["handoff"]["objective"] == f"subtask {SUBTASK}"


def test_an_unexpected_defect_is_reported_not_raised(bridge, monkeypatch):
    harness = bridge("first")
    start_claim(harness)
    capture_checkpoints(harness, monkeypatch, raises=RuntimeError("transport exploded"))
    from agent.subagent_lifecycle import bind_subagent_parent
    with bind_subagent_parent(harness.agent):
        deliver(harness, "loop:boom")  # must not raise into the dispatch
        report = status(harness)
    assert report["blocker_checkpoints"] == [
        {"directive_id": "loop:boom", "subtask_id": SUBTASK, "ok": False,
         "error": "checkpoint_raised (RuntimeError)"}]


def test_invalid_mode_names_the_setting_and_refuses_registration(load_plugin, bridge):
    harness = bridge()
    with pytest.raises(ValueError, match="blocker_checkpoint"):
        harness.module._blocker_mode(
            SimpleNamespace(get_config=lambda key, default: "sometimes"))
    with pytest.raises(AssertionError, match="blocker_checkpoint"):
        load_plugin("covey-tasks", {"binary": "covey", "db_path": "/tmp/covey.db",
                                    "blocker_checkpoint": "sometimes"})
