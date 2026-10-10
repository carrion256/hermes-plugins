"""Evidence-based completion gate (plugins/continuation) vs the native
pre_verify stop-gate consumer.

Behavior contract: evidence and review are consumed natively — real
verification_evidence ledger (record_terminal_result /
mark_workspace_edited / verification_status), real native /review
dispatch through review_engine + delegate_task + the async_delegation
durable record rail. Review context is the authoritative pre_llm_call
capture (driven here through the actual turn_context producer), never
agent message attributes: the live agent transcript is empty during the
run, which is exactly the CLI smoke regression (native ValueError
"Nothing to review yet"). The fake sits exactly at the reviewer model
boundary (delegate_tool._build_child_agent / _run_single_child, host
test pattern). Missing evidence, disabled ledger, missing/malformed/
contradictory/canonical-misplaced verdicts, findings, same-path edits
after review, budget exhaustion, unreadable todo ledger, missing
capture, new task identity, dispatch failure, reset/stop — none of these
may complete silently; the honest outcome is labeled on the final
response via transform_llm_output.

One isolated home fixture owns HERMES_HOME (autouse): plugin manager,
ctx.state, evidence ledger and delegation records all share it; the
project fixture only provides tmp cwd, HERMES_VERIFY_ON_STOP and the
resolve_context_cwd boundary.
"""

import hashlib
import json
import threading
import time

import pytest

from agent.subagent_lifecycle import bind_subagent_parent
from agent.turn_context import _collect_pre_llm_call_context
from agent.verification_evidence import mark_workspace_edited, record_terminal_result
from hermes_cli.plugins import get_pre_verify_continue_message
from tools.async_delegation import get_durable_delegation
from hermes_cli.plugins_state import PluginState
from tools.process_registry import process_registry
from tools.todo_tool import TodoStore

SESSION = "sess-completion"
_NOTE = "Completion unverified:"
_TASK_TEXT = "fix the widget crash"


def _drain_completion_queue():
    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except Exception:
            return


def _wait_state(delegation_id, states, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = get_durable_delegation(delegation_id)
        if row is not None and row["state"] in states:
            return row
        time.sleep(0.05)
    raise AssertionError(f"delegation {delegation_id} never reached {states}")


def _verdict_text(verdict, findings=None):
    return "Review complete.\n" + json.dumps(
        {"review_verdict": verdict, "findings": findings or []})


def _run_observed_check(harness, agent, project, identity="pytest", call_id="tc", workdir=None,
                        session_id=None):
    """Drive current-owner pre/post hooks with a real native evidence row."""
    sid = session_id or agent.session_id
    args = {"command": identity}
    if workdir is not None:
        args["workdir"] = str(workdir)
    with bind_subagent_parent(agent):
        harness.hook("pre_tool_call", tool_name="terminal", session_id=sid,
                     tool_call_id=call_id, args=args)
        evidence = record_terminal_result(command=identity, cwd=workdir or project,
                                          session_id=sid, exit_code=0,
                                          output="1 passed")
        assert evidence is not None
        harness.hook("post_tool_call", tool_name="terminal", session_id=sid,
                     tool_call_id=call_id, args=args,
                     result={"exit_code": 0, "verification_evidence": {
                         "id": evidence["id"], "status": "passed",
                         "kind": "test", "canonical_command": evidence["canonical_command"]}})
    return evidence


def _canonical_source_revision(hashes):
    """Byte-exact contract with the external producers: sorted
    workspace-relative path + per-file streamed sha256."""
    canon = "".join(f"{rel}\0{hashes[rel]}\n" for rel in sorted(hashes))
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


class FakeAgent:
    """Live agents have empty transcript attributes during the run; this
    fake has none at all — review context must come from capture."""

    def __init__(self, todos=None):
        self.session_id = SESSION
        self.platform = "cli"
        self.model = "test-model"
        self._todo_store = TodoStore()
        if todos:
            self._todo_store.write(todos)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Tmp project cwd + ledger policy only; HERMES_HOME comes from the
    isolated fixture home, which manager, plugin state, evidence ledger and
    delegation records all share. Native workspace resolution points at
    the tmp project."""
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "widget.py").write_text("VALUE = 1\n")
    monkeypatch.setattr("agent.runtime_cwd.resolve_context_cwd", lambda: tmp_path)
    yield tmp_path
    _drain_completion_queue()


@pytest.fixture
def reviewer(monkeypatch):
    """Fake ONLY the reviewer model boundary; all rails stay real."""
    import tools.delegate_tool as dt
    from unittest.mock import MagicMock

    gate = threading.Event()
    gate.set()  # default: reviewer completes immediately
    control = {"summary": _verdict_text("clean"), "gate": gate, "built": {}}

    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    creds = {k: None for k in
             ("model", "provider", "base_url", "api_key", "api_mode", "command", "args")}

    def fake_build(**kw):
        control["built"].update(kw)
        return fake_child

    def fake_run(task_index, goal, child=None, parent_agent=None, **kw):
        assert gate.wait(5), "reviewer runner blocked too long"
        return {"task_index": 0, "status": "completed", "summary": control["summary"],
                "api_calls": 1, "duration_seconds": 0.1, "model": "m",
                "exit_reason": "completed"}

    monkeypatch.setattr(dt, "_build_child_agent", fake_build)
    monkeypatch.setattr(dt, "_run_single_child", fake_run)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    monkeypatch.setattr("agent.review_engine._load_review_credentials_cfg", lambda: None)
    return control


@pytest.fixture
def agent():
    return FakeAgent()


def _capture(agent, task_id="task-1", turn_id="turn-1"):
    """Drive the ACTUAL pre_llm_call producer (turn_context) so the plugin's
    capture runs on the authoritative hook payload shape."""
    with bind_subagent_parent(agent):
        _collect_pre_llm_call_context(
            agent, effective_task_id=task_id, turn_id=turn_id,
            original_user_message=_TASK_TEXT,
            messages=[{"role": "user", "content": _TASK_TEXT}],
            conversation_history=None)


def _hook(harness, agent, coding=True, changed_paths=None, attempt=0):
    payload = {
        "session_id": SESSION, "coding": coding, "attempt": attempt,
        "platform": "cli", "model": "m", "final_response": "I'm done.",
        "changed_paths": changed_paths if changed_paths is not None else ["widget.py"],
    }
    with bind_subagent_parent(agent):
        return harness.hook("pre_verify", **payload)


def _review_id(harness):
    record = harness.context.state.get(f"session:{SESSION}") or {}
    return record.get("review", {}).get("delegation_id")


def _verify_ok(project):
    record_terminal_result(command="pytest", cwd=project, session_id=SESSION,
                           exit_code=0, output="1 passed")


# --- native consumer contract -------------------------------------------------


def test_host_consumer_receives_directive(project, load_plugin, agent):
    """The real host consumer (get_pre_verify_continue_message) receives and
    accepts the plugin directive; missing evidence steers, nothing completes."""
    load_plugin("continuation")
    with bind_subagent_parent(agent):
        msg = get_pre_verify_continue_message(
            session_id=SESSION, platform="cli", model="m", coding=True, attempt=0,
            changed_paths=[str(project / "widget.py")], final_response="I'm done.")
    assert msg and "verification" in msg.lower()


def test_non_coding_turn_passes_through(project, load_plugin, agent):
    harness = load_plugin("continuation")
    results = _hook(harness, agent, coding=False,
                    changed_paths=[str(project / "widget.py")])
    assert results == []


def test_ledger_disabled_is_unverified_not_pass(project, load_plugin, agent, monkeypatch):
    harness = load_plugin("continuation")
    monkeypatch.delenv("HERMES_VERIFY_ON_STOP")
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert len(results) == 1 and results[0]["action"] == "continue"
    assert "verification" in results[0]["message"].lower()
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0]
    assert "disabled" in labeled[0]


# --- remaining-task criteria (native todo ledger) ------------------------------


def test_pending_todo_blocks_and_persists_blockers(project, load_plugin, reviewer):
    harness = load_plugin("continuation")
    agent = FakeAgent(todos=[{"id": "1", "content": "apply the fix", "status": "pending"}])
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "apply the fix" in results[0]["message"]
    assert reviewer["built"] == {}, "todo gate must fire before any review dispatch"
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled and "unfinished tasks" in labeled[0], \
        "todo blockers persist so a cap release is never a plain 'done'"


def test_unreadable_todo_ledger_is_unverified(project, load_plugin, agent):
    harness = load_plugin("continuation")

    class BrokenStore:
        def read(self):
            raise RuntimeError("ledger gone")

    agent._todo_store = BrokenStore()
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "unverified" in results[0]["message"]
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0]


# --- fresh native verification (real evidence ledger) --------------------------


def test_missing_capture_blocks_review_not_silently(project, load_plugin, agent, reviewer):
    """CLI smoke regression: without the pre_llm_call capture the gate must
    block explicitly — no native dispatch, no silent finish."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "capture" in results[0]["message"].lower()
    assert reviewer["built"] == {}, "dispatch requires the actual captured task"
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0]


