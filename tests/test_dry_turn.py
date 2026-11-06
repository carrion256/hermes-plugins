"""#10 — /dry: meta-classifier withholds mutating tool calls for the turn.

The keyword and the enforcement live in different plugins, so the behaviour under
test is the join: keyword-steering publishes the turn's activations,
meta-classifier reads them through the shared ``turnstate`` module, refuses what
its OWN effect analysis calls mutating, and records the refusal so
``steering_status`` can show it. A /dry turn that silently let a write through —
or that spent a classifier call to find out — is the failure this guards.
"""
import json

import pytest
from test_steering_modes import _turn

MUTATING = [("write_file", {"path": "notes.md", "content": "hello"}),
            ("patch", {"path": "notes.md", "patch": "*** Begin Patch\n-x\n+y\n*** End Patch"}),
            ("write_file", {"path": "notes.md", "content": "x", "mode": "delete"}),
            ("terminal", {"command": "rm -rf build"})]
READ_ONLY = [("read_file", {"path": "notes.md"}),
             ("search_files", {"pattern": "needle"}),
             ("process_manage", {"action": "poll", "session_id": "p1"})]


def _load(load_plugin, classifier_stub, *, dry=True):
    """Both plugins through the real loader, with the shared transport standing in first.

    keyword-steering loads first, so the shared turn-state module exists before
    meta-classifier registers. The declining stand-in goes in BEFORE either registration:
    a plugin resolves its classifier client once, at register, so a module registered
    afterwards would be a second client the plugin never uses, and the request record the
    tests below read would prove nothing. Returns the stand-in as well as the plugins.
    """
    stub = _no_classifier(classifier_stub)
    steer = load_plugin("keyword-steering", settings={})
    mc = load_plugin("meta-classifier", {"dry_turn": dry})
    return steer, mc, stub


def _call(mc, session_id, tool, args, turn_id="t-1"):
    """One screened tool call; the single directive the gate issued, or ``None``.

    ``invoke_hook`` collects handler results, and the risk gate is the only
    ``pre_tool_call`` handler here — a second one would make this ambiguous, and
    this file would then be asserting nothing in particular. No subagent parent is
    bound: unlike the risk-screening tests, this file never reaches the agent —
    which is exactly what the "no classifier call" assertion below proves.
    """
    results = mc.hook("pre_tool_call", tool_name=tool, task_id="task-1",
                      tool_call_id="c1", session_id=session_id, args=args,
                      turn_id=turn_id)
    assert len(results) <= 1, results
    return results[0] if results else None


def _withheld(steer, session_id):
    return json.loads(steer.tool("steering_status", {}, session_id=session_id))["withheld_tool_calls"]


def _no_classifier(classifier_stub):
    """Install the shared-transport stand-in with nothing to answer.

    A transport with no reply installed declines every request -- which is how the shared
    client expresses an unreachable service -- so its record of requests, not a raised
    exception, is the proof: a /dry refusal has to leave that record empty.
    """
    return classifier_stub.install()


def test_dry_blocks_mutating_calls_without_asking_the_model(load_plugin, classifier_stub):
    steer, mc, calls = _load(load_plugin, classifier_stub)
    _turn(steer, "/dry look around", session_id="dry-a", turn_id="t-1")

    for tool, args in MUTATING:
        result = _call(mc, "dry-a", tool, args)
        assert result["action"] == "block" and result["rule_key"] == "dry_turn", (tool, args)
        assert result["message"] == (
            f"DRY TURN: {tool} withheld — mutating operation; re-issue without /dry to apply.")
        assert result["directive_id"].startswith("dry_turn:")
    assert calls.calls == []
    assert [row["tool_name"] for row in _withheld(steer, "dry-a")] == [tool for tool, _ in MUTATING]
    assert all(row["reason"] == "dry_turn" and row["turn_id"] == "t-1"
               for row in _withheld(steer, "dry-a"))


