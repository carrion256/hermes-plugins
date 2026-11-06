"""Gate-friction PRODUCERS (#7b): continuation's repeated hold and loop-guard's
spent intervention budget file rows through the papercuts sink.

The sink itself is #7a (test_gate_friction.py); here it is replaced by a stub
installed in ``sys.modules`` so the producer contract is what is under test:
WHEN a row is filed, exactly ONCE, and what the plugin says when nothing is
listening. A missing sink must be counted and surfaced (``friction_unavailable``),
never raised, and never change what either gate decided.
"""
from __future__ import annotations

import hashlib
import json
import sys
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import ModuleType

import pytest

FRICTION_MODULE = "hermes_plugins_papercuts_friction"
SESSION = "friction-producer-session"
# The exact native todo-blocker text the continuation gate produces for this
# ledger, so the expected identity is derived here rather than trusted from the
# plugin's own helper.
TODO_BLOCKER = "unfinished tasks: 1(finish the widget)"
OTHER_BLOCKER = "unfinished tasks: 2(sharpen the bolt)"
FAILING_INVARIANT = "repeated tool error (backend_unavailable)"


def _bound(agent: object) -> AbstractContextManager[object]:
    """Bind ``agent`` as the active subagent parent, the way the host does
    around a run. Both host modules this file needs are imported here: they are
    runtime collaborators, and a test module must not import the host at import
    time."""
    from agent.subagent_lifecycle import bind_subagent_parent

    return bind_subagent_parent(agent)


