"""subagent-effort runtime regressions: async classification, leak-free
stop, unload/late-result inertness, bounded fail-open overload.

Deterministic throughout: threading.Event park/gate signals, no sleeps, no
timing thresholds. The classifier boundary is the shared client from
classifier-core, stood in for by conftest's ``classifier_stub`` (no plugin in
these tests ever reaches a socket). The stand-in parks INSIDE ``ask`` — the
point where the shared client would be waiting on the service — and the test
waits until the worker is provably parked (Event set) BEFORE releasing it, so
'pending' is observed with certainty and 'settled' with equal certainty.
Multi-worker tests hand out one park per child, so a parked worker holds its
classifier slot while the test observes it."""

import hashlib
import importlib.util
import json
import sys
import threading
from inspect import getclosurevars
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from agent.subagent_lifecycle import bind_subagent_parent

_LEDGER_PLUGIN = "shadow-gate"
_LEDGER_FILE = "subagent_effort.difficulty.jsonl"
_ROW_KEYS = sorted(("child_session_id", "verdict", "suggested_effort", "supported_efforts",
                    "clamped_effort", "classification_state", "classification_reason",
                    "child_reasoning_config", "child_model", "child_provider",
                    "outcome_status", "latency_ms", "repeated_mutation_targets", "usage",
                    "verification", "outcome_observed"))
_RAW_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "requests",
                   "cost_usd", "cost_status", "cost_source")


def _internals(h):
    # the plugin scatters its live-state references across nested closures
    # (child_sessions/verdicts in on_subagent_start, usage_totals in
    # on_post_api_request, jobs/closed in the classifier machinery reached
    # via _spawn_classifier); walk the closure graph and merge everything.
    seen = set()

    def walk(obj):
        if id(obj) in seen or not callable(obj):
            return {}
        seen.add(id(obj))
        merged = {}
        try:
            captured = dict(getclosurevars(obj).nonlocals)
        except (TypeError, ValueError):
            return {}
        for value in captured.values():
            merged.update(walk(value))
        merged.update(captured)
        return merged

    merged = {}
    for callbacks in h.manager._hooks.values():
        for callback in callbacks:
            if callback.__module__ == h.module.__name__:
                merged.update(walk(callback))
    return merged


def _rows(h):
    return {row["child_session_id"]: row
            for row in json.loads(h.command("subagent_effort"))["children"]}


class _StalledClassifier:
    """The shared client, parked at the point where it would wait on the service.

    Each started child takes the next park: ``ask`` sets ``parked`` to prove the worker
    reached the classifier boundary and is holding its slot, then waits on ``gate``
    until the test releases it. Parks are handed out in call order, which is the order
    the test starts children in; a call beyond the parks the test planned for answers
    at once rather than hanging the suite. ``callers``/``timeouts`` record what the
    plugin asked with, which is the part of the contract this plugin owns.
    """

    def __init__(self, verdict="complex"):
        self.verdict = verdict
        self.callers: list[str] = []
        self.timeouts: list[float | None] = []
        self._parks: list[tuple[threading.Event, threading.Event]] = []
        self._calls = 0

    def park(self):
        """Reserve the next park; call it before starting the child that will take it."""
        parked, gate = threading.Event(), threading.Event()
        self._parks.append((parked, gate))
        return parked, gate

    def ask(self, state, questions, *, caller, timeout=None):
        self.callers.append(caller)
        self.timeouts.append(timeout)
        if self._calls < len(self._parks):
            parked, gate = self._parks[self._calls]
            self._calls += 1
            parked.set()
            gate.wait()
        return {"answers": {"difficulty": {"type": "choice", "choice": self.verdict}}}


def _stalled_classifier(classifier_stub, verdict="complex"):
    """Install the parking client BEFORE the plugin loads: it resolves its client once.

    Returns the controller; a test takes one ``park()`` per child it starts.
    """
    control = _StalledClassifier(verdict)
    classifier_stub.install(control.ask)
    return control


def _settle(h, timeout=10):
    """Wait for every classifier attempt started so far to settle. The
    jobs map is drained only AFTER the row update (inside the plugin
    lock), so waiting for every Event guarantees all row updates are
    applied. The gate releases are performed by the test itself, which
    knows how many workers are parked."""
    for job in dict(_internals(h)["jobs"]).values():
        assert job.wait(timeout), "classifier worker did not settle"


