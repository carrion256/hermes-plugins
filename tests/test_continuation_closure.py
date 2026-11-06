"""Rule-obligation hold + the shared closure reader (plugins/continuation).

Two contracts:

1. ``pre_verify`` holds on obligations the rules plugin recorded — every owed
   ref needs a BOUND acceptance declaration whose ``refs`` contain that exact
   ref or whose check identity equals it (cancelling a covering declaration is
   not coverage), the hold is counted in ``record["blockers"]`` and the message
   names ``rule → ref → paths`` plus the exact ``acceptance`` call that would
   satisfy it. ``rule_obligations: false`` or an unavailable store means no
   hold, never a silent half-gate.
2. ``closure.read_closure`` is the ONE typed reader of "did this session
   close?": ``closed`` needs a clean review AND every required criterion bound
   AND no blockers — one test per failing leg. ``acceptance status`` reports
   through that same reader.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import ModuleType

import pytest
from agent.subagent_lifecycle import bind_subagent_parent
from agent.verification_evidence import record_terminal_result
from test_rule_obligations import REF, SESSION, _store
from tools.todo_tool import TodoStore

CLOSURE_FILE = (Path(__file__).resolve().parents[1] / "plugins" / "continuation"
                / "closure.py")
CLOSURE_MODULE = "hermes_plugins_continuation_closure"
RULE_ID = "rule:0123456789abcdef01234567"
RULE_PATH = str(Path("/rules") / "guard.md")


def _closure() -> ModuleType:
    shared = sys.modules.get(CLOSURE_MODULE)
    if isinstance(shared, ModuleType):
        return shared
    spec = importlib.util.spec_from_file_location(CLOSURE_MODULE, CLOSURE_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[CLOSURE_MODULE] = module
    spec.loader.exec_module(module)
    return module


def _record(review=None, acceptance=None, blockers=None, findings=None, rounds=1):
    record = {"rounds": rounds}
    if review is not None:
        record["review"] = review
    if acceptance is not None:
        record["acceptance"] = acceptance
    if blockers is not None:
        record["blockers"] = blockers
    if findings is not None:
        record["findings"] = findings
    return record


def _clean_review(paths=None):
    return {"verdict": "clean", "identity": "turn:1", "snapshot": {"x": {"status": "passed"}},
            "paths": paths if paths is not None else ["widget.py"],
            "required_coverage": ["1"], "cancellable": [], "acceptance_digest": "sha"}


def _criterion(cid, *, bound=True, refs=(), identity="pytest", disposition=None):
    crit = {"observable": f"{cid} observable", "check_kind": "terminal",
            "check_identity": identity, "paths": ["widget.py"],
            "refs": list(refs), "workspace": "/ws", "generation": 1,
            "declared_at": "2026-11-06T00:00:00+00:00"}
    if bound:
        crit["evidence"] = {"evidence_id": 7, "identity": identity,
                            "workspace": "/ws", "source_hashes": {}}
    if disposition:
        crit["disposition"] = {"status": disposition, "reason": "scope change"}
    return crit


def _reader(record):
    def read(key: str) -> object:
        return {f"session:{SESSION}": record}.get(key)
    return read


def _read(record):
    return _closure().read_closure(SESSION, state_reader=_reader(record))


# --- the closure condition, one leg per test ---------------------------------


def test_clean_bound_and_clear_is_closed():
    closure = _read(_record(review=_clean_review(),
                            acceptance={"1": _criterion("1", refs=[REF])}))
    assert closure is not None and closure.closed
    assert closure.session_id == SESSION
    assert closure.review_verdict == "clean"
    assert closure.review_identity == "turn:1"
    assert closure.review_paths == ("widget.py",)
    assert closure.rounds == 1
    assert [(a.id, a.bound, a.disposition) for a in closure.acceptance] == [("1", True, None)]
    assert [a.check_kind for a in closure.acceptance] == ["terminal"]
    assert closure.unmet == ()


def test_findings_verdict_never_closes():
    review = _clean_review()
    review["verdict"] = "findings"
    closure = _read(_record(review=review, acceptance={"1": _criterion("1")}))
    assert closure is not None and not closure.closed
    assert closure.review_verdict == "findings"


def test_unverified_verdict_never_closes():
    review = _clean_review()
    review["verdict"] = "unverified"
    closure = _read(_record(review=review, acceptance={"1": _criterion("1")}))
    assert closure is not None and not closure.closed
    assert closure.review_verdict == "unverified"


def test_missing_verdict_never_closes():
    closure = _read(_record(acceptance={"1": _criterion("1")}))
    assert closure is not None and not closure.closed
    assert closure.review_verdict is None


def test_unbound_criterion_never_closes_and_is_named():
    closure = _read(_record(review=_clean_review(),
                            acceptance={"1": _criterion("1"),
                                        "2": _criterion("2", bound=False)}))
    assert closure is not None and not closure.closed
    assert [a.id for a in closure.unmet] == ["2"]


def test_outstanding_blocker_never_closes():
    closure = _read(_record(review=_clean_review(),
                            acceptance={"1": _criterion("1")},
                            blockers=["evidence is no longer current"]))
    assert closure is not None and not closure.closed
    assert closure.blockers == ("evidence is no longer current",)
    assert not closure.closed, "staleness reaches closed through the blocker ledger"


def test_cancelled_criterion_is_not_required_proof():
    """An explicitly cancelled criterion needs no evidence; it is exempt from
    the bound requirement (the gate's own cancellation blocker is separate)."""
    closure = _read(_record(review=_clean_review(),
                            acceptance={"1": _criterion("1", bound=False,
                                                       disposition="cancelled")}))
    assert closure is not None and closure.closed


def test_unknown_kind_and_disposition_are_no_value_at_all():
    """A closed set is parsed, not coerced: an unknown value becomes no value
    (never an empty string that could be mistaken for a judgement), and it
    certainly does not excuse a missing proof."""
    criterion = _criterion("1", bound=False)
    criterion["check_kind"] = "vibes"
    criterion["disposition"] = {"status": "meh"}
    closure = _read(_record(review=_clean_review(), acceptance={"1": criterion}))
    assert closure is not None and not closure.closed
    assert [(a.check_kind, a.disposition) for a in closure.acceptance] == [(None, None)]
    assert [a.id for a in closure.unmet] == ["1"]


def test_absent_session_has_no_closure():
    assert _closure().read_closure(SESSION, state_reader=lambda key: None) is None
    assert _closure().read_closure(SESSION, state_reader=lambda key: "not a record") is None
    assert _closure().read_closure("", state_reader=lambda key: {}) is None


def test_outcome_is_inferred_from_what_the_gate_left_behind():
    carried = _read(_record(findings=["real defect"], blockers=["carried"]))
    assert carried is not None and carried.review_verdict == "findings"
    assert not carried.closed
    stalled = _read(_record(blockers=["review dispatch failed (unverified)"]))
    assert stalled is not None and stalled.review_verdict == "unverified"
    assert not stalled.closed


def test_malformed_record_degrades_to_safe_defaults():
    closure = _read(_record(review="not a mapping", acceptance=[1, 2],
                            blockers=["ok", 5, None], rounds="many"))
    assert closure is not None
    assert closure.review_verdict == "unverified" and closure.review_identity is None
    assert closure.review_paths == () and closure.rounds == 0
    assert closure.blockers == ("ok",) and closure.acceptance == ()
    assert not closure.closed


def test_reader_failure_reaches_the_caller():
    class Broken:
        def __call__(self, key: str) -> object:
            raise RuntimeError("state store unreadable")

    with pytest.raises(RuntimeError, match="state store unreadable"):
        _closure().read_closure(SESSION, state_reader=Broken())


# --- the obligation hold -----------------------------------------------------


class FakeAgent:
    def __init__(self) -> None:
        self.session_id = SESSION
        self.platform = "cli"
        self.model = "test-model"
        self._todo_store = TodoStore()


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n",
                                             encoding="utf-8")
    (tmp_path / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr("agent.runtime_cwd.resolve_context_cwd", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def agent():
    return FakeAgent()


def _hook(harness, agent, *, paths=None):
    with bind_subagent_parent(agent):
        return harness.hook(
            "pre_verify", session_id=SESSION, coding=True, attempt=0,
            platform="cli", model="m", final_response="I'm done.",
            changed_paths=paths if paths is not None else ["widget.py"])


def _owe(ref=REF, paths=("widget.py",), rule_id=RULE_ID, name="guard"):
    store = _store()
    store.record(SESSION, store.Obligation(
        rule_id=store.RuleId(rule_id), rule_name=name, source=RULE_PATH,
        refs=(ref,),
        paths=paths, recorded_at=store.now_iso()))
    return store.for_session(SESSION)


def _tool(harness, **args):
    return json.loads(harness.tool("acceptance", args, session_id=SESSION,
                                   task_id="task-1"))


def _declare(harness, cid, *, refs=(), identity="pytest"):
    return _tool(harness, action="declare", id=cid,
                 observable=f"{cid} observable", check_kind="terminal",
                 check_identity=identity, paths=["widget.py"], refs=list(refs))


def _observe_and_bind(harness, agent, project, cid):
    """A genuine observed check whose native row the plugin itself validates."""
    args = {"command": "pytest"}
    with bind_subagent_parent(agent):
        harness.hook("pre_tool_call", tool_name="terminal", session_id=SESSION,
                     tool_call_id=f"tc-{cid}", args=args)
        evidence = record_terminal_result(command="pytest", cwd=project,
                                         session_id=SESSION, exit_code=0,
                                         output="1 passed")
        assert evidence is not None
        harness.hook("post_tool_call", tool_name="terminal", session_id=SESSION,
                     tool_call_id=f"tc-{cid}", args=args,
                     result={"exit_code": 0, "verification_evidence": {
                         "id": evidence["id"], "status": "passed", "kind": "test",
                         "canonical_command": evidence["canonical_command"]}})
    bound = _tool(harness, action="bind", id=cid, evidence_id=evidence["id"])
    assert bound["success"] is True, bound.get("error")


def _holds(harness, agent) -> str:
    results = _hook(harness, agent)
    return " ".join(str(r.get("message", "")) for r in results if isinstance(r, dict))


def test_owed_ref_holds_the_turn_with_the_exact_call(project, load_plugin, agent):
    owed = _owe()
    harness = load_plugin("continuation")

    message = _holds(harness, agent)
    assert "rule-declared acceptance obligations" in message.lower()
    assert RULE_ID in message and repr(REF) in message
    assert RULE_PATH.split("/")[-1] in message or "guard" in message
    assert owed[0].paths[0] in message, "the hold names the paths the rule touched"
    assert '"action":"declare"' in message and f'"check_identity":"{REF}"' in message
    assert '"action":"bind"' in message and '"evidence_id"' in message

    record = harness.context.state.get(f"session:{SESSION}") or {}
    assert any(RULE_ID in str(b) for b in record.get("blockers") or []), \
        "the hold is counted like every other blocker, so the output is labelled"


def test_bound_declaration_covering_the_ref_releases_the_hold(project, load_plugin, agent):
    _owe()
    harness = load_plugin("continuation")
    assert _declare(harness, "1", refs=[REF])["success"] is True
    _observe_and_bind(harness, agent, project, "1")

    message = _holds(harness, agent)
    assert "acceptance obligations" not in message.lower(), message
    assert RULE_ID not in message


def test_check_identity_equality_also_covers_the_ref(project, load_plugin, agent):
    """Coverage by identity: the obligation ref IS the declared check identity,
    so the bound native row for that exact command satisfies the rule."""
    _owe(ref="pytest")
    harness = load_plugin("continuation")
    assert _declare(harness, "1")["success"] is True
    _observe_and_bind(harness, agent, project, "1")

    message = _holds(harness, agent)
    assert "acceptance obligations" not in message.lower(), message


def test_unrelated_bound_declaration_does_not_cover_the_ref(project, load_plugin, agent):
    _owe()
    harness = load_plugin("continuation")
    assert _declare(harness, "1", refs=["pytest tests/test_other.py"])["success"] is True
    _observe_and_bind(harness, agent, project, "1")

    message = _holds(harness, agent)
    assert "acceptance obligations" in message.lower() and RULE_ID in message


def test_cancelling_the_covering_declaration_is_not_coverage(project, load_plugin, agent):
    """An unbound declaration covers nothing; explicit cancellation neither
    proves nor excuses the rule's own check."""
    _owe()
    harness = load_plugin("continuation")
    assert _declare(harness, "1", refs=[REF])["success"] is True
    assert _tool(harness, action="disposition", id="1",
                 disposition="cancelled", reason="not my scope")["success"] is True

    message = _holds(harness, agent)
    assert "acceptance obligations" in message.lower() and RULE_ID in message


def test_setting_off_removes_the_hold(project, load_plugin, agent):
    _owe()
    harness = load_plugin("continuation", settings={"rule_obligations": False})

    message = _holds(harness, agent)
    assert "acceptance obligations" not in message.lower()


def test_malformed_setting_keeps_the_hold(project, load_plugin, agent):
    _owe()
    harness = load_plugin("continuation", settings={"rule_obligations": "maybe"})

    message = _holds(harness, agent)
    assert "acceptance obligations" in message.lower() and RULE_ID in message


def test_unavailable_store_means_no_hold(project, load_plugin, agent, monkeypatch):
    _owe()
    harness = load_plugin("continuation")
    monkeypatch.setattr(harness.module, "_path_load", lambda *a, **k: None)

    message = _holds(harness, agent)
    assert "acceptance obligations" not in message.lower(), \
        "an absent rules plugin is no obligations, never a hold"


# --- status reports through the one reader -----------------------------------


def test_status_reports_the_shared_reader_result(project, load_plugin, agent):
    harness = load_plugin("continuation")
    harness.context.state.set(
        f"session:{SESSION}",
        _record(review=_clean_review(), acceptance={"1": _criterion("1", refs=[REF])},
                rounds=2))

    with bind_subagent_parent(agent):
        status = json.loads(harness.tool("acceptance", {"action": "status"},
                                         session_id=SESSION))
    expected = _read(harness.context.state.get(f"session:{SESSION}"))
    # The tool speaks JSON, so the shared reader's result is compared in its
    # wire form — same values, same structure, one implementation.
    assert status["closure"] == json.loads(json.dumps(asdict(expected)))
    assert status["closure"]["closed"] is True
    assert status["closure"]["rounds"] == 2


def test_status_without_a_record_reports_no_closure(project, load_plugin, agent):
    harness = load_plugin("continuation")
    with bind_subagent_parent(agent):
        status = json.loads(harness.tool("acceptance", {"action": "status"},
                                         session_id=SESSION))
    assert status["success"] is True and status["closure"] is None
