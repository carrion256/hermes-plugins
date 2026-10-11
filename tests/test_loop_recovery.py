"""Recovery must distinguish failed approaches, credit delivery, and retain blockers."""
import json

import pytest


@pytest.fixture
def recovery(load_plugin):
    from run_agent import AIAgent
    from agent.subagent_lifecycle import bind_subagent_parent

    h = load_plugin("loop-guard", {"tool_call_streak": 3, "redirect_cap": 1})
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = "recovery-owner"
    agent._current_turn_id = "turn-a"
    agent._persist_disabled = False
    agent._interrupt_requested = False
    with bind_subagent_parent(agent):
        yield h, agent


def observe(h, agent, tool, args, result, **extra):
    from agent.inline_tool_executors import emit_terminal_post_tool_call

    emit_terminal_post_tool_call(
        agent, function_name=tool, function_args=args, result=json.dumps(result),
        effective_task_id="recovery-task", tool_call_id="observed-call", **extra)


def status(h):
    return json.loads(h.tool("loop_recovery", {"action": "status"},
                             session_id="recovery-owner", task_id="recovery-task"))

@pytest.fixture
def project(tmp_path, monkeypatch):
    """Real tmp workspace with the ledger on and this test's session cwd bound."""
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("VALUE = 1\n")
    (tmp_path / "src" / "b.py").write_text("OTHER = 2\n")
    monkeypatch.setattr("agent.runtime_cwd.resolve_context_cwd", lambda: tmp_path)
    from tools.terminal_tool import record_session_cwd
    record_session_cwd("recovery-task", str(tmp_path))
    yield tmp_path


def native_check(h, agent, project, command, ok):
    """Record a check through the actual native terminal producer, then emit
    the native result payload carrying the authoritative evidence id."""
    from agent.verification_evidence import record_terminal_result

    evidence = record_terminal_result(command=command, cwd=project,
                                       session_id=agent.session_id,
                                       exit_code=0 if ok else 1,
                                       output="1 passed" if ok else "1 failed")
    assert evidence is not None, "native ledger refused to record the check"
    observe(h, agent, "terminal", {"command": command},
            {"exit_code": evidence["exit_code"], "verification_evidence": {
                key: evidence[key] for key in
                ("id", "status", "kind", "scope", "canonical_command", "check_id")}})
    return evidence



def test_same_structured_failure_survives_argument_variation(recovery):
    from hermes_cli.plugins import get_pre_tool_call_directive

    h, agent = recovery
    for attempt in range(3):
        observe(h, agent, "terminal", {"command": f"fetch --attempt={attempt}"},
                {"ok": False, "code": "backend_unavailable", "error": "connection refused"})
    action, _ = get_pre_tool_call_directive(
        "terminal", {"command": "fetch --attempt=4"}, session_id=agent.session_id)
    assert action == "block"
    report = status(h)
    assert len({row["argument_digest"] for row in report["blocker"]["approaches"]}) == 3
    # Merely selecting a diagnostic directive does not spend delivery budget.
    assert report["delivered_interventions"] == 0


def test_blocker_label_is_concise_and_actionable(recovery):
    from hermes_cli.plugins import get_pre_tool_call_directive

    h, agent = recovery
    for attempt in range(3):
        observe(h, agent, "terminal", {"command": f"fetch --attempt={attempt}"},
                {"ok": False, "code": "backend_unavailable", "error": "connection refused"})
    action, message = get_pre_tool_call_directive(
        "terminal", {"command": "fetch --attempt=4"}, session_id=agent.session_id)
    assert action == "block"
    blocker = json.loads(message.split("LOOP GUARD: ", 1)[1])
    assert blocker["invariant"] == "repeated tool error (backend_unavailable)"
    assert blocker["next_step"] == \
        "loop_recovery hypothesis: name the assumption and one exact check."
    assert len(blocker["next_step"]) < 120  # concise, actionable text