def test_stalled_classifier_does_not_block_child_launch(load_plugin, classifier_stub):
    """The launch path (native invoke_hook) returns before any classifier
    verdict lands, and classification stays pending while the classifier is
    parked at the shared client boundary; releasing it settles the row."""
    control = _stalled_classifier(classifier_stub)
    h = load_plugin("subagent-effort")
    parked, gate = control.park()
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
           child_goal="complex-marker do work")
    assert parked.wait(timeout=10)  # worker reached the boundary, parked
    row = _rows(h)["child-x"]
    assert row["classification_state"] == "pending"
    assert row["verdict"] is None and row["suggested_effort"] is None
    gate.set()
    _settle(h)
    row = _rows(h)["child-x"]
    assert row["classification_state"] == "classified"
    assert row["verdict"] == "complex"


def test_no_classification_leaves_no_live_bookkeeping(load_plugin, classifier_stub):
    """The original bug shape: with the classifier unreachable the stop
    hook used to return early, leaking child_sessions/usage_totals forever.
    Stop must ALWAYS clear them even with the classification never landing."""
    control = _stalled_classifier(classifier_stub)
    h = load_plugin("subagent-effort")
    internals = _internals(h)
    parked, gate = control.park()
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
           child_goal="complex-marker do work")
    assert parked.wait(timeout=10)
    h.hook("post_api_request", session_id="child-x",
           usage={"prompt_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    h.hook("subagent_stop", parent_session_id="p", child_session_id="child-x",
           child_status="completed", duration_ms=7, tool_call_history=[])
    assert internals["child_sessions"] == set()
    assert internals["usage_totals"] == {}
    row = _rows(h)["child-x"]
    assert row["outcome_status"] == "completed"
    assert row["usage"]["prompt_tokens"] == 10
    gate.set()  # cleanup: classifier may land after stop
    _settle(h)


def test_stop_before_classification_retains_observed_evidence(load_plugin, classifier_stub):
    """Stop wins over the stalled classifier: origin/outcome/cost stay on
    the row, classification stays honestly pending; the late verdict
    enriches the SAME retained row without resurrecting live tracking."""
    control = _stalled_classifier(classifier_stub)
    h = load_plugin("subagent-effort")
    internals = _internals(h)
    parked, gate = control.park()
    agent = SimpleNamespace(
        session_id="child-x", reasoning_config=None, model="m", provider="openai",
        session_estimated_cost_usd=0.25, session_cost_status="estimated")
    with bind_subagent_parent(agent):
        h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
               child_goal="complex-marker do work")
        assert parked.wait(timeout=10)
        h.hook("post_llm_call", session_id="child-x")
        h.hook("pre_llm_call", session_id="child-x", user_message="complex-marker ...")
        h.hook("subagent_stop", parent_session_id="p", child_session_id="child-x",
               child_status="completed", duration_ms=500, tool_call_history=[])
    row = _rows(h)["child-x"]
    assert row["classification_state"] == "pending"
    assert row["outcome_observed"] is True
    assert row["child_model"] == "m"
    assert internals["child_sessions"] == set() and internals["usage_totals"] == {}
    # late completion enriches the retained row...
    gate.set()
    _settle(h)
    row = _rows(h)["child-x"]
    assert row["classification_state"] == "classified" and row["verdict"] == "complex"
    # ...without resurrecting live bookkeeping
    assert internals["child_sessions"] == set()
    assert internals["usage_totals"] == {}


def test_unload_and_late_results_cannot_resurrect_state(load_plugin, classifier_stub):
    """Native disposal (manager.unload -> ctx.on_unload callback) releases
    live bookkeeping; the parked worker's late completion is inert."""
    control = _stalled_classifier(classifier_stub)
    h = load_plugin("subagent-effort")
    internals = _internals(h)
    parked, gate = control.park()
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
           child_goal="complex-marker do work")
    assert parked.wait(timeout=10)
    job = internals["jobs"]["child-x"]
    # native disposal runs the registered on_unload cleanup callback
    h.manager.unload("subagent-effort")
    # late completion attempt against unloaded state is inert: release the
    # parked worker AFTER unload, nothing comes back
    gate.set()
    assert job.wait(timeout=10)
    assert internals["verdicts"]["child-x"]["verdict"] is None
    # jobs was cleared by unload; live bookkeeping stays empty forever
    assert internals["jobs"] == {}
    assert internals["child_sessions"] == set()
    assert internals["usage_totals"] == {}
    # hooks after unload are inert: a late start records nothing live
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-y",
           child_goal="complex-marker late")
    assert internals["child_sessions"] == set()
    assert "child-y" not in internals["verdicts"]