def _digest(text: str) -> str:
    """Whitespace-normalised blocker identity: sha256 prefix, as documented."""
    normalised = " ".join(text.split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class _Receipt:
    """Same shape as the sink's receipt: ``filed`` never disagrees with it."""

    filed: bool
    reason: str


class _RecordingSink:
    """Stands in for plugins/papercuts/friction.py and answers ``filed``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def module(self) -> ModuleType:
        sink = self

        class GateFriction:
            """The real entry is a frozen dataclass; the surface a producer
            touches is this constructor, so the stub carries exactly it."""

            def __init__(self, source: str, kind: str, identity: str, summary: str,
                         detail: str, session_id: str) -> None:
                self.source = source
                self.kind = kind
                self.identity = identity
                self.summary = summary
                self.detail = detail
                self.session_id = session_id

        def report(friction: GateFriction) -> _Receipt:
            sink.calls.append({"source": friction.source, "kind": friction.kind,
                               "identity": friction.identity, "summary": friction.summary,
                               "detail": friction.detail, "session_id": friction.session_id})
            return _Receipt(filed=True, reason="filed")

        module = ModuleType(FRICTION_MODULE)
        module.GateFriction = GateFriction
        module.report = report
        return module


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> _RecordingSink:
    """A stub sink occupying the shared slot the plugins look up first."""
    recorder = _RecordingSink()
    monkeypatch.setitem(sys.modules, FRICTION_MODULE, recorder.module())
    return recorder


@pytest.fixture
def no_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """No papercuts in this process: not in the shared slot, not loadable."""
    monkeypatch.delitem(sys.modules, FRICTION_MODULE, raising=False)


# --- continuation: the repeated hold -----------------------------------------


class _FakeTodos:
    """Minimal stand-in for the native todo ledger (only ``read`` is used)."""

    def __init__(self, items: list[dict[str, str]]) -> None:
        self.items = items

    def read(self) -> list[dict[str, str]]:
        return [dict(item) for item in self.items]


class _FakeAgent:
    def __init__(self, items: list[dict[str, str]]) -> None:
        self.session_id = SESSION
        self.platform = "cli"
        self.model = "test-model"
        self._todo_store = _FakeTodos(items)


def _todo(item_id: str, content: str) -> dict[str, str]:
    return {"id": item_id, "content": content, "status": "pending"}


@pytest.fixture
def project(tmp_path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n",
                                             encoding="utf-8")
    (tmp_path / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr("agent.runtime_cwd.resolve_context_cwd", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def agent() -> _FakeAgent:
    return _FakeAgent([_todo("1", "finish the widget")])


def _hold(harness, agent: _FakeAgent) -> list[object]:
    with _bound(agent):
        return harness.hook("pre_verify", session_id=SESSION, coding=True, attempt=0,
                            platform="cli", model="m", final_response="I'm done.",
                            changed_paths=["widget.py"])


def _holds(harness, agent: _FakeAgent, count: int) -> None:
    for _ in range(count):
        results = _hold(harness, agent)
        assert any(isinstance(row, dict) and row.get("action") == "continue"
                   for row in results), "the gate must still hold the turn"


def _friction(harness, agent: _FakeAgent) -> dict[str, object]:
    with _bound(agent):
        status = json.loads(harness.tool("acceptance", {"action": "status"},
                                         session_id=SESSION))
    return status["friction"]


def test_third_hold_on_one_blocker_files_exactly_one_row(sink, project, load_plugin,
                                                         agent) -> None:
    """Two identical holds are ordinary iteration; the third is a papercut, and
    a fourth must not turn it into a pile of them."""
    harness = load_plugin("continuation")

    _holds(harness, agent, 2)
    assert sink.calls == [], "below the threshold nothing is filed"

    _holds(harness, agent, 1)
    assert len(sink.calls) == 1, "the threshold files exactly one row"
    row = sink.calls[0]
    assert row["source"] == "continuation" and row["kind"] == "repeated_hold"
    assert row["identity"] == _digest(TODO_BLOCKER)
    assert row["summary"] == "continuation held 3x on the same blocker"
    assert TODO_BLOCKER in row["detail"] and len(row["detail"]) <= 1_000
    assert row["session_id"] == SESSION

    _holds(harness, agent, 1)
    assert len(sink.calls) == 1, "one row per blocker per session, not per hold"

    state = _friction(harness, agent)
    assert state["reported"] == 1 and state["last_reason"] == "filed"
    assert state["hold_counts"] == {_digest(TODO_BLOCKER): 4}


def test_friction_state_is_durable_in_the_session_record(sink, project, load_plugin,
                                                         agent) -> None:
    """The streak and the filed set survive the hook: they live in the record,
    not in plugin memory."""
    harness = load_plugin("continuation")
    _holds(harness, agent, 3)

    record = harness.context.state.get(f"session:{SESSION}") or {}
    friction = record.get("friction") or {}
    assert friction.get("reported") == [_digest(TODO_BLOCKER)]
    assert friction.get("hold_counts") == {_digest(TODO_BLOCKER): 3}


def test_a_different_blocker_resets_the_streak(sink, project, load_plugin, agent) -> None:
    """Only blockers present in THIS hold carry a streak forward: an interleaved
    different blocker is a new streak, not a longer one."""
    harness = load_plugin("continuation")

    _holds(harness, agent, 2)
    agent._todo_store = _FakeTodos([_todo("1", "finish the widget"),
                                    _todo("2", "sharpen the bolt")])
    _holds(harness, agent, 1)
    assert sink.calls == []

    agent._todo_store = _FakeTodos([_todo("1", "finish the widget")])
    _holds(harness, agent, 2)
    assert sink.calls == [], "the interrupted streak counts from one again"
    _holds(harness, agent, 1)

    assert len(sink.calls) == 1
    assert sink.calls[0]["identity"] == _digest(TODO_BLOCKER)
    state = _friction(harness, agent)
    assert _digest(OTHER_BLOCKER) not in state["hold_counts"], \
        "a blocker that stopped holding the turn keeps no streak"


def test_threshold_zero_never_files(sink, project, load_plugin, agent) -> None:
    harness = load_plugin("continuation", settings={"friction_after_holds": 0})
    _holds(harness, agent, 5)
    assert sink.calls == []


def test_malformed_threshold_keeps_the_default_and_files(sink, project, load_plugin,
                                                         agent) -> None:
    """Garbage config never silences a producer that was configured on."""
    harness = load_plugin("continuation", settings={"friction_after_holds": "often"})
    _holds(harness, agent, 3)
    assert len(sink.calls) == 1
    assert _friction(harness, agent)["threshold"] == 3


def test_continuation_files_nothing_when_no_sink_can_load(project, load_plugin, agent,
                                                          no_sink,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """No papercuts is a normal configuration: the gate holds as always, nothing
    raises, and the status says plainly why nothing was filed."""
    harness = load_plugin("continuation")
    monkeypatch.setattr(harness.module, "_friction_module", lambda: None)

    _holds(harness, agent, 4)
    state = _friction(harness, agent)
    assert state["reported"] == 0
    assert state["last_reason"] == "friction_unavailable"
    assert state["unavailable"] is True


# --- loop-guard: the spent budget ---------------------------------------------

GUARD_SESSION = "budget-owner"
GUARD_BASE_SETTINGS = {"tool_call_streak": 3, "redirect_cap": 1}


def _guard(load_plugin, settings: dict[str, object]):
    """A live owned agent plus the guard under test, inside the binding the
    guard requires (the same shape test_loop_recovery uses)."""
    from run_agent import AIAgent

    harness = load_plugin("loop-guard", settings)
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = GUARD_SESSION
    agent._current_turn_id = "turn-a"
    agent._persist_disabled = False
    agent._interrupt_requested = False
    return harness, agent


@pytest.fixture
def guard(load_plugin):
    harness, agent = _guard(load_plugin, dict(GUARD_BASE_SETTINGS))
    with _bound(agent):
        yield harness, agent


def _failures(harness, agent, attempts: int = 3) -> None:
    """Three identical structured failures: the guard's own detection threshold."""
    for attempt in range(attempts):
        with _bound(agent):
            harness.hook("post_tool_call", tool_name="web_search",
                         args={"query": f"attempt {attempt}"}, session_id=GUARD_SESSION,
                         task_id="budget-task",
                         result={"ok": False, "code": "backend_unavailable",
                                 "error": "connection refused"})


def _attempt(harness, agent, marker: str) -> list[object]:
    with _bound(agent):
        return harness.hook("pre_tool_call", tool_name="web_search",
                            args={"query": f"try {marker}"}, session_id=GUARD_SESSION,
                            task_id="budget-task", tool_call_id=f"tc-{marker}")


def _guard_status(harness) -> dict[str, object]:
    return json.loads(harness.tool("loop_recovery", {"action": "status"},
                                   session_id=GUARD_SESSION, task_id="budget-task"))


def test_first_exhaustion_files_one_budget_exhausted_row(sink, guard) -> None:
    """The block that could not be emitted is the friction; the one that was
    delivered is ordinary intervention."""
    harness, agent = guard
    _failures(harness, agent)

    blocked = _attempt(harness, agent, "first")
    assert any(isinstance(row, dict) and row.get("action") == "block" for row in blocked)
    assert sink.calls == [], "an intervention that was delivered is not friction"

    _attempt(harness, agent, "second")
    assert len(sink.calls) == 1, "the spent budget is filed once"
    row = sink.calls[0]
    assert row["source"] == "loop-guard" and row["kind"] == "budget_exhausted"
    assert row["summary"] == "loop-guard intervention budget exhausted"
    assert row["session_id"] == GUARD_SESSION
    assert len(row["identity"]) == 64, "identity is the blocker-invariant digest"
    assert json.loads(row["detail"])["invariant"] == FAILING_INVARIANT

    _attempt(harness, agent, "third")
    _attempt(harness, agent, "fourth")
    assert len(sink.calls) == 1, "repeated exhaustion on the same blocker stays one row"

    assert _guard_status(harness)["friction"] == {
        "enabled": True, "reported": 1, "last_reason": "filed", "unavailable": False}


def test_friction_off_files_nothing(load_plugin, sink) -> None:
    harness, agent = _guard(load_plugin, {**GUARD_BASE_SETTINGS, "friction": False})
    with _bound(agent):
        _failures(harness, agent)
        for marker in ("first", "second", "third"):
            _attempt(harness, agent, marker)

        assert sink.calls == []
        assert _guard_status(harness)["friction"] == {
            "enabled": False, "reported": 0, "last_reason": None, "unavailable": False}


def test_malformed_friction_setting_keeps_reporting(load_plugin, sink) -> None:
    harness, agent = _guard(load_plugin, {**GUARD_BASE_SETTINGS, "friction": "yes"})
    with _bound(agent):
        _failures(harness, agent)
        _attempt(harness, agent, "first")
        _attempt(harness, agent, "second")

    assert len(sink.calls) == 1, "garbage config never silences a producer"


def test_loop_guard_files_nothing_when_no_sink_can_load(guard, no_sink,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    harness, agent = guard
    monkeypatch.setattr(harness.module, "_friction_module", lambda: None)

    _failures(harness, agent)
    _attempt(harness, agent, "first")
    _attempt(harness, agent, "second")

    state = _guard_status(harness)["friction"]
    assert state["reported"] == 0
    assert state["last_reason"] == "friction_unavailable"
    assert state["unavailable"] is True


def test_loop_guard_status_always_carries_the_friction_block(load_plugin, sink) -> None:
    """The producer's accounting is session-scoped, so status answers it with no
    live agent to describe: a consumer must read one stable shape, enabled or
    not, agent present or not."""
    harness, _agent = _guard(load_plugin, dict(GUARD_BASE_SETTINGS))

    # No _bound() here on purpose: this is the no-owned-agent path.
    status = _guard_status(harness)
    assert status["ok"] is False, "the agent-owned half is unavailable here"
    assert status["friction"] == {"enabled": True, "reported": 0,
                                   "last_reason": None, "unavailable": False}