def test_short_cycles_detected_but_polling_and_progress_are_not(recovery):
    from hermes_cli.plugins import get_pre_tool_call_directive

    h, agent = recovery
    for _ in range(3):
        observe(h, agent, "read_file", {"path": "a"}, {"content": "same a"})
        observe(h, agent, "process_manage", {"action": "poll", "pid": 8}, {"status": "running"})
        observe(h, agent, "read_file", {"path": "b"}, {"content": "same b"})
    assert get_pre_tool_call_directive("process_manage", {"action": "poll", "pid": 8},
                                       session_id=agent.session_id) == (None, None)
    assert get_pre_tool_call_directive("read_file", {"path": "a"},
                                       session_id=agent.session_id)[0] == "block"
    agent._current_turn_id = "turn-b"
    for content in ("first", "second", "third"):
        observe(h, agent, "read_file", {"path": "a"}, {"content": content})
    assert get_pre_tool_call_directive("read_file", {"path": "a"},
                                       session_id=agent.session_id) == (None, None)


def test_suppressed_directives_do_not_spend_recovery_budget(recovery):
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks

    h, agent = recovery
    args = {"path": "status.txt"}
    for _ in range(3):
        observe(h, agent, "read_file", args, {"content": "unchanged"})

    def stronger_policy(**_kwargs):
        return {"action": "block", "message": "policy refused", "directive_id": "test-policy"}

    callbacks = h.manager._hooks["pre_tool_call"]
    callbacks.insert(0, stronger_policy)
    try:
        _dispatch_pre_tool_call_hooks("read_file", args, session_id=agent.session_id,
                                      task_id="recovery-task", tool_call_id="suppressed-call")
    finally:
        callbacks.remove(stronger_policy)
    report = status(h)
    assert report["delivered_interventions"] == 0
    assert report["suppressed_interventions"] == 1
    _dispatch_pre_tool_call_hooks("read_file", args, session_id=agent.session_id,
                                  task_id="recovery-task", tool_call_id="delivered-call")
    report = status(h)
    assert report["delivered_interventions"] == 1
    assert report["remaining_interventions"] == 0
    # Exhaustion is not success, and doesn't silently drop the unresolved invariant.
    assert any(row.get("action") == "continue" for row in h.hook("pre_verify", session_id=agent.session_id))
    outputs = h.hook("transform_llm_output", response_text="Done", session_id=agent.session_id)
    assert any("Loop recovery unresolved:" in row for row in outputs if isinstance(row, str))


def test_hypothesis_permits_one_check_but_only_observed_pass_resolves(recovery, project):
    from hermes_cli.plugins import get_pre_tool_call_directive

    h, agent = recovery
    for _ in range(3):
        native_check(h, agent, project, "pytest -q tests/test_a.py", ok=False)
    check = {"command": "pytest -q tests/test_a.py"}
    proposal = json.loads(h.tool("loop_recovery", {
        "action": "hypothesis", "hypothesis": "the owner predicate is inverted",
        "next_tool": "terminal", "next_arguments": check,
        "expected_observation": "the owner-specific negative case passes after the predicate repair",
    }, session_id=agent.session_id))
    assert proposal["blocker"] is not None
    assert get_pre_tool_call_directive("terminal", check, session_id=agent.session_id) == (None, None)
    assert get_pre_tool_call_directive("terminal", check, session_id=agent.session_id)[0] == "block"
    # A different successful check cannot launder the failed invariant.
    native_check(h, agent, project, "pytest -q tests/test_b.py", ok=True)
    assert status(h)["blocker"] is not None
    native_check(h, agent, project, "pytest -q tests/test_a.py", ok=True)
    assert status(h)["blocker"] is None
    assert status(h)["observed_recoveries"] == 1