def test_evicted_row_cannot_be_resurrected(load_plugin, classifier_stub, monkeypatch):
    """The row cap evicts; a late verdict for the evicted child lands
    nowhere, and the live row keeps its own result."""
    control = _stalled_classifier(classifier_stub)
    h = load_plugin("subagent-effort")
    internals = _internals(h)
    monkeypatch.setattr(h.module, "_MAX_ROWS", 1)  # force eviction headroom

    parked_x, gate_x = control.park()
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
           child_goal="complex-marker x")
    assert parked_x.wait(timeout=10)  # worker x parked
    assert len(internals["jobs"]) == 1
    # a second child, parked independently of the first...
    parked_y, gate_y = control.park()
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-y",
           child_goal="complex-marker y")
    assert parked_y.wait(timeout=10)
    # ...evicts the first row
    assert internals["verdicts"].get("child-x") is None
    # release both workers: the evicted row's late verdict lands nowhere
    gate_x.set()
    gate_y.set()
    _settle(h)
    assert "child-x" not in internals["verdicts"]
    assert internals["verdicts"]["child-y"]["verdict"] == "complex"


def test_overload_is_bounded_and_fails_open(load_plugin, classifier_stub):
    """4 concurrent classifier slots, held open; the next 2 children fail
    open to an honest 'overloaded' unavailable row with no backlog."""
    control = _stalled_classifier(classifier_stub)
    h = load_plugin("subagent-effort")
    internals = _internals(h)
    held = []
    for i in range(4):
        # one park per child: the worker parks at the classifier boundary
        # (parked.set proves it) and HOLDS its slot on gate.wait() while
        # the test observes
        parked, gate = control.park()
        held.append(gate)
        h.hook("subagent_start", parent_session_id="p", child_session_id=f"c{i}",
               child_goal=f"complex-marker {i}")
        assert parked.wait(timeout=10)
    for i in (4, 5):
        h.hook("subagent_start", parent_session_id="p", child_session_id=f"c{i}",
               child_goal=f"complex-marker {i}")
    rows = internals["verdicts"]
    assert {sid for sid, r in rows.items()
            if r["classification_reason"] == "overloaded"} == {"c4", "c5"}
    assert all(rows[f"c{i}"]["classification_state"] == "pending"
               for i in range(4))
    # releasing the parked workers classifies them; nothing queued grew
    for gate in held:
        gate.set()
    _settle(h)
    assert internals["jobs"] == {}
    assert {sid for sid, r in rows.items()
            if r["classification_state"] == "classified"} == {"c0", "c1", "c2", "c3"}
    assert len(internals["verdicts"]) == 6


def test_explicit_effort_untouched_and_origin_captured_stalled(load_plugin, classifier_stub):
    """While the classifier is parked, the child's explicit reasoning
    config is observed untouched, origin is recorded, and the late verdict
    records the raw suggestion WITHOUT clamping to an invented level."""
    control = _stalled_classifier(classifier_stub)
    h = load_plugin("subagent-effort")
    parked, gate = control.park()
    agent = MagicMock()
    agent.session_id = "child-x"
    agent.reasoning_config = {"enabled": True, "effort": "medium"}
    agent.model = "m"
    agent.provider = "openai"
    agent.api_mode = "chat_completions"
    with bind_subagent_parent(agent):
        h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
               child_goal="complex-marker work")
        assert parked.wait(timeout=10)
        h.hook("pre_llm_call", session_id="child-x", user_message="complex-marker ...")
        # pure shadow: nothing is ever written to the agent
        assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
    row = _rows(h)["child-x"]
    # origin recorded even though classification has not landed
    assert row["classification_state"] == "pending"
    assert row["child_reasoning_config"] == {"enabled": True, "effort": "medium"}
    assert row["clamped_effort"] == "unknown"
    gate.set()
    _settle(h)
    row = _rows(h)["child-x"]
    # late verdict records the raw suggestion but the unknown capability
    # table keeps clamping explicitly unknown — never an invented level
    assert row["verdict"] == "complex" and row["suggested_effort"] == "high"
    assert row["clamped_effort"] == "unknown"
    assert agent.reasoning_config == {"enabled": True, "effort": "medium"}


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