def test_dry_passes_read_only_calls_untouched(load_plugin, classifier_stub):
    steer, mc, _ = _load(load_plugin, classifier_stub)
    _turn(steer, "/dry look around", session_id="dry-b", turn_id="t-1")

    for tool, args in READ_ONLY:
        assert _call(mc, "dry-b", tool, args) is None, (tool, args)
    assert _withheld(steer, "dry-b") == []


def test_the_refusal_is_stable_for_a_call_and_bound_to_its_turn(load_plugin, classifier_stub):
    steer, mc, _ = _load(load_plugin, classifier_stub)
    _turn(steer, "/dry look around", session_id="dry-c", turn_id="t-1")
    tool, args = MUTATING[0]

    first = _call(mc, "dry-c", tool, args)
    again = _call(mc, "dry-c", tool, args)
    assert first["directive_id"] == again["directive_id"]  # auditable, not random

    # A different turn of the same session is a different refusal record.
    _turn(steer, "/dry again", session_id="dry-c", turn_id="t-2")
    assert _call(mc, "dry-c", tool, args)["directive_id"] != first["directive_id"]


def test_dry_withholds_nothing_on_an_ordinary_turn(load_plugin, classifier_stub):
    steer, mc, _ = _load(load_plugin, classifier_stub)
    _turn(steer, "just a question", session_id="dry-d", turn_id="t-1")

    # No /dry this turn: the veto is not consulted, so the call proceeds to the
    # ordinary risk path (which degrades here because no classifier is reachable).
    result = _call(mc, "dry-d", "write_file", {"path": "a.md", "content": "x"})
    assert not (isinstance(result, dict) and result.get("rule_key") == "dry_turn")
    assert _withheld(steer, "dry-d") == []


def test_a_bare_dry_in_prose_withholds_nothing(load_plugin, classifier_stub):
    steer, mc, _ = _load(load_plugin, classifier_stub)
    _turn(steer, "the dry run went fine", session_id="dry-e", turn_id="t-1")

    result = _call(mc, "dry-e", "write_file", {"path": "a.md", "content": "x"})
    assert not (isinstance(result, dict) and result.get("rule_key") == "dry_turn")
    assert _withheld(steer, "dry-e") == []


def test_dry_turn_false_disables_the_veto(load_plugin, classifier_stub):
    steer, mc, _ = _load(load_plugin, classifier_stub, dry=False)
    _turn(steer, "/dry look around", session_id="dry-f", turn_id="t-1")

    result = _call(mc, "dry-f", "write_file", {"path": "a.md", "content": "x"})
    assert not (isinstance(result, dict) and result.get("rule_key") == "dry_turn")
    # The keyword was still declared for the turn, so the status must not pretend
    # nothing happened: it says the turn is active and that nothing was withheld.
    status = json.loads(steer.tool("steering_status", {}, session_id="dry-f"))
    assert "dry" in status["active"]
    assert status["withheld_tool_calls"] == []


def test_bad_dry_turn_setting_refuses_registration(load_plugin):
    with pytest.raises(AssertionError, match="dry_turn"):
        load_plugin("meta-classifier", {"dry_turn": "yes"})


def test_guidance_names_the_enforcer(load_plugin, classifier_stub):
    steer, _mc, _ = _load(load_plugin, classifier_stub)

    out = _turn(steer, "/dry look around", session_id="dry-g", turn_id="t-1")

    assert any("The meta-classifier plugin enforces this" in item["context"] for item in out)


def test_guidance_says_when_nobody_enforces(load_plugin):
    # keyword-steering alone: the read-only promise is guidance, and saying
    # otherwise would credit a veto this process never makes.
    solo = load_plugin("keyword-steering", settings={})

    out = _turn(solo, "/dry look around", session_id="dry-h", turn_id="t-1")

    assert any("not enforcing it right now" in item["context"] for item in out)
    # The keyword itself still works: the turn state is there, and the status
    # reports the mechanism as available rather than pretending it is absent.
    assert json.loads(solo.tool("steering_status", {}, session_id="dry-h"))["turn_state"] == "available"