def test_native_pytest_target_mismatch_is_not_recovery(recovery, project):
    from hermes_cli.plugins import get_pre_tool_call_directive

    h, agent = recovery
    for _ in range(3):
        native_check(h, agent, project, "pytest -q tests/test_a.py", ok=False)
    assert get_pre_tool_call_directive(
        "terminal", {"command": "pytest -q tests/test_a.py"},
        session_id=agent.session_id)[0] == "block"
    # Same canonical command (pytest), different target: not recovery.
    native_check(h, agent, project, "pytest -q tests/test_b.py", ok=True)
    assert status(h)["blocker"] is not None
    # An id that cannot be resolved never attests, in either direction.
    observe(h, agent, "terminal", {"command": "pytest -q tests/test_a.py"},
            {"verification_evidence": {"id": 999999, "status": "passed",
                                       "canonical_command": "pytest"}})
    assert status(h)["blocker"] is not None
    # The native row lookup resolves THIS exact failed check and clears it.
    native_check(h, agent, project, "pytest -q tests/test_a.py", ok=True)
    assert status(h)["blocker"] is None
    assert status(h)["observed_recoveries"] == 1


def test_unrelated_patch_success_is_not_recovery(recovery, project):
    from hermes_cli.plugins import get_pre_tool_call_directive

    h, agent = recovery
    failing = {"mode": "replace", "path": "src/a.py",
               "old_string": "nope", "new_string": "x"}
    for _ in range(3):
        observe(h, agent, "patch", failing,
                {"success": False, "error": "Could not find old_string", "code": "not_found"})
    assert get_pre_tool_call_directive("patch", failing,
                                       session_id=agent.session_id)[0] == "block"
    observe(h, agent, "patch",
            {"mode": "replace", "path": "src/b.py",
             "old_string": "OTHER = 2", "new_string": "OTHER = 3"},
            {"success": True, "error": None})
    assert status(h)["blocker"] is not None
    observe(h, agent, "patch",
            {"mode": "replace", "path": "src/a.py",
             "old_string": "VALUE = 1", "new_string": "VALUE = 2"},
            {"success": True, "error": None})
    assert status(h)["blocker"] is None
    assert status(h)["observed_recoveries"] == 1


def test_repeat_blocker_clears_when_observed_result_changes(recovery):
    from hermes_cli.plugins import get_pre_tool_call_directive

    h, agent = recovery
    args = {"path": "status.txt"}
    for _ in range(3):
        observe(h, agent, "read_file", args, {"content": "unchanged"})
    assert get_pre_tool_call_directive("read_file", args,
                                       session_id=agent.session_id)[0] == "block"
    observe(h, agent, "read_file", args, {"content": "unchanged"})
    assert status(h)["blocker"] is not None
    assert status(h)["observed_recoveries"] == 0
    observe(h, agent, "read_file", args, {"content": "changed at last"})
    assert status(h)["blocker"] is None
    assert status(h)["observed_recoveries"] == 1


def test_capacity_reserve_release_and_delivery_ack(recovery):
    h, agent = recovery
    args = {"path": "status.txt"}
    for _ in range(3):
        observe(h, agent, "read_file", args, {"content": "unchanged"})
    first = h.hook("pre_tool_call", tool_name="read_file", args=args,
                   session_id=agent.session_id, tool_call_id="proposal-1")
    assert first[0]["action"] == "block"
    assert first[0]["directive_id"]
    # The pending reservation holds the cap-1 slot; a concurrent proposal
    # never reaches native arbitration and cannot double-spend.
    assert h.hook("pre_tool_call", tool_name="read_file", args=args,
                  session_id=agent.session_id, tool_call_id="proposal-2") == []
    # Suppression releases the reservation without spending budget.
    h.hook("post_tool_directive", delivered=False, action=None,
           selected_directive_id="", suppressed_directive_ids=[first[0]["directive_id"]],
           session_id=agent.session_id, tool_call_id="proposal-1")
    assert status(h)["delivered_interventions"] == 0
    assert status(h)["suppressed_interventions"] == 1
    # Capacity is free again and only the delivered acknowledgement spends it.
    second = h.hook("pre_tool_call", tool_name="read_file", args=args,
                    session_id=agent.session_id, tool_call_id="proposal-2")
    assert second[0]["action"] == "block"
    h.hook("post_tool_directive", delivered=True, action="block",
           selected_directive_id=second[0]["directive_id"],
           session_id=agent.session_id, tool_call_id="proposal-2")
    report = status(h)
    assert report["delivered_interventions"] == 1
    assert report["remaining_interventions"] == 0
    # Exhaustion is not success: the invariant stays visible.
    assert any(row.get("action") == "continue" for row in
               h.hook("pre_verify", session_id=agent.session_id))