def test_pure_bookkeeping_persistence_is_throttled(load_plugin, classifier_stub, monkeypatch):
    """Pending-row bookkeeping is rate-limited; the suppressed row still
    reaches the durable store on the next snapshot write (coalesced, not
    lost), and verdict/worker writes below remain durable anyway."""
    control = _stalled_classifier(classifier_stub)
    h = load_plugin("subagent-effort")
    monkeypatch.setattr(h.module, "time", _FakeClock())
    writes = []
    from hermes_cli.plugins_state import PluginState
    real_set = PluginState.set
    monkeypatch.setattr(PluginState, "set",
                        lambda self, key, value: writes.append(key) or real_set(self, key, value))
    gates = []
    for child in ("child-a", "child-b"):
        _parked, gate = control.park()  # each child holds its classifier slot
        gates.append(gate)
        h.hook("subagent_start", parent_session_id="p", child_session_id=child,
               child_goal="complex-marker do work")
    assert writes == ["shadow"]  # child-b's bookkeeping write coalesced away
    # both rows observable live
    assert set(_rows(h)) == {"child-a", "child-b"}
    clock = h.module.time
    clock.now += h.module._BOOKKEEPING_INTERVAL + 1
    _parked, gate = control.park()
    gates.append(gate)
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-c",
           child_goal="complex-marker do work")
    assert writes == ["shadow", "shadow"]
    # the coalesced write carried child-b's row after all: coalesced, not lost
    assert "child-b" in h.context.state.get("shadow")
    for gate in gates:
        gate.set()  # cleanup: settle the parked workers
    _settle(h)


def _point_ledger_at(isolated_hermes, root):
    """Configure the shared ledger through shadow-gate's own setting, before any load.

    This is the test's own isolated Hermes home under ``tmp_path`` (never the live
    profile, which the host CLI owns), and the YAML goes through the parser, not
    string surgery: the plugin must resolve the operator-facing setting, not a patch.
    """
    isolated_hermes.config.setdefault("plugins", {}).setdefault("entries", {})[
        _LEDGER_PLUGIN] = {"enabled": True, "settings": {"ledger_dir": str(root)}}
    (isolated_hermes.home / "config.yaml").write_text(
        yaml.safe_dump(isolated_hermes.config), encoding="utf-8")


def _ledger_rows(root):
    """Every row shadow-gate appended for this decision, oldest first."""
    path = root / _LEDGER_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _without_shadow_ledger(monkeypatch):
    """Make the shadow-gate sibling unreadable, exactly as an absent install would.

    The module key is dropped first because a path-loaded sibling stays registered for
    the life of the process; everything else still resolves by path.
    """
    monkeypatch.delitem(sys.modules, "hermes_plugins_shadow_gate", raising=False)
    real_spec = importlib.util.spec_from_file_location

    def no_ledger_spec(name, location, *_args, **_kwargs):
        if str(location).replace("\\", "/").endswith("shadow-gate/ledger.py"):
            return None
        return real_spec(name, location)

    monkeypatch.setattr(importlib.util, "spec_from_file_location", no_ledger_spec)