def test_missing_then_fresh_evidence(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "unverified" in results[0]["message"]
    assert reviewer["built"] == {}

    _verify_ok(project)
    _capture(agent)
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "review" in results[0]["message"].lower()
    delegation_id = _review_id(harness)
    assert delegation_id
    assert _wait_state(delegation_id, ("running", "completed")) is not None


def test_stale_evidence_gates_finish(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    mark_workspace_edited(session_id=SESSION, cwd=project,
                          paths=[str(project / "widget.py")])
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "stale" in results[0]["message"]


def test_failed_evidence_gates_finish(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    record_terminal_result(command="pytest", cwd=project, session_id=SESSION,
                           exit_code=1, output="1 failed")
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "failed" in results[0]["message"]


# --- independent native /review with structured verdict ------------------------


def test_producer_capture_drives_real_review(project, load_plugin, agent, reviewer):
    """Regression for the CLI smoke failure: native /review runs on the
    actual pre_llm_call capture (real producer payload) even though the
    bound agent has no transcript attributes — no 'Nothing to review yet'."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent, task_id="task-real", turn_id="turn-real")
    record = harness.context.state.get(f"session:{SESSION}") or {}
    assert record["task"]["task_id"] == "task-real"
    assert record["task"]["identity"] == "turn:turn-real"
    assert record["task"]["user_message"] == _TASK_TEXT
    assert record["task"]["excerpt"], "pre-LLM conversation must be captured"
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert reviewer["built"], "review dispatched on captured context"
    assert _review_id(harness)


def test_clean_verdict_verified_finish(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    reviewer["summary"] = _verdict_text("clean")
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled == [], "verified finish carries no unverified label"


def test_new_task_identity_invalidates_stale_review(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent, task_id="task-x", turn_id="turn-1")
    reviewer["summary"] = _verdict_text("clean")
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    old_id = _review_id(harness)
    _wait_state(old_id, ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []

    # second genuine user turn on the SAME terminal task_id: terminal
    # task_id is not identity; the native hook turn_id is.
    _capture(agent, task_id="task-x", turn_id="turn-2")
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results, "new user turn identity invalidates the old clean review"
    new_id = _review_id(harness)
    assert new_id and new_id != old_id, "invalidated review must dispatch its own delegation"
    record = harness.context.state.get(f"session:{SESSION}") or {}
    assert record["review"]["identity"] == "turn:turn-2"


def test_message_digest_fallback_identity(project, load_plugin, agent, reviewer):
    """No native turn_id: the user message digest is the identity fallback —
    different genuine user messages invalidate even with empty turn_id."""
    harness = load_plugin("continuation")
    _verify_ok(project)

    def _capture_no_turn(text):
        with bind_subagent_parent(agent):
            _collect_pre_llm_call_context(
                agent, effective_task_id="default", turn_id="",
                original_user_message=text,
                messages=[{"role": "user", "content": text}],
                conversation_history=None)

    _capture_no_turn(_TASK_TEXT)
    reviewer["summary"] = _verdict_text("clean")
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []

    _capture_no_turn("actually, also fix the logging")
    record = harness.context.state.get(f"session:{SESSION}") or {}
    assert str(record["task"]["identity"]).startswith("msg:")
    assert record.get("review") in ({}, None), "different user message invalidates the review"


def test_pending_review_holds_turn(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    reviewer["gate"].clear()  # hold the reviewer runner
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    delegation_id = _review_id(harness)
    assert _wait_state(delegation_id, ("running",))["state"] == "running"
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "running" in results[0]["message"]
    record = harness.context.state.get(f"session:{SESSION}") or {}
    assert record["review"]["identity"] == record["task"]["identity"], \
        "internal pre_verify rounds re-verify identity; the capture never changes"
    reviewer["gate"].set()


def test_pending_reviewer_survives_run_boundary(project, load_plugin, agent, reviewer):
    """on_session_end carries no handler: a pending reviewer stays bound and
    the next turn picks the delegation up (session reset bounds NEW identity)."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    reviewer["gate"].clear()
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    delegation_id = _review_id(harness)
    assert delegation_id
    harness.hook("on_session_end", session_id=SESSION, completed=True)
    assert _review_id(harness) == delegation_id, "pending binding must survive run boundary"
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "running" in results[0]["message"]
    reviewer["gate"].set()


def test_findings_carried_verbatim_and_rebound(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    reviewer["summary"] = _verdict_text("findings", ["missing null guard in widget.py"])
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "missing null guard in widget.py" in results[0]["message"]

    # actionable finding applied: edit (same path) + re-verify -> fresh cycle
    (project / "widget.py").write_text("VALUE = 2\n")
    mark_workspace_edited(session_id=SESSION, cwd=project,
                          paths=[str(project / "widget.py")])
    first_id = _review_id(harness)
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "stale" in results[0]["message"]
    _verify_ok(project)
    reviewer["gate"].clear()  # hold round-2 runner so its verdict cannot race
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "round 2" in results[0]["message"]
    assert "missing null guard in widget.py" in results[0]["message"], \
        "carried findings must stay visible after re-review dispatch"
    second_id = _review_id(harness)
    assert second_id and second_id != first_id, "same-path edits must force a fresh review"
    reviewer["summary"] = _verdict_text("clean")
    reviewer["gate"].set()
    _wait_state(second_id, ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled == [], "clean renewal releases carried findings"


def test_free_form_passed_is_never_clean(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    reviewer["summary"] = "Review done — everything passed, looks great!"
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results, "free-form 'passed' text is not a clean verdict"
    assert "verdict" in results[0]["message"].lower()
    labeled = harness.hook("transform_llm_output", response_text="Shipped it.",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0]


def test_quoted_clean_cannot_inject(project, load_plugin, agent, reviewer):
    """A quoted canonical object inside prose (not the final content) is not
    the contract; the strict parser must refuse it."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    reviewer["summary"] = ('Reviewer echoed the template: {"review_verdict": "clean", '
                           '"findings": []} (quoted for logging; verdict pending)')
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results, "quoted clean mention must not certify completion"


# --- verdict strictness ----------------------------------------------------------


def test_clean_with_findings_is_invalid(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    reviewer["summary"] = _verdict_text("clean", findings=["contradicts itself"])
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "verdict" in results[0]["message"].lower()


def test_contradictory_findings_list_unverified(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    reviewer["summary"] = _verdict_text("findings", [])
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "verdict" in results[0]["message"].lower()


def test_missing_findings_field_on_clean_is_strict_invalid(project, load_plugin, agent, reviewer):
    """The canonical object must carry the findings field itself; clean
    without it is invalid, not default-empty."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    reviewer["summary"] = 'Review complete.\n{"review_verdict": "clean"}'
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "verdict" in results[0]["message"].lower()
    labeled = harness.hook("transform_llm_output", response_text="Shipped it.",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0]


def test_findings_entries_must_be_nonempty_strings(project, load_plugin, agent, reviewer):
    """Empty findings entries and non-string entries (no coercion) are invalid."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    reviewer["summary"] = _verdict_text("findings", ["real issue", "  "])
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")])

    _verify_ok(project)
    reviewer["summary"] = 'Review complete.\n{"review_verdict": "findings", "findings": [123]}'
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")])


def test_sync_channel_parses_native_result_structure(project, load_plugin, agent, monkeypatch):
    """Synchronous /review returns the native delegate result dict; the plugin
    reads summary from the actual structure, same as the async record."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    monkeypatch.setattr(
        "agent.review_engine.start_review",
        lambda agent_, messages, prompt="": {"results": [{"status": "completed", "summary":
                                                          _verdict_text("clean")}]},
    )
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []


def test_sync_channel_findings_block(project, load_plugin, agent, monkeypatch):
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    monkeypatch.setattr(
        "agent.review_engine.start_review",
        lambda agent_, messages, prompt="": {"results": [{"status": "completed", "summary":
                                                          _verdict_text("findings", ["bad guard"])}]},
    )
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "bad guard" in results[0]["message"]


# --- bounded rounds, honest release ---------------------------------------------


def test_review_dispatch_failure_blocks_labeled(project, load_plugin, agent, monkeypatch):
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)

    def refuse(agent_, messages, prompt=""):
        raise ValueError("dispatch refused")

    monkeypatch.setattr("agent.review_engine.start_review", refuse)
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "dispatch" in results[0]["message"].lower()
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0]


def test_round_budget_releases_labeled(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation", settings={"max_review_rounds": 1})
    _verify_ok(project)
    _capture(agent)
    reviewer["summary"] = _verdict_text("findings", ["bad guard"])
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "bad guard" in results[0]["message"]

    # evidence moves again: review invalidation would need round 2, but the
    # budget is spent — release happens only as a labeled blocked outcome.
    (project / "widget.py").write_text("VALUE = 3\n")
    mark_workspace_edited(session_id=SESSION, cwd=project,
                          paths=[str(project / "widget.py")])
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "stale" in results[0]["message"]
    assert "bad guard" in results[0]["message"], "carried findings survive the verification hold"
    _verify_ok(project)
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []
    labeled = harness.hook("transform_llm_output", response_text="Done!",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0] and "bad guard" in labeled[0]
    assert "budget exhausted" in labeled[0] and "current evidence" in labeled[0], \
        "cap label truthfully reports review outstanding on the new snapshot"
    assert "stale" not in labeled[0], "fresh verification must not be relabeled stale"


# --- session lifecycle cleanup --------------------------------------------------


def test_reset_clears_review_state(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    first_id = _review_id(harness)
    harness.hook("on_session_reset", session_id=SESSION)
    assert harness.context.state.get(f"session:{SESSION}") is None
    # fresh session identity: the new task captures its own context again
    _capture(agent, task_id="task-new", turn_id="turn-new")
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "round 1" in results[0]["message"]
    assert _review_id(harness) != first_id


def test_user_stop_clears_blockers(project, load_plugin, agent):
    harness = load_plugin("continuation")
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])  # missing evidence
    harness.hook("agent_loop_stopped", session_key=SESSION, reason="user /stop")
    labeled = harness.hook("transform_llm_output", response_text="stopped.",
                           session_id=SESSION, model="m")
    assert labeled == [], "stopped session outcome carries no stale blocker label"


def test_transform_uses_memory_marker_when_store_fails(project, load_plugin, agent, monkeypatch):
    """Review: durable state failure must never yield an unlabelled
    completion. The store is faulted at the REAL class boundary (the plugin
    registration closure holds its own PluginContext, so swapping the test
    harness context would not reach the plugin); the gate's marker is
    consulted BEFORE the failing store access."""
    harness = load_plugin("continuation")
    _capture(agent)
    calls = {"get": 0, "set": 0}

    def broken_get(self, *args, **kwargs):
        calls["get"] += 1
        raise RuntimeError("state store unavailable")

    def broken_set(self, *args, **kwargs):
        calls["set"] += 1
        raise RuntimeError("state store unavailable")

    monkeypatch.setattr(PluginState, "get", broken_get)
    monkeypatch.setattr(PluginState, "set", broken_set)
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert calls["set"] >= 1, "the gate must actually hit the failing store"
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0], \
        "store failure still yields an honest unverified label via the in-memory marker"


def test_clean_finish_clears_provisional_marker(project, load_plugin, agent, monkeypatch):
    """The provisional pending marker is cleared only on the authoritative
    clean/current binding; a clean synchronous review must leave NO label."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    monkeypatch.setattr(
        "agent.review_engine.start_review",
        lambda agent_, messages, prompt="": {
            "results": [{"status": "completed", "summary": _verdict_text("clean")}]},
    )
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []
    labeled = harness.hook("transform_llm_output", response_text="Done.",
                           session_id=SESSION, model="m")
    assert labeled == [], "clean renewal clears the provisional marker; completion is verified"


def test_real_dispatcher_timeout_keeps_answer_labeled(project, load_plugin, agent,
                                                      monkeypatch):
    """Review regression: real dispatcher with a short callback timeout and a
    synchronously blocked native review. The answer emitted while the reviewer
    is stuck stays explicitly unverified (plugin callbacks are abandoned
    fail-open; the pending state is persisted before the blocking I/O)."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.5)
    release = threading.Event()

    def blocked_start(agent_, messages, prompt=""):
        release.wait(5)
        return {"results": [{"status": "completed", "summary": _verdict_text("clean")}]}

    monkeypatch.setattr("agent.review_engine.start_review", blocked_start)
    with bind_subagent_parent(agent):
        results = harness.hook("pre_verify", session_id=SESSION, coding=True, attempt=0,
                               platform="cli", model="m", final_response="I'm done.",
                               changed_paths=[str(project / "widget.py")])
    assert results == [], "dispatcher abandons the stuck callback at the timeout cap"
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0], \
        "reviewer stuck -> answer labeled unverified, never presented as complete"
    release.set()


def test_transform_labels_promptly_during_stalled_store(project, load_plugin, agent, monkeypatch):
    """Review regression (P1): a stalled durable store holds the _save
    transaction lock PAST the hook callback timeout. transform_llm_output
    runs while the stall Event is HELD and must label promptly and
    conservatively — non-blocking lock acquisition, immediate pending marker,
    never waiting on the stalled transaction and never consulting the
    durable store. The dispatcher cap itself proves promptness: if transform
    blocked, the bounded dispatcher abandons it and labeled stays empty."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.5)

    entered = threading.Event()
    hold = threading.Event()
    original_set = PluginState.set

    def stalling_set(self, *args, **kwargs):
        if not entered.is_set():
            entered.set()
            hold.wait(10)
        return original_set(self, *args, **kwargs)

    monkeypatch.setattr(PluginState, "set", stalling_set)
    monkeypatch.setattr(
        "agent.review_engine.start_review",
        lambda agent_, messages, prompt="": {
            "results": [{"status": "completed", "summary": _verdict_text("clean")}]},
    )

    def stalled_callback():
        with bind_subagent_parent(agent):
            harness.hook("pre_verify", session_id=SESSION, coding=True, attempt=0,
                         platform="cli", model="m", final_response="I'm done.",
                         changed_paths=[str(project / "widget.py")])

    stalled = threading.Thread(target=stalled_callback, daemon=True)
    stalled.start()
    assert entered.wait(2.0), "writer never reached the stalled ctx.state.set"

    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0], \
        "stalled transaction must not delay or silence the unverified label"
    assert "pending" in labeled[0], "conservative contention label reports honest pending state"
    hold.set()
    stalled.join(5.0)


def test_stale_save_cannot_clobber_newer_turn(project, load_plugin, agent, monkeypatch):
    """Review regression: the stale writer is parked INSIDE ctx.state.set
    after its owner check passed (the exact race), the genuine new turn then
    takes over, and the final durable state is the new turn's. The old
    non-transactional code fails this test deterministically (stale durable
    write and marker land after the takeover); the transactional gate
    serializes ownership check, durable write and marker mirror so the
    takeover either fences the writer or cleanly overwrites it."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent, task_id="task-old", turn_id="turn-old")

    entered = threading.Event()
    release = threading.Event()
    original_set = PluginState.set

    def stalling_set(self, *args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(10), "stale save was never released"
        return original_set(self, *args, **kwargs)

    monkeypatch.setattr(PluginState, "set", stalling_set)
    monkeypatch.setattr(
        "agent.review_engine.start_review",
        lambda agent_, messages, prompt="": {
            "results": [{"status": "completed", "summary": _verdict_text("clean")}]},
    )
    stale_done = threading.Event()

    def stale_callback():
        with bind_subagent_parent(agent):
            harness.hook("pre_verify", session_id=SESSION, coding=True, attempt=0,
                         platform="cli", model="m", final_response="I'm done.",
                         changed_paths=[str(project / "widget.py")])
        stale_done.set()

    stale = threading.Thread(target=stale_callback, daemon=True)
    stale.start()
    assert entered.wait(2.0), "stale writer never reached ctx.state.set"

    takeover_done = threading.Event()

    def new_turn():
        _capture(agent, task_id="task-new", turn_id="turn-new")
        takeover_done.set()

    new_thread = threading.Thread(target=new_turn, daemon=True)
    new_thread.start()
    time.sleep(0.2)  # takeover is parked waiting for the transaction (or done already)
    release.set()
    stale.join(5.0)
    new_thread.join(5.0)
    assert stale_done.is_set() and takeover_done.is_set()

    record = harness.context.state.get(f"session:{SESSION}") or {}
    assert str((record.get("task") or {}).get("identity") or "") == "turn:turn-new", \
        "new genuine turn owns the durable state"
    assert record.get("review", {}) == {}, "stale review binding cannot survive takeover"
    assert int(record.get("rounds", 0)) == 0, "stale round consumption cannot clobber the new budget"
    assert record.get("blockers") in (None, []), "stale blocker cannot clobber the new turn"
    labeled = harness.hook("transform_llm_output", response_text="New turn.",
                           session_id=SESSION, model="m")
    assert labeled == [], "the new turn's output carries no stale unverified label"


def test_findings_verdict_containing_braces_is_retained(project, load_plugin, agent, reviewer):
    """Review regression: canonical findings whose finding text contains
    braces (e.g. `if (ready) { ... }`) must parse strictly and travel
    verbatim, not degrade to a parse-error blocker."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    finding = "Guard the if (ready) { body } before the write"
    reviewer["summary"] = _verdict_text("findings", [finding])
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and finding in results[0]["message"], \
        "brace-bearing finding must reach the conversation verbatim"
    labeled = harness.hook("transform_llm_output", response_text="Hmm.",
                           session_id=SESSION, model="m")
    assert labeled and finding in labeled[0]


def test_genuine_turn_resets_round_budget(project, load_plugin, agent, reviewer):
    """Review regression: terminal task_id is not the identity — two genuine
    user turns carrying the SAME task_id must each get their own bounded
    review budget (the second turn must not inherit an exhausted one)."""
    harness = load_plugin("continuation", settings={"max_review_rounds": 1})
    _verify_ok(project)
    _capture(agent, task_id="task-x", turn_id="turn-1")
    reviewer["summary"] = _verdict_text("findings", ["bad guard"])
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    _verify_ok(project)
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []
    labeled = harness.hook("transform_llm_output", response_text="Done!",
                           session_id=SESSION, model="m")
    assert labeled and "budget exhausted" in labeled[0]

    # second genuine turn: same terminal task_id (commonly 'default'), the
    # producer-side turn identity differs -> fresh budget
    _capture(agent, task_id="task-x", turn_id="turn-2")
    first_review = _review_id(harness)
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "round 1/1" in results[0]["message"], \
        "new genuine turn gets its own review round, not the exhausted budget"
    assert _review_id(harness) != first_review


# --- explicit acceptance ledger (declare/bind/status/disposition) ------------


def _coverage_verdict(verdict, coverage, findings=None):
    return "Review complete.\n" + json.dumps(
        {"review_verdict": verdict, "findings": findings or [],
         "coverage": coverage})


def _tool(harness, **call):
    return json.loads(harness.tool("acceptance", call, session_id=SESSION))


def _declare(harness, cid="c1", identity="pytest", paths=("widget.py",),
             observable=None):
    return _tool(harness, action="declare", id=cid,
                 observable=observable or f"{cid} observable",
                 check_kind="terminal",
                 check_identity=identity, paths=list(paths))


def test_declare_requires_explicit_check_and_source_paths(project, load_plugin, agent):
    harness = load_plugin("continuation")
    bad = _tool(harness, action="declare", id="c1", observable="x")
    assert bad["success"] is False and "check_kind" in bad["error"]
    assert _declare(harness, paths=[])["success"] is False
    bad = _declare(harness, paths=["/etc/passwd"])
    assert bad["success"] is False and "workspace-relative" in bad["error"]
    assert _declare(harness)["success"] is True


def test_unrelated_green_check_does_not_satisfy_requirement(
        project, load_plugin, agent, reviewer):
    """A generic green pytest run cannot launder an unrelated requirement:
    the evidence's check identity must match the declared check."""
    harness = load_plugin("continuation")
    evidence = record_terminal_result(command="pytest", cwd=project,
                                      session_id=SESSION, exit_code=0,
                                      output="1 passed")
    assert _declare(harness, cid="1", identity="pytest tests/test_widget.py -q",
                    paths=("widget.py",))["success"] is True
    bound = _tool(harness, action="bind", id="1", evidence_id=evidence["id"])
    assert bound["success"] is False and "check identity" in bound["error"]
    crit = {c["id"]: c for c in _tool(harness, action="status")["criteria"]}["1"]
    assert crit["status"] == "unverified" and crit["evidence_id"] is None


def test_stale_evidence_is_rejected_from_binding(
        project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    evidence = record_terminal_result(command="pytest", cwd=project,
                                      session_id=SESSION, exit_code=0)
    mark_workspace_edited(session_id=SESSION, cwd=project, paths=["widget.py"])
    assert _declare(harness, cid="1")["success"] is True
    bound = _tool(harness, action="bind", id="1", evidence_id=evidence["id"])
    assert bound["success"] is False and "stale" in bound["error"]


def test_cross_session_evidence_id_rejected(project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    evidence = record_terminal_result(command="pytest", cwd=project,
                                      session_id="another-session", exit_code=0)
    assert _declare(harness, cid="1")["success"] is True
    bound = _tool(harness, action="bind", id="1", evidence_id=evidence["id"])
    assert bound["success"] is False
    crit = {c["id"]: c for c in _tool(harness, action="status")["criteria"]}["1"]
    assert crit["status"] == "unverified"


def test_source_mutation_around_observed_check_prohibits_binding(
        project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1", identity="pytest",
                    paths=("widget.py",))["success"] is True
    with bind_subagent_parent(agent):
        harness.hook("pre_tool_call", tool_name="terminal", session_id=SESSION,
                     tool_call_id="tc-1", args={"command": "pytest"})
        (project / "widget.py").write_text("VALUE = 42\n")  # check mutates source
        evidence = record_terminal_result(command="pytest", cwd=project,
                                          session_id=SESSION, exit_code=0)
        harness.hook("post_tool_call", tool_name="terminal", session_id=SESSION,
                     tool_call_id="tc-1",
                     result={"exit_code": 0, "verification_evidence": {
                         "id": evidence["id"], "status": "passed",
                         "kind": "test", "canonical_command": "pytest"}})
    bound = _tool(harness, action="bind", id="1", evidence_id=evidence["id"])
    assert bound["success"] is False and "observed check execution" in bound["error"], \
        "source mutated during the observed check: no genuine pre/post pair exists"


def test_source_mutation_after_bind_unverifies_evidence(
        project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    _capture(agent)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    (project / "widget.py").write_text("VALUE = 99\n")
    assert _tool(harness, action="status")["criteria"][0]["status"] == "stale_evidence"
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "no longer certifies" in results[0]["message"]


def test_reviewer_clean_without_coverage_assessment_is_unverified(
        project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    reviewer["summary"] = _verdict_text("clean")  # coverage field missing
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "unverified" in results[0]["message"].lower()
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0]


def test_completed_todo_requires_coverage_of_its_outcome(
        project, load_plugin, reviewer):
    """Native todo outcomes are reviewable REQUIRED outcomes: a completed
    todo without evidence cannot be laundered clean by a reviewer assertion —
    the evidence gate fires BEFORE the reviewer dispatches."""
    harness = load_plugin("continuation")
    agent = FakeAgent(todos=[{"id": "9", "content": "done thing",
                              "status": "completed"}])
    _verify_ok(project)
    _capture(agent)
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "9" in results[0]["message"] \
        and "criterion" in results[0]["message"].lower(), \
        "todo 9 needs a declared criterion + evidence; reviewer cannot cover it"
    assert reviewer["built"] == {}, "evidence gate precedes review dispatch"
    assert _declare(harness, cid="9")["success"] is True
    evidence9 = _run_observed_check(harness, agent, project, call_id="tc-9")
    assert _tool(harness, action="bind", id="9",
                 evidence_id=evidence9["id"])["success"] is True
    reviewer["summary"] = _coverage_verdict("clean", {"1": "covered"})  # todo 9 unassessed
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "unverified" in results[0]["message"].lower()
    reviewer["summary"] = _coverage_verdict("clean", {"1": "covered", "9": "covered"})
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []


def test_acceptance_evidence_and_coverage_certify_completion(
        project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    _capture(agent)
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    reviewer["summary"] = _coverage_verdict("clean", {"1": "covered"})
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled == [], "verified finish carries no unverified label"


def test_blocked_disposition_stays_visible_blocker(project, load_plugin, agent):
    harness = load_plugin("continuation")
    _verify_ok(project)
    assert _declare(harness, cid="1")["success"] is True
    assert _tool(harness, action="disposition", id="1", disposition="blocked",
                 reason="upstream contract missing")["success"] is True
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "upstream contract missing" in results[0]["message"]
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled and "blocked" in labeled[0]
    assert _tool(harness, action="disposition", id="1",
                 disposition="clear")["success"] is True


def test_cancelled_criterion_is_visible_never_completion(
        project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    _capture(agent)
    _tool(harness, action="bind", id="1", evidence_id=evidence["id"])
    assert _tool(harness, action="disposition", id="1", disposition="cancelled",
                 reason="requirement dropped by user")["success"] is True
    crit = _tool(harness, action="status")["criteria"][0]
    assert crit["status"] == "cancelled" and crit["evidence_id"] is None, \
        "cancellation drops the bound evidence and stays visible"
    reviewer["summary"] = _coverage_verdict("clean", {"1": "covered"})
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results, "cancelled criterion may not be laundered as covered"
    reviewer["summary"] = _coverage_verdict("clean", {"1": "cancelled"})
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0] and "cancelled" in labeled[0], \
        "agent-cancelled work releases bounded and labeled unverified, never clean"


def test_new_requirement_invalidates_durable_review_binding(
        project, load_plugin, agent, reviewer):
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    _capture(agent)
    _tool(harness, action="bind", id="1", evidence_id=evidence["id"])
    reviewer["summary"] = _coverage_verdict("clean", {"1": "covered"})
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []
    # new requirement AFTER the clean review: the durable binding is
    # invalidated — the old clean verdict cannot cover the new criterion.
    assert _declare(harness, cid="2")["success"] is True
    evidence2 = _run_observed_check(harness, agent, project, call_id="tc-2")
    assert _tool(harness, action="bind", id="2",
                 evidence_id=evidence2["id"])["success"] is True
    reviewer["summary"] = _coverage_verdict("clean", {"1": "covered", "2": "covered"})
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "round 2" in results[0]["message"]
    _wait_state(_review_id(harness), ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []


def test_green_check_before_declaration_cannot_be_relabelled(
        project, load_plugin, agent):
    """A green check executed BEFORE the criterion existed was never an
    observed execution of that criterion — it can never be bound, even
    though its command, session, and freshness all match."""
    harness = load_plugin("continuation")
    evidence = record_terminal_result(command="pytest", cwd=project,
                                      session_id=SESSION, exit_code=0,
                                      output="1 passed")
    assert _declare(harness, cid="1", identity="pytest",
                    paths=("widget.py",))["success"] is True
    bound = _tool(harness, action="bind", id="1", evidence_id=evidence["id"])
    assert bound["success"] is False and "observed check execution" in bound["error"]
    # rerunning the declared check after declaration produces the genuine
    # pre/post pair, and exactly that fresh evidence binds.
    fresh = _run_observed_check(harness, agent, project, call_id="tc-rerun")
    assert _tool(harness, action="bind", id="1",
                 evidence_id=fresh["id"])["success"] is True


def test_restart_drops_observations_and_requires_rerun(project, load_plugin, agent):
    """Observations are per-process: after a restart the old evidence id has
    no pre/post pair anymore — binding fails and the declared check must be
    rerun; there is no current-hash fallback."""
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    # simulated restart: fresh plugin state; the durable requirement is
    # re-declared identically, but the per-process observation is gone.
    fresh_harness = load_plugin("continuation")
    assert _declare(fresh_harness, cid="1")["success"] is True
    bound = _tool(fresh_harness, action="bind", id="1", evidence_id=evidence["id"])
    assert bound["success"] is False and "observed check execution" in bound["error"]
    # the declared check is rerun; exactly that fresh evidence binds.
    rerun = _run_observed_check(fresh_harness, agent, project, call_id="tc-after")
    assert _tool(fresh_harness, action="bind", id="1",
                 evidence_id=rerun["id"])["success"] is True


def test_missing_declared_source_cannot_certify(project, load_plugin, agent):
    """Declared sources that are missing/unreadable can never certify: the
    pre snapshot is not certifiable, so no genuine observation exists."""
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1", identity="pytest",
                    paths=("absent-implementation.py",))["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    bound = _tool(harness, action="bind", id="1", evidence_id=evidence["id"])
    assert bound["success"] is False and "observed check execution" in bound["error"]


def test_symlink_escape_cannot_certify(project, load_plugin, agent):
    """read_bytes must follow no symlink outside the workspace: the RESOLVED
    path is confinement-checked, so an escaping source cannot certify."""
    harness = load_plugin("continuation")
    outside = project.parent / "outside-workspace.py"
    outside.write_text("SECRET = 1\n")
    (project / "link.py").symlink_to(outside)
    assert _declare(harness, cid="1", identity="pytest",
                    paths=("link.py",))["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    bound = _tool(harness, action="bind", id="1", evidence_id=evidence["id"])
    assert bound["success"] is False and "observed check execution" in bound["error"]


def test_workspace_change_cannot_rebind_criteria(
        project, load_plugin, agent, reviewer, monkeypatch, tmp_path):
    """The criterion is pinned to its declaring workspace; a changed cwd
    cannot rebind the evidence to the new tree — the evidence becomes
    unverified instead."""
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    _capture(agent)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    other = tmp_path / "different-tree"
    other.mkdir()
    (other / "widget.py").write_text("VALUE = 1\n")
    monkeypatch.setattr("agent.runtime_cwd.resolve_context_cwd", lambda: other)
    results = _hook(harness, agent, changed_paths=[str(other / "widget.py")])
    assert results and "no longer certifies" in results[0]["message"]


def test_external_scenario_binds_real_source_revision(
        project, load_plugin, agent):
    """Browser/external criteria bind the REAL recorded row: canonical
    identity external:{source}:{check_id}, and the row's source_revision
    must equal the canonical hash of the current declared source set —
    never an arbitrary digest that merely happens to exist on some row."""
    from agent.verification_evidence import record_external_verification
    harness = load_plugin("continuation")
    current = {"widget.py": hashlib.sha256(
        (project / "widget.py").read_bytes()).hexdigest()}
    # The row must POSTDATE the declaration: declare first, record after.
    assert _tool(harness, action="declare", id="1",
                 observable="login renders", check_kind="external",
                 check_identity="external:browser-bladebro:browser:login",
                 paths=["widget.py"])["success"] is True
    row = record_external_verification(
        session_id=SESSION, cwd=project, source="browser-bladebro",
        check_id="browser:login", ok=True,
        source_revision=_canonical_source_revision(current))
    assert row and row.get("id") is not None
    with bind_subagent_parent(agent):
        _run_observed_capture(harness, row["id"])
    assert _tool(harness, action="bind", id="1",
                 evidence_id=row["id"])["success"] is True
    # an unrelated row whose digest was spoofed cannot satisfy the criterion
    assert _tool(harness, action="declare", id="2",
                 observable="same check, spoofed digest", check_kind="external",
                 check_identity="external:browser-bladebro:browser:login",
                 paths=["widget.py"])["success"] is True
    bad = record_external_verification(
        session_id=SESSION, cwd=project, source="browser-bladebro",
        check_id="browser:login", ok=True, source_revision="sha256:" + "0" * 64)
    with bind_subagent_parent(agent):
        _run_observed_capture(harness, bad["id"], call_id="cap-bad")
    bound = _tool(harness, action="bind", id="2", evidence_id=bad["id"])
    assert bound["success"] is False and "source_revision" in bound["error"]
    # source mutation after external evidence: the canonical hash no longer
    # matches AND the drift check unverifies the binding.
    (project / "widget.py").write_text("VALUE = 42\n")
    assert _tool(harness, action="status")["criteria"][0]["status"] == "stale_evidence"


def test_bound_row_staleness_blocks_completion(project, load_plugin, agent, reviewer):
    """Certification re-looks-up the NATIVE ROW every time: workspace edits
    make the row stale inside the host ledger even when the plugin cannot
    see any source-byte change — completion blocks, reviewer never runs."""
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    _capture(agent)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    mark_workspace_edited(session_id=SESSION, cwd=project, paths=["widget.py"])
    assert _tool(harness, action="status")["criteria"][0]["status"] == "stale_evidence"
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "unverified" in results[0]["message"]
    assert "round" not in results[0]["message"], \
        "stale evidence cannot be laundered into a review round"
    assert reviewer["built"] == {}


def test_redeclaration_invalidates_earlier_observation(project, load_plugin, agent):
    """Re-declaring a criterion is a NEW requirement: observations made
    against the previous declaration generation can never bind."""
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    assert _declare(harness, cid="1", observable="changed requirement",
                    identity="pytest")["success"] is True
    bound = _tool(harness, action="bind", id="1", evidence_id=evidence["id"])
    assert bound["success"] is False and "observed check execution" in bound["error"], \
        "old-generation observation must not certify the new declaration"
    fresh = _run_observed_check(harness, agent, project, call_id="tc-after-redeclare")
    assert _tool(harness, action="bind", id="1",
                 evidence_id=fresh["id"])["success"] is True


def _external_row(project, canon_rev):
    from agent.verification_evidence import record_external_verification
    return record_external_verification(
        session_id=SESSION, cwd=project, source="browser-bladebro",
        check_id="browser:login", ok=True, source_revision=canon_rev)


def _declare_external(harness, cid="1"):
    return _tool(harness, action="declare", id=cid, observable="login renders",
                 check_kind="external",
                 check_identity="external:browser-bladebro:browser:login",
                 paths=["widget.py"])


def _run_observed_capture(harness, row_id, call_id="cap"):
    """Drive the pre/post browser_capture pair for row_id with the plugin's
    real envelope check (native_recorded + integer evidence_id)."""
    harness.hook("pre_tool_call", tool_name="browser_capture", session_id=SESSION,
                 tool_call_id=call_id, args={"scenario_id": "login"})
    harness.hook("post_tool_call", tool_name="browser_capture", session_id=SESSION,
                 tool_call_id=call_id,
                 result=json.dumps({"success": True, "native_recorded": True,
                                     "evidence_id": row_id,
                                     "evidence_binding": "bound"}))


def test_external_evidence_before_declaration_rejected(project, load_plugin, agent):
    """External evidence recorded BEFORE the criterion existed cannot
    retroactively certify it — declared_at provenance fence — even when the
    row is observed live in the current turn."""
    harness = load_plugin("continuation")
    current = {"widget.py": hashlib.sha256(
        (project / "widget.py").read_bytes()).hexdigest()}
    row = _external_row(project, _canonical_source_revision(current))
    assert _declare_external(harness)["success"] is True
    with bind_subagent_parent(agent):
        _run_observed_capture(harness, row["id"])
    bound = _tool(harness, action="bind", id="1", evidence_id=row["id"])
    assert bound["success"] is False and "predates" in bound["error"]


def test_external_evidence_from_earlier_turn_rejected(project, load_plugin, agent):
    """Same-session external evidence recorded AFTER the declaration but in
    an earlier turn is unbindable too: the current-turn started_at fence
    rejects it even if a live capture observation cites the stale row id."""
    harness = load_plugin("continuation")
    current = {"widget.py": hashlib.sha256(
        (project / "widget.py").read_bytes()).hexdigest()}
    assert _declare_external(harness)["success"] is True
    row = _external_row(project, _canonical_source_revision(current))
    _capture(agent)  # the CURRENT turn starts after the row was recorded
    with bind_subagent_parent(agent):
        _run_observed_capture(harness, row["id"], call_id="cap-stale")
    bound = _tool(harness, action="bind", id="1", evidence_id=row["id"])
    assert bound["success"] is False and "predates" in bound["error"]


def test_sync_channel_cancellation_stays_labeled(project, load_plugin, agent,
                                                 monkeypatch):
    """The synchronous review channel applies the same bounded unverified
    release to cancelled criteria as the async delegation rail."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    assert _tool(harness, action="disposition", id="1", disposition="cancelled",
                 reason="requirement dropped by user")["success"] is True
    monkeypatch.setattr(
        "agent.review_engine.start_review",
        lambda agent_, messages, prompt="": {
            "results": [{"status": "completed",
                         "summary": _coverage_verdict("clean", {"1": "cancelled"})}]},
    )
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []
    labeled = harness.hook("transform_llm_output", response_text="All done.",
                           session_id=SESSION, model="m")
    assert labeled and _NOTE in labeled[0] and "cancelled" in labeled[0], \
        "sync-channel clean review still releases cancellations labeled unverified"


def test_missing_workspace_never_verified(project, load_plugin, agent, monkeypatch):
    """An unresolvable workspace can never certify anything: not 'verified'
    in status, and the gate holds completion unverified."""
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    _capture(agent)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    monkeypatch.setattr("agent.runtime_cwd.resolve_context_cwd", lambda: None)
    monkeypatch.setattr("agent.runtime_cwd.resolve_agent_cwd", lambda: None)
    assert _tool(harness, action="status")["criteria"][0]["status"] == "unverified"
    results = _hook(harness, agent, changed_paths=[])
    assert results and "unverified" in results[0]["message"]


def test_todo_content_change_invalidates_review(project, load_plugin, agent,
                                                reviewer):
    """The review binding hashes the FULL todo items: a content-only change
    (same id/status) is a different outcome and invalidates the clean review."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    reviewer["summary"] = _coverage_verdict("clean", {"1": "covered"})
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []
    agent._todo_store.write([{"id": "1", "content": "completely different requirement",
                              "status": "completed"}])
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "round 2" in results[0]["message"]


def test_acceptance_contract_survives_compaction_rotation(project, load_plugin,
                                                          agent, monkeypatch):
    """Same-agent session rotation (compaction) carries declarations and
    dispositions forward on the new session id, invalidating evidence
    bindings — never orphans the contract on the dead key."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    rotated = "rotated-after-compaction"
    agent.session_id = rotated
    with bind_subagent_parent(agent):
        st = json.loads(harness.tool("acceptance", {"action": "status"},
                                     session_id=rotated))
    crit = st["criteria"][0]
    assert crit["id"] == "1"
    assert crit["evidence_id"] is None, "evidence must be re-observed post-rotation"
    record = harness.context.state.get(f"session:{rotated}")
    assert record and record["acceptance"]["1"]["check_identity"] == "pytest"
    assert harness.context.state.get(f"session:{SESSION}") is None, \
        "the dead session key is cleared, not left dangling with the contract"


def test_task_keyed_native_cwd_used_even_without_configured_cwd(
        project, load_plugin, agent, monkeypatch):
    """The native terminal cwd registry is keyed by the effective TASK id;
    the plugin threads tool/hook task_id through workspace resolution
    instead of guessing from the session id or failing closed with the
    launch cwd unavailable."""
    harness = load_plugin("continuation")
    monkeypatch.setattr("agent.runtime_cwd.resolve_context_cwd", lambda: None)
    monkeypatch.setattr("agent.runtime_cwd.resolve_agent_cwd", lambda: None)
    monkeypatch.setattr("tools.terminal_tool.get_session_cwd",
                        lambda ident: str(project) if ident == "task-1" else None)
    declared = json.loads(harness.tool(
        "acceptance", {"action": "declare", "id": "1", "observable": "x",
                       "check_kind": "terminal", "check_identity": "pytest",
                       "paths": ["widget.py"]},
        session_id=SESSION, task_id="task-1"))
    assert declared["success"] is True
    record = harness.context.state.get(f"session:{SESSION}") or {}
    assert record["acceptance"]["1"]["workspace"] == str(project)


def test_captured_task_cwd_drives_completion_gate(project, load_plugin, agent,
                                                  reviewer, monkeypatch):
    """pre_verify's producer sends NO task_id: the captured record's task id
    (and its native cwd) resolve the workspace through declare/check/bind to
    a released completion, with no configured workspace present."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    monkeypatch.setattr("agent.runtime_cwd.resolve_context_cwd", lambda: None)
    monkeypatch.setattr("agent.runtime_cwd.resolve_agent_cwd", lambda: None)
    monkeypatch.setattr("tools.terminal_tool.get_session_cwd",
                        lambda ident: str(project) if ident == "task-1" else None)
    _capture(agent)  # producer-side capture carries task_id="task-1"
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    reviewer["summary"] = _coverage_verdict("clean", {"1": "covered"})
    _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []


def test_monorepo_exec_cwd_must_match_declared_workspace(project, load_plugin, agent):
    from agent.verification_evidence import verification_evidence_by_id

    harness = load_plugin("continuation")
    pkg_b = project / "pkg-b"
    pkg_b.mkdir()
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project, call_id="tc-b", workdir=pkg_b)
    # Native project-root ownership succeeds; the plugin must enforce the finer cwd boundary.
    assert verification_evidence_by_id(evidence["id"], session_id=SESSION,
                                       cwd=project)["status"] == "passed"
    assert _tool(harness, action="bind", id="1", evidence_id=evidence["id"])["success"] is False
    correct = _run_observed_check(harness, agent, project, call_id="tc-a")
    assert _tool(harness, action="bind", id="1", evidence_id=correct["id"])["success"] is True


def test_purge_is_owner_scoped_across_sessions(project, load_plugin, agent):
    """Redefining c1 in another session must never destroy session A's
    observation of its own c1."""
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    declared = json.loads(harness.tool(
        "acceptance", {"action": "declare", "id": "1",
                       "observable": "B's different requirement",
                       "check_kind": "terminal", "check_identity": "pytest",
                       "paths": ["widget.py"]}, session_id="other-session"))
    assert declared["success"] is True
    bound = _tool(harness, action="bind", id="1", evidence_id=evidence["id"])
    assert bound["success"] is True, \
        "another session's purge must not touch this session's observation"


def test_first_check_after_compaction_binds(project, load_plugin, agent):
    """The first declared check AFTER rotation is genuinely observed and
    bindable: the pre/post hooks run same-agent migration before reading
    criteria, so the post-compaction execution is never rejected as
    unobserved."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    assert _declare(harness, cid="1")["success"] is True
    rotated = "rotated-after-compaction"
    agent.session_id = rotated
    with bind_subagent_parent(agent):
        evidence = _run_observed_check(harness, agent, project, call_id="tc-rot")
        st = json.loads(harness.tool("acceptance", {
            "action": "bind", "id": "1",
            "evidence_id": evidence["id"]}, session_id=rotated))
    assert st["success"] is True, \
        "the first post-compaction check must observe and bind cleanly"


def test_sync_review_edit_during_review_is_not_certified(project, load_plugin,
                                                         agent, monkeypatch):
    """Native reviewers run with tools: a state move DURING the synchronous
    review invalidates the clean verdict — the current-state comparison must
    re-run before certification, exactly like the async/cached routes."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True

    def mid_review_edit(agent_, messages, prompt=""):
        (project / "widget.py").write_text("VALUE = 7\n")
        record_terminal_result(
            session_id=SESSION, cwd=project, command="pytest", exit_code=0,
            output="1 passed")
        return {"results": [{"status": "completed",
                             "summary": _coverage_verdict("clean",
                                                          {"1": "covered"})}]}

    monkeypatch.setattr("agent.review_engine.start_review", mid_review_edit)
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "synchronous review" in results[0]["message"]
    record = harness.context.state.get(f"session:{SESSION}") or {}
    assert "verdict" not in (record.get("review") or {}), \
        "a clean verdict for moved state is never certified"


def test_selector_subset_cannot_certify_broad_declaration(project, load_plugin, agent):
    """Raw command identity is exact: 'pytest -k smoke' canonicalizes to
    'pytest' (native may even record it as full-scope), but a passing
    SELECTED subset can never certify a declared whole-suite criterion."""
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1", identity="pytest")["success"] is True
    subset = _run_observed_check(harness, agent, project,
                                 identity="pytest -k smoke", call_id="tc-sub")
    bound = _tool(harness, action="bind", id="1", evidence_id=subset["id"])
    assert bound["success"] is False and "check identity" in bound["error"]
    # the exact declared raw command/selector CAN bind the same execution
    assert _declare(harness, cid="1", identity="pytest -k smoke")["success"] is True
    exact = _run_observed_check(harness, agent, project,
                                identity="pytest -k smoke", call_id="tc-exact")
    assert _tool(harness, action="bind", id="1",
                 evidence_id=exact["id"])["success"] is True


def test_cwd_move_requires_redeclaration_in_current_workspace(
        project, load_plugin, agent, tmp_path, monkeypatch):
    """A native cwd move cannot rebind the criterion to the new tree:
    binding is rejected against the declaration-pinned workspace, and the
    criterion must be redeclared and rerun in the current workspace."""
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1", identity="pytest")["success"] is True
    b = tmp_path / "tree-b"
    b.mkdir()
    (b / "widget.py").write_text("VALUE = 1\n")
    monkeypatch.setattr("agent.runtime_cwd.resolve_context_cwd", lambda: b)
    evidence = record_terminal_result(command="pytest", cwd=b,
                                      session_id=SESSION, exit_code=0)
    bound = _tool(harness, action="bind", id="1", evidence_id=evidence["id"])
    assert bound["success"] is False and "workspace" in bound["error"]
    # redeclaration in the current tree, then the check rerun there, binds
    assert _declare(harness, cid="1", identity="pytest")["success"] is True
    moved = _run_observed_check(harness, agent, b)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=moved["id"])["success"] is True


def test_task_cwd_override_not_blocked_by_launch_workspace(
        project, load_plugin, agent, reviewer, tmp_path, monkeypatch):
    """The captured task workspace is authoritative; the configured launch
    directory is a fallback, not an additional workspace to verify — the
    launch tree's untouched ledger must not block completion."""
    harness = load_plugin("continuation")
    b = tmp_path / "task-tree"
    b.mkdir()
    (b / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (b / "widget.py").write_text("VALUE = 1\n")
    monkeypatch.setattr("tools.terminal_tool.get_session_cwd",
                        lambda ident: str(b) if ident == "task-1" else None)
    _verify_ok(b)  # ONLY the task tree is green; project/launch has no evidence
    _capture(agent)  # capture pins task-1
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, b)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    reviewer["summary"] = _coverage_verdict("clean", {"1": "covered"})
    _hook(harness, agent, changed_paths=[str(b / "widget.py")])
    _wait_state(_review_id(harness), ("completed",))
    assert _hook(harness, agent, changed_paths=[str(b / "widget.py")]) == []


def test_65_criteria_all_bind_within_history_bound(project, load_plugin, agent):
    """65 criteria in one call is within the native 256-item bound: the
    complete active-call observation set must survive, so the criteria the
    old arbitrary per-criterion pruning evicted (c0..c32) still bind."""
    harness = load_plugin("continuation")
    for n in range(65):
        assert _declare(harness, cid=f"c{n}", identity="pytest")["success"] is True
    evidence = _run_observed_check(harness, agent, project, call_id="tc-many")
    for cid in ("c0", "c32", "c64"):
        assert _tool(harness, action="bind", id=cid,
                     evidence_id=evidence["id"])["success"] is True, \
            f"active-call observation {cid} was evicted from under its own check"


def test_status_observations_scoped_to_requesting_session(project, load_plugin, agent):
    """acceptance status may disclose ONLY the requesting session's own
    evidence observations: no other session's criterion ids or evidence ids
    leak into the response."""
    harness = load_plugin("continuation")
    assert _declare(harness, cid="1")["success"] is True
    _run_observed_check(harness, agent, project)
    own = json.loads(harness.tool("acceptance", {"action": "status"},
                                  session_id=SESSION))
    assert own["evidence_observations"], "own session must see its own observations"
    other = json.loads(harness.tool("acceptance", {"action": "status"},
                                     session_id="other-session"))
    assert other["evidence_observations"] == []
    assert other["criteria"] == []


def test_oversized_mapping_fails_closed_before_review(
        project, load_plugin, agent, reviewer):
    """native review_engine truncates every message at 12000 chars: a
    mapping past that limit must fail closed with an explicit unverified
    reason instead of dispatching a silently truncated review."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    huge = "x" * 14000
    assert _declare(harness, cid="1", observable=huge)["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "cap" in results[0]["message"]
    assert reviewer["built"] == {}, "truncated review must never dispatch"
    record = harness.context.state.get(f"session:{SESSION}") or {}
    assert any("unverified" in b for b in record.get("blockers") or [])


def test_sync_review_certifies_unchanged_state(project, load_plugin, agent, monkeypatch):
    """The synchronous channel's current-state equality is equivalent to the
    async/cached routes: when nothing moves DURING the review, the exact
    reviewed snapshot certifies cleanly."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True

    def clean_sync(agent_, messages, prompt=""):
        return {"results": [{"status": "completed",
                             "summary": _coverage_verdict("clean", {"1": "covered"})}]}

    monkeypatch.setattr("agent.review_engine.start_review", clean_sync)
    assert _hook(harness, agent, changed_paths=[str(project / "widget.py")]) == []
    record = harness.context.state.get(f"session:{SESSION}") or {}
    assert (record.get("review") or {}).get("verdict") == "clean"
    assert not record.get("blockers")


def test_sync_review_new_evidence_during_review_is_not_certified(
        project, load_plugin, agent, monkeypatch):
    """A native evidence event recorded DURING the synchronous review moves
    the evidence snapshot even when sources, todos and declared bindings are
    unchanged: the release branch must require exact snapshot equality,
    exactly like the async/cached routes."""
    harness = load_plugin("continuation")
    _verify_ok(project)
    _capture(agent)
    assert _declare(harness, cid="1")["success"] is True
    evidence = _run_observed_check(harness, agent, project)
    assert _tool(harness, action="bind", id="1",
                 evidence_id=evidence["id"])["success"] is True

    def clean_sync_plus_new_event(agent_, messages, prompt=""):
        # event-only move: NO file edit, no source drift, same todos and
        # same declared binding — only the evidence snapshot changes
        record_terminal_result(session_id=SESSION, cwd=project, command="pytest",
                               exit_code=0, output="1 passed")
        return {"results": [{"status": "completed",
                             "summary": _coverage_verdict("clean", {"1": "covered"})}]}

    monkeypatch.setattr("agent.review_engine.start_review", clean_sync_plus_new_event)
    results = _hook(harness, agent, changed_paths=[str(project / "widget.py")])
    assert results and "synchronous review" in results[0]["message"]
    record = harness.context.state.get(f"session:{SESSION}") or {}
    assert "verdict" not in (record.get("review") or {}), \
        "a clean verdict bound to the pre-review evidence snapshot is never released"
    assert any("unverified" in b for b in record.get("blockers") or [])


def test_256_criterion_call_survives_peer_call_overlap(
        project, load_plugin, agent):
    """Session A runs a full native-width 256-criterion check; session B
    starts its own check before A's post hook completes. Whole-call-group
    retention must admit B WITHOUT deleting any of A's pending rows: every
    selected criterion of A's genuine result then binds."""
    harness = load_plugin("continuation")
    a, b = "sess-wide-a", "sess-wide-b"
    for n in range(256):
        assert json.loads(harness.tool(
            "acceptance", {"action": "declare", "id": f"c{n}",
                           "observable": f"c{n} observable", "check_kind": "terminal",
                           "check_identity": "pytest", "paths": ["widget.py"]},
            session_id=a))["success"] is True
    assert json.loads(harness.tool(
        "acceptance", {"action": "declare", "id": "b1", "observable": "b observable",
                       "check_kind": "terminal", "check_identity": "pytest",
                       "paths": ["widget.py"]}, session_id=b))["success"] is True

    with bind_subagent_parent(agent):
        harness.hook("pre_tool_call", tool_name="terminal", session_id=a,
                     tool_call_id="tc-a", args={"command": "pytest"})
        b_pre = harness.hook("pre_tool_call", tool_name="terminal", session_id=b,
                             tool_call_id="tc-b", args={"command": "pytest"})
        assert b_pre == [], "B's small call must be admitted, not blocked or destructive"
        b_ev = record_terminal_result(command="pytest", cwd=project, session_id=b,
                                       exit_code=0, output="1 passed")
        harness.hook("post_tool_call", tool_name="terminal", session_id=b,
                     tool_call_id="tc-b", args={"command": "pytest"},
                     result={"exit_code": 0, "verification_evidence": {
                         "id": b_ev["id"], "status": "passed",
                         "kind": "test", "canonical_command": b_ev["canonical_command"]}})
        a_ev = record_terminal_result(command="pytest", cwd=project, session_id=a,
                                       exit_code=0, output="1 passed")
        harness.hook("post_tool_call", tool_name="terminal", session_id=a,
                     tool_call_id="tc-a", args={"command": "pytest"},
                     result={"exit_code": 0, "verification_evidence": {
                         "id": a_ev["id"], "status": "passed",
                         "kind": "test", "canonical_command": a_ev["canonical_command"]}})
        for n in (0, 127, 255):  # head, middle and tail: none evicted row-by-row
            assert json.loads(harness.tool(
                "acceptance", {"action": "bind", "id": f"c{n}",
                               "evidence_id": a_ev["id"]}, session_id=a))["success"] is True, \
                f"criterion c{n} was evicted from under its own in-flight check"
        # B bound its own genuine row in its own session
        assert json.loads(harness.tool(
            "acceptance", {"action": "bind", "id": "b1", "evidence_id": b_ev["id"]},
            session_id=b))["success"] is True
        # status stays exact-session private even with 256 foreign rows live
        b_status = json.loads(harness.tool(
            "acceptance", {"action": "status"}, session_id=b))
        assert [c["id"] for c in b_status["criteria"]] == ["b1"]
        assert all(o["criterion"] == "b1" for o in b_status["evidence_observations"])
        other = json.loads(harness.tool(
            "acceptance", {"action": "status"}, session_id="sess-wide-other"))
        assert other["criteria"] == [] and other["evidence_observations"] == []


def test_group_capacity_blocks_new_call_and_whole_completed_group_evicts(
        project, load_plugin, agent, monkeypatch):
    """All active groups -> the new call is explicitly refused BEFORE its
    check runs, with every active group's rows intact; completing one call
    frees the slot for admission, and the next admission evicts only the
    WHOLE completed group — no pending observation is ever trimmed."""
    harness = load_plugin("continuation")
    monkeypatch.setattr(harness.module, "_MAX_GROUPS", 3)
    s = "sess-capacity"
    assert json.loads(harness.tool(
        "acceptance", {"action": "declare", "id": "1", "observable": "o",
                       "check_kind": "terminal", "check_identity": "pytest",
                       "paths": ["widget.py"]}, session_id=s))["success"] is True

    def pre(call_id):
        return harness.hook("pre_tool_call", tool_name="terminal", session_id=s,
                            tool_call_id=call_id, args={"command": "pytest"})

    def complete(call_id):
        with bind_subagent_parent(agent):
            ev = record_terminal_result(command="pytest", cwd=project,
                                        session_id=s, exit_code=0, output="1 passed")
            harness.hook("post_tool_call", tool_name="terminal", session_id=s,
                         tool_call_id=call_id, args={"command": "pytest"},
                         result={"exit_code": 0, "verification_evidence": {
                             "id": ev["id"], "status": "passed",
                             "kind": "test", "canonical_command": ev["canonical_command"]}})
        return ev

    for cid in ("t1", "t2", "t3"):
        assert pre(cid) == []
    denied = pre("t4")
    assert denied and denied[0]["action"] == "block", \
        "a full active-group set refuses the new call instead of evicting peers"
    # a real evidence row exists, but t2's observation is still PENDING —
    # no observation was destroyed or trimmed to admit t4
    pending_ev = record_terminal_result(command="pytest", cwd=project,
                                        session_id=s, exit_code=0, output="1 passed")
    assert "no genuine observed check execution" in json.loads(harness.tool(
        "acceptance", {"action": "bind", "id": "1", "evidence_id": pending_ev["id"]},
        session_id=s))["error"]

    ev1 = complete("t1")
    assert pre("t4") == [], "completing one call frees the slot for admission"
    assert "no genuine observed check execution" in json.loads(harness.tool(
        "acceptance", {"action": "bind", "id": "1", "evidence_id": ev1["id"]},
        session_id=s))["error"], \
        "t1's COMPLETED whole group is evictable once its slot is needed"

    ev4 = complete("t4")
    assert pre("t5") == []
    # t5's admission evicted t4's WHOLE completed group — t4's evidence is
    # now unobserved, while the pending groups t2/t3 are untouched
    assert "no genuine observed check execution" in json.loads(harness.tool(
        "acceptance", {"action": "bind", "id": "1", "evidence_id": ev4["id"]},
        session_id=s))["error"], "t4's completed group was evicted whole once needed"
    denied = pre("t6")
    assert denied and denied[0]["action"] == "block", \
        "t4's evicted slot was refilled; t2/t3 stay protected"
    with bind_subagent_parent(agent):
        for cid in ("t2", "t3"):
            ev = complete(cid)
            assert json.loads(harness.tool(
                "acceptance", {"action": "bind", "id": "1", "evidence_id": ev["id"]},
                session_id=s))["success"] is True, \
                f"pending group {cid} was partially trimmed by group eviction"



def test_over_wide_declaration_fails_closed(monkeypatch, project, load_plugin, agent):
    """An oversized single call is refused outright (block directive) —
    never half-trimmed — and an exactly-at-bound call observes and binds
    all of its criteria."""
    harness = load_plugin("continuation")
    monkeypatch.setattr(harness.module, "_MAX_OBS_PER_CALL", 2)
    s = "sess-width"
    for cid in ("w1", "w2", "w3"):
        assert json.loads(harness.tool(
            "acceptance", {"action": "declare", "id": cid, "observable": f"{cid} o",
                           "check_kind": "terminal", "check_identity": "pytest",
                           "paths": ["widget.py"]}, session_id=s))["success"] is True
    denied = harness.hook("pre_tool_call", tool_name="terminal", session_id=s,
                          tool_call_id="tc-wide", args={"command": "pytest"})
    assert denied and denied[0]["action"] == "block"
    with bind_subagent_parent(agent):
        monkeypatch.setattr(harness.module, "_MAX_OBS_PER_CALL", 3)
        ev = record_terminal_result(command="pytest", cwd=project,
                                    session_id=s, exit_code=0, output="1 passed")
        harness.hook("pre_tool_call", tool_name="terminal", session_id=s,
                     tool_call_id="tc-ok", args={"command": "pytest"})
        harness.hook("post_tool_call", tool_name="terminal", session_id=s,
                     tool_call_id="tc-ok", args={"command": "pytest"},
                     result={"exit_code": 0, "verification_evidence": {
                         "id": ev["id"], "status": "passed",
                         "kind": "test", "canonical_command": ev["canonical_command"]}})
        for cid in ("w1", "w2", "w3"):
            assert json.loads(harness.tool(
                "acceptance", {"action": "bind", "id": cid, "evidence_id": ev["id"]},
                session_id=s))["success"] is True


def _closure_cell(harness, hook_name, var):
    """Group/observation state lives inside register()'s closure; the
    registered callback carries the live cell — read it for white-box
    assertions."""
    cb = harness.manager._hooks[hook_name][-1]
    return dict(zip(cb.__code__.co_freevars, cb.__closure__))[var].cell_contents


def test_composite_criterion_id_survives_suffix_redeclare(monkeypatch, project, load_plugin, agent):
    """Criterion ids may contain '|'. Purging by delimiter suffix (the old
    behavior) let declaring 'case' destroy 'suite|case''s genuine evidence.
    (1) After an unrelated 'case' declare/redeclare, the observed
    'suite|case' remains bindable; (2) re-declaring 'suite|case' itself
    purges its observations, invalidating the old evidence as intended."""
    harness = load_plugin("continuation")

    def declare(sid, cid):
        assert json.loads(harness.tool(
            "acceptance", {"action": "declare", "id": cid,
                            "observable": f"{cid} observable",
                            "check_kind": "terminal", "check_identity": "pytest",
                            "paths": ["widget.py"]}, session_id=sid))["success"] is True

    def bind(sid, cid, evidence_id):
        return json.loads(harness.tool(
            "acceptance", {"action": "bind", "id": cid,
                            "evidence_id": evidence_id}, session_id=sid))

    # (1) preserving branch: unrelated suffix-id declaration must not purge
    s_keep = "sess-composite-keep"
    declare(s_keep, "suite|case")
    ev_keep = _run_observed_check(harness, agent, project, call_id="tc-keep",
                                  session_id=s_keep)
    declare(s_keep, "case")            # fresh unrelated criterion
    declare(s_keep, "case")            # and its redeclaration
    assert bind(s_keep, "suite|case", ev_keep["id"])["success"] is True, \
        "'case' declaration must not purge 'suite|case' observations"

    # (2) invalidating branch: re-declaring the composite id itself
    s_kill = "sess-composite-kill"
    declare(s_kill, "suite|case")
    ev_kill = _run_observed_check(harness, agent, project, call_id="tc-kill",
                                  session_id=s_kill)
    declare(s_kill, "suite|case")      # own-id redeclare purges own rows
    err = bind(s_kill, "suite|case", ev_kill["id"])
    assert err["success"] is False
    assert "no genuine observed check execution" in err["error"], \
        "own-id redeclare must invalidate old evidence as intended"


class _ParkedHash:
    """Deterministic overlap seam: parks the plugin's real _source_hashes
    on the next armed call until released. The cell is shared by the
    pre/post observers' closures, so one scoped monkeypatch covers both
    hook paths; the abandoned worker is genuinely mid-IO inside the real
    hashing seam — no machine-speed margin, no synthetic observer, and
    the bounded wait makes an unexpected path fail loud, never hang."""

    def __init__(self, harness, monkeypatch):
        cb = harness.manager._hooks["post_tool_call"][-1]
        cell = dict(zip(cb.__code__.co_freevars, cb.__closure__))["_source_hashes"]
        self.orig = cell.cell_contents
        self.started = threading.Event()
        self.released = threading.Event()
        self._armed = False

        def parked(workspace, paths):
            if self._armed:
                self._armed = False
                self.started.set()
                # fail-loud bound: no code path may park past the test
                self.released.wait(30)
            return self.orig(workspace, paths)

        monkeypatch.setattr(cell, "cell_contents", parked)

    def arm(self):
        self._armed = True

    def release(self):
        self.released.set()

    @property
    def parked(self):
        return self.started.is_set() and not self.released.is_set()


def _setup_wedge_scenario(project, load_plugin, agent, monkeypatch, settle=True):
    """Shared driver: two admitted terminal calls over a declared source,
    terminal events dispatched through the REAL bounded dispatcher. Call
    A's post observer is parked inside the real _source_hashes seam
    (worker abandoned mid-IO, still running); call B's observer is then
    natively suppressed (_HOOK_SKIPPED, no replay). Returns everything
    the assertions need, including the park for deterministic release."""
    harness = load_plugin("continuation")
    monkeypatch.setattr(harness.module, "_MAX_GROUPS", 2)
    park = _ParkedHash(harness, monkeypatch)
    (project / "gate.src").write_bytes(b"gate-source-v1\n")
    s = "sess-settle-wedge"
    assert json.loads(harness.tool(
        "acceptance", {"action": "declare", "id": "g1", "observable": "o",
                       "check_kind": "terminal", "check_identity": "pytest",
                       "paths": ["gate.src"]}, session_id=s))["success"] is True
    assert harness.hook("pre_tool_call", tool_name="terminal", session_id=s,
                        tool_call_id="tc-a", args={"command": "pytest"}) == []
    assert harness.hook("pre_tool_call", tool_name="terminal", session_id=s,
                        tool_call_id="tc-b", args={"command": "pytest"}) == []
    with bind_subagent_parent(agent):
        ev_a = record_terminal_result(command="pytest", cwd=project, session_id=s,
                                       exit_code=0, output="1 passed")
        ev_b = record_terminal_result(command="pytest", cwd=project, session_id=s,
                                       exit_code=0, output="1 passed")
    # 10ms per-callback cap, suppression window expired: the next dispatch
    # of the same callback hits the STILL-RUNNING worker branch directly.
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.01)
    monkeypatch.setattr(harness.manager, "_hook_timeout_suppression_seconds", 0.0)

    def post(tool_call_id):
        with bind_subagent_parent(agent):
            harness.hook("post_tool_call", tool_name="terminal", session_id=s,
                         tool_call_id=tool_call_id, args={"command": "pytest"},
                         result={"exit_code": 0, "verification_evidence": {
                             "id": (ev_a if tool_call_id == "tc-a" else ev_b)["id"],
                             "status": "passed", "kind": "test",
                             "canonical_command": "pytest"}})

    if not settle:  # pre-fix dispatch: no terminal bookkeeping seam
        harness.manager._hooks["post_tool_call_settled"] = []
    park.arm()
    post("tc-a")                      # observer abandoned mid-hash; still running
    assert harness.manager._hook_running_callbacks, "overlap must be real (observer running)"
    assert park.parked, "observer must be parked inside the real hashing seam"
    post("tc-b")                      # same callback key -> _HOOK_SKIPPED, no replay
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 30.0)
    return harness, s, ev_a, ev_b, park


def test_suppressed_bounded_post_hook_settles_group(project, load_plugin, agent, monkeypatch):
    """Consumer-boundary overlap regression: the native dispatcher may
    suppress post_tool_call (_HOOK_SKIPPED — still-running/timeout, NO
    replay), yet the guaranteed post_tool_call_settled seam settles every
    group so suppressed observations can never wedge the group budget.
    Dropped evidence stays unbindable; a late observer never resurrects
    evicted groups/rows. The parked-hash seam makes the overlap
    deterministic — no machine-speed margin, no synthetic observer."""
    harness, s, ev_a, ev_b, park = _setup_wedge_scenario(
        project, load_plugin, agent, monkeypatch)
    groups = _closure_cell(harness, "post_tool_call_settled", "_tool_obs_groups")
    obs_rows = _closure_cell(harness, "post_tool_call", "_evidence_obs")
    # Guaranteed terminal bookkeeping settled the suppressed group
    # synchronously inside the overlapping dispatch.
    assert groups[f"{s}|tc-b"]["done"] is True

    # Every dispatch settles exactly once, on the dispatching thread.
    settled = []

    def observer(tool_call_id="", session_id="", **_rest):
        settled.append((session_id, tool_call_id, threading.current_thread().name))
    harness.manager._hooks.setdefault("post_tool_call_settled", []).append(observer)
    harness.hook("post_tool_call", tool_name="terminal", session_id=s,
                 tool_call_id="tc-c", args={"command": "pytest"}, result={})
    assert settled == [(s, "tc-c", threading.current_thread().name)]

    # Suppressed B's group is settled (done), and its slot is released: the
    # new call is admitted instead of the wedge the defect caused.
    assert groups[f"{s}|tc-b"]["done"] is True
    assert harness.hook("pre_tool_call", tool_name="terminal", session_id=s,
                        tool_call_id="tc-d", args={"command": "pytest"}) == []

    # Dropped evidence never becomes passing evidence — B's suppressed post
    # data (and the late A rows evicted with its group) are unbindable.
    for ev in (ev_b, ev_a):
        err = json.loads(harness.tool(
            "acceptance", {"action": "bind", "id": "g1",
                            "evidence_id": ev["id"]}, session_id=s))
        assert err["success"] is False
        assert "no genuine observed check execution" in err["error"]

    # The parked observer finishes late; its commit must find nothing
    # (group evicted) and resurrect neither rows nor groups.
    park.release()
    deadline = time.monotonic() + 30
    while harness.manager._hook_running_callbacks and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not harness.manager._hook_running_callbacks, "abandoned observer hung"
    assert f"{s}|tc-a" not in groups
    assert not any(str(k).startswith(f"{s}|tc-a|") for k in obs_rows)
    err = json.loads(harness.tool(
        "acceptance", {"action": "bind", "id": "g1",
                       "evidence_id": ev_a["id"]}, session_id=s))
    assert "no genuine observed check execution" in err["error"]


def test_parked_pre_observer_never_blocks_the_dispatching_thread(
        project, load_plugin, agent, monkeypatch):
    """The pre_tool_call observer must never hold _tool_obs_lock across
    source hashing: the guaranteed post_tool_call_settled seam runs on
    the DISPATCHING thread with no native timeout, so IO under the lock
    would stall every later dispatch for the hashing duration — the
    unbounded hot-path stall class the settle seam exists to remove.
    Deterministic seam: the pre observer is parked inside the plugin's
    real _source_hashes; the whole post dispatch must complete while the
    observer is still parked, and its late commit must bless nothing."""
    harness = load_plugin("continuation")
    monkeypatch.setattr(harness.module, "_MAX_GROUPS", 1)
    park = _ParkedHash(harness, monkeypatch)
    (project / "gate.src").write_bytes(b"gate-source-v1\n")
    s = "sess-pre-lock"

    def declare():
        assert json.loads(harness.tool(
            "acceptance", {"action": "declare", "id": "g1", "observable": "o",
                            "check_kind": "terminal", "check_identity": "pytest",
                            "paths": ["gate.src"]}, session_id=s))["success"] is True
    declare()
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.01)
    monkeypatch.setattr(harness.manager, "_hook_timeout_suppression_seconds", 0.0)
    groups = _closure_cell(harness, "post_tool_call_settled", "_tool_obs_groups")
    obs_rows = _closure_cell(harness, "post_tool_call", "_evidence_obs")

    # The pre observer parks inside the real hashing seam; the bounded
    # dispatcher abandons it at the cap. The reservation booked under the
    # lock is all settlement needs to release, and the parked observer
    # holds nothing the dispatching thread waits on.
    park.arm()
    denied = harness.hook("pre_tool_call", tool_name="terminal", session_id=s,
                          tool_call_id="tc-a", args={"command": "pytest"})
    assert denied and denied[0]["action"] == "block", \
        "a timed-out pre observer refuses its call fail closed, never half-observes"
    assert park.parked, "pre observer must be parked mid-hash inside the seam"
    assert harness.manager._hook_running_callbacks, "pre observer must still be running"
    # Cap restored for every LATER dispatch (observer work itself is
    # tiny): only the settle seam is under test, and it has no native cap.
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 30.0)

    # The tool ran for real; its post dispatch (observer loop +
    # caller-thread settle) must COMPLETE while the pre observer is still
    # parked — under the lock-across-hashing defect the settle seam
    # stalls on _tool_obs_lock and this wait goes red in 10s. Nothing
    # that touches the observation lock may run on the main thread
    # before this point, or a lock-holding mutant would stall the
    # assertion thread itself and mask the defect.
    with bind_subagent_parent(agent):
        ev = record_terminal_result(command="pytest", cwd=project, session_id=s,
                                    exit_code=0, output="1 passed")
    settled = []

    def settle_observer(tool_call_id="", session_id="", **_rest):
        settled.append((tool_call_id, threading.current_thread().name))
    harness.manager._hooks.setdefault("post_tool_call_settled", []).append(settle_observer)
    dispatched = threading.Event()

    def dispatch():
        with bind_subagent_parent(agent):
            harness.hook("post_tool_call", tool_name="terminal", session_id=s,
                         tool_call_id="tc-a", args={"command": "pytest"},
                         result={"exit_code": 0, "verification_evidence": {
                             "id": ev["id"], "status": "passed", "kind": "test",
                             "canonical_command": "pytest"}})
        dispatched.set()
    t = threading.Thread(target=dispatch, name="dispatcher")
    t.start()
    assert dispatched.wait(10), (
        "dispatch blocked on pre-hook hashing: the settle seam stalled on "
        "_tool_obs_lock while the pre observer was parked mid-hash")
    assert park.parked, "dispatch must complete while the pre observer is parked"
    assert settled == [("tc-a", "dispatcher")], \
        "the settle seam must fire exactly once, on the dispatching thread"
    # The reservation is released the instant settlement marks the group
    # done — while the pre observer is still parked mid-hash. The group
    # becomes evictable whole-group history; no capacity wedge can form
    # (the negative control shows the wedge only when settlement is
    # absent).
    assert groups[f"{s}|tc-a"]["done"] is True

    # The criterion is re-declared while the observer is parked: its late
    # commit must never bless the superseded declaration.
    declare()

    park.release()
    deadline = time.monotonic() + 30
    while harness.manager._hook_running_callbacks and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not harness.manager._hook_running_callbacks, "parked observer hung"

    # Release: the late pre commit blesses nothing — its group is gone
    # (identity guard) and its declaration superseded (generation guard).
    assert f"{s}|tc-a" not in groups, "late pre observer must not resurrect its group"
    assert not any(str(k).startswith(f"{s}|tc-a|") for k in obs_rows), \
        "late pre observer must not re-insert rows for a superseded declaration"

    err = json.loads(harness.tool(
        "acceptance", {"action": "bind", "id": "g1",
                       "evidence_id": ev["id"]}, session_id=s))
    assert err["success"] is False
    assert "no genuine observed check execution" in err["error"], \
        "evidence whose observation was dropped stays unbindable forever"


def test_missing_settlement_seam_wedges_capacity_negative_control(
        project, load_plugin, agent, monkeypatch):
    """Negative control for the regression above, at the same consumer
    boundary with the SAME real suppression: without the settled
    bookkeeping seam (pre-fix behavior) the suppressed group stays active
    forever and blocks every future check. Active peers are never evicted;
    the failure mode is the wedge, and the regression above catches it."""
    harness, s, ev_a, ev_b, park = _setup_wedge_scenario(project, load_plugin, agent,
                                                         monkeypatch, settle=False)
    groups = _closure_cell(harness, "post_tool_call", "_tool_obs_groups")
    obs_rows = _closure_cell(harness, "post_tool_call", "_evidence_obs")

    # Both groups still pending: capacity exhausted with live observations —
    # the new call is refused, and neither group's rows are trimmed.
    denied = harness.hook("pre_tool_call", tool_name="terminal", session_id=s,
                          tool_call_id="tc-d", args={"command": "pytest"})
    assert denied and denied[0]["action"] == "block"
    assert any(str(k).startswith(f"{s}|tc-b|") for k in obs_rows)
    assert groups[f"{s}|tc-b"]["done"] is False
    err = json.loads(harness.tool(
        "acceptance", {"action": "bind", "id": "g1",
                       "evidence_id": ev_b["id"]}, session_id=s))
    assert "no genuine observed check execution" in err["error"]

    # Even after the parked observer is released and finishes late (group
    # A settles itself), the suppressed group B is permanently wedged —
    # no replay, no slot.
    park.release()
    deadline = time.monotonic() + 30
    while harness.manager._hook_running_callbacks and time.monotonic() < deadline:
        time.sleep(0.05)
    assert groups[f"{s}|tc-a"]["done"] is True
    assert groups[f"{s}|tc-b"]["done"] is False
    assert harness.hook("pre_tool_call", tool_name="terminal", session_id=s,
                        tool_call_id="tc-d", args={"command": "pytest"}) == [], \
        "completed group A is evictable history once its observer finished"
    denied = harness.hook("pre_tool_call", tool_name="terminal", session_id=s,
                          tool_call_id="tc-e", args={"command": "pytest"})
    assert denied and denied[0]["action"] == "block", \
        "without settlement the suppressed group stays active forever"
    assert groups[f"{s}|tc-b"]["done"] is False