def test_diagnostics_without_dispatch_ack_never_spend_budget(recovery):
    h, agent = recovery
    args = {"path": "status.txt"}
    for _ in range(3):
        observe(h, agent, "read_file", args, {"content": "unchanged"})
    proposal = h.hook("pre_tool_call", tool_name="read_file", args=args,
                      session_id=agent.session_id, tool_call_id="")
    assert proposal[0]["action"] == "block"
    assert "directive_id" not in proposal[0]
    # An ack for a host-generated id cannot credit this plugin's budget and
    # the unreserved proposal never occupied capacity in the first place.
    h.hook("post_tool_directive", delivered=True, action="block",
           selected_directive_id="host-abc", session_id=agent.session_id, tool_call_id="")
    report = status(h)
    assert report["delivered_interventions"] == 0
    assert report["remaining_interventions"] == 1
    second = h.hook("pre_tool_call", tool_name="read_file", args=args,
                    session_id=agent.session_id, tool_call_id="call-2")
    assert second[0]["action"] == "block" and "directive_id" in second[0]


@pytest.mark.parametrize("order", [
    ("continuation", "loop-guard"), ("loop-guard", "continuation")])
def test_output_labels_chain_in_both_registration_orders(load_plugin, order):
    import logging
    from run_agent import AIAgent
    from agent.subagent_lifecycle import bind_subagent_parent
    from agent.turn_finalizer import _apply_output_hooks
    from agent.inline_tool_executors import emit_terminal_post_tool_call

    handles = {}
    for name in order:
        handles[name] = load_plugin(name, {"tool_call_streak": 2} if name == "loop-guard" else None)
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = "chain-owner"
    agent.model = "test-model"
    agent.platform = "cli"
    agent._persist_disabled = False
    agent._interrupt_requested = False
    agent._current_turn_id = "turn-a"
    with bind_subagent_parent(agent):
        for _ in range(2):
            emit_terminal_post_tool_call(
                agent, function_name="read_file", function_args={"path": "status.txt"},
                result='{"content":"unchanged"}', effective_task_id="chain-task",
                tool_call_id="chain-call")
        # A real continuation blocker: the todo ledger is unavailable here, so
        # the completion gate truthfully records unverified.
        handles["continuation"].hook("pre_verify", coding=True, session_id="chain-owner",
                                      changed_paths=["src/a.py"], final_response="I am done")
        final, transformed, original = _apply_output_hooks(
            agent, "I am done", logging.getLogger("chain-test"), platform="cli",
            effective_task_id="chain-task", turn_id="turn-a",
            original_user_message="fix it", messages=[])
    assert transformed is True
    assert original == "I am done"
    assert final.startswith("I am done")
    assert "Completion unverified:" in final
    assert "Loop recovery unresolved:" in final


def test_recovering_later_error_does_not_clear_established_blocker(recovery):
    h, agent = recovery
    a, b = {"path": "a.py"}, {"path": "b.py"}
    for _ in range(3):
        observe(h, agent, "patch", a, {"error": "matching text not found"})
    established = status(h)["blocker"]
    assert established is not None
    observe(h, agent, "patch", b, {"error": "matching text not found"})
    observe(h, agent, "patch", b, {"success": True})
    assert status(h)["blocker"] == established
    assert status(h)["observed_recoveries"] == 0
    observe(h, agent, "patch", a, {"success": True})
    assert status(h)["blocker"] is None
    assert status(h)["observed_recoveries"] == 1