def test_one_classification_writes_a_verdict_row_and_its_outcome_row(
        load_plugin, classifier_stub, isolated_hermes, tmp_path):
    """The shadow's evidence: the verdict when it lands, the realized result when it stops.

    Both rows carry the same key (the child session) and the verdict row carries the
    digest of the goal that produced it, which is what lets shadow-gate join a verdict
    to the outcome it predicted. The outcome here is an observed clean completion, so
    the higher effort was not needed — the ledger says so in the producer's words.
    """
    root = tmp_path / "ledger"
    _point_ledger_at(isolated_hermes, root)
    control = _stalled_classifier(classifier_stub)
    h = load_plugin("subagent-effort")
    parked, gate = control.park()
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
           child_goal="complex-marker do work")
    assert parked.wait(timeout=10)
    assert control.callers == ["subagent-effort"]  # declines attribute to this plugin
    assert control.timeouts == [10.0]  # the bounded wait this plugin asked for, explicitly
    gate.set()
    _settle(h)
    h.hook("subagent_stop", parent_session_id="p", child_session_id="child-x",
           child_status="completed", duration_ms=40, tool_call_history=[])
    rows = _ledger_rows(root)
    verdicts = [row for row in rows if row["kind"] == "verdict"]
    outcomes = [row for row in rows if row["kind"] == "outcome"]
    assert [(row["decision"], row["key"], row["verdict"]) for row in verdicts] == [
        ("subagent_effort.difficulty", "child-x", "complex")]
    assert verdicts[0]["input_digest"] == hashlib.sha256(
        b"complex-marker do work").hexdigest()[:16]
    assert set(verdicts[0]["meta"]) == {"suggested_effort", "clamped_effort"}
    assert verdicts[0]["meta"]["suggested_effort"] == "high"
    assert [(row["key"], row["outcome"]) for row in outcomes] == [("child-x", "needed_low")]
    assert set(outcomes[0]["meta"]) == {"status", "repeated_mutation_targets", "verification"}
    note = json.loads(h.command("subagent_effort"))["note"]
    assert "shadow-gate rows recorded=2 ledger_unavailable=0" in note


@pytest.mark.parametrize("row,expected", [
    ({"status": "failed"}, "needed_high"),  # an observed failure proves the harder effort
    ({"status": "completed", "repeated_mutation_targets": ["a.py", "a.py"]}, "needed_high"),
    ({"status": "completed", "repair_cycles": 2}, "needed_high"),
    ({"status": "completed"}, "needed_low"),
    ({"status": "completed", "repair_cycles": 1}, "needed_low"),  # one repair is ordinary
    ({"status": "unknown"}, "unknown"),
    ({}, "unknown"),
    ({"status": "completed", "repair_cycles": "2"}, "needed_low"),  # untyped: no evidence
])
def test_realized_outcome_follows_the_evaluators_rule(load_plugin, classifier_stub,
                                                      row, expected):
    """The outcome is read exactly as the paired evaluator reads it: no invented evidence.

    A missing signal is never read as agreement — an unobserved rework count means
    "no evidence of rework", not "no rework" — and a status that is neither an
    observed failure nor an observed success stays unknown.
    """
    classifier_stub.install()
    h = load_plugin("subagent-effort")
    assert h.module.realized_outcome(row) == expected


def test_missing_shadow_gate_sibling_counts_rows_as_unavailable(load_plugin, classifier_stub,
                                                               monkeypatch):
    """Without the ledger plugin the shadow still runs; the gap is counted, never silent.

    Classification, origin and outcome stay recorded; every row that could not be
    written is reported in the command note, because thin evidence has to be visible
    to an operator rather than quietly absent.
    """
    _without_shadow_ledger(monkeypatch)
    control = _stalled_classifier(classifier_stub)
    h = load_plugin("subagent-effort")
    parked, gate = control.park()
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
           child_goal="complex-marker do work")
    assert parked.wait(timeout=10)
    gate.set()
    _settle(h)
    h.hook("subagent_stop", parent_session_id="p", child_session_id="child-x",
           child_status="completed", duration_ms=40, tool_call_history=[])
    assert _rows(h)["child-x"]["verdict"] == "complex"  # the shadow itself is unaffected
    note = json.loads(h.command("subagent_effort"))["note"]
    assert "shadow-gate rows recorded=0 ledger_unavailable=2" in note


def test_command_rows_keep_their_published_keys(load_plugin, classifier_stub):
    """The summary row is an operator-facing contract: the key set may not drift.

    Renaming or dropping a key breaks whatever reads the command; adding one belongs
    with an update to this list, so the list is pinned here rather than inferred.
    """
    control = _stalled_classifier(classifier_stub)
    h = load_plugin("subagent-effort")
    parked, gate = control.park()
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
           child_goal="complex-marker do work")
    assert parked.wait(timeout=10)
    gate.set()
    _settle(h)
    assert sorted(_rows(h)["child-x"]) == _ROW_KEYS
    h.hook("post_api_request", session_id="child-x",
           usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    h.hook("subagent_stop", parent_session_id="p", child_session_id="child-x",
           child_status="completed", duration_ms=40, tool_call_history=[])
    assert sorted(_rows(h)["child-x"]) == sorted([*_ROW_KEYS, *_RAW_USAGE_KEYS])
