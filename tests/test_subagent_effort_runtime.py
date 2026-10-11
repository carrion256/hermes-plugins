"""subagent-effort runtime regressions: async classification, leak-free
stop, unload/late-result inertness, bounded fail-open overload.

Deterministic throughout: threading.Event park/gate signals, no sleeps, no
timing thresholds. The classifier HTTP boundary is the fake urlopen
(socket.connect is blocked repo-wide by conftest); the fake parks INSIDE
the response-body read, and the test waits until the worker is provably
parked (Event set) BEFORE releasing it, so 'pending' is observed with
certainty and 'settled' with equal certainty. Multi-worker tests install
one per-worker fake, so a parked worker holds its classifier slot while
the test observes it."""

import json
import threading
from inspect import getclosurevars
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.subagent_lifecycle import bind_subagent_parent


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


def _stalled_reply(parked, gate, verdict="complex"):
    """Fake urlopen result: the response-body read is the stall point —
    parked.set() proves the worker reached the boundary (and is holding
    its classifier slot), gate.wait() parks it until the test releases."""
    class StalledReply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            parked.set()
            gate.wait()
            return json.dumps({"difficulty": verdict}).encode()
    return StalledReply()


def _install_stalled_fake(monkeypatch):
    """Install one per-worker stall fake; returns (parked, gate)."""
    parked, gate = threading.Event(), threading.Event()
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _stalled_reply(parked, gate))
    monkeypatch.setenv("CLASSIFIER_API_KEY", "test-key")
    return parked, gate


def _settle(h, timeout=10):
    """Wait for every classifier attempt started so far to settle. The
    jobs map is drained only AFTER the row update (inside the plugin
    lock), so waiting for every Event guarantees all row updates are
    applied. The gate releases are performed by the test itself, which
    knows how many workers are parked."""
    for job in dict(_internals(h)["jobs"]).values():
        assert job.wait(timeout), "classifier worker did not settle"


def test_stalled_classifier_does_not_block_child_launch(load_plugin, monkeypatch):
    """The launch path (native invoke_hook) returns before any classifier
    I/O, and classification stays pending while the classifier is parked
    inside the response read; releasing it settles the row."""
    parked, gate = _install_stalled_fake(monkeypatch)
    h = load_plugin("subagent-effort")
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


def test_no_classification_leaves_no_live_bookkeeping(load_plugin, monkeypatch):
    """The original bug shape: with the classifier unreachable the stop
    hook used to return early, leaking child_sessions/usage_totals forever.
    Stop must ALWAYS clear them even with the classification never landing."""
    parked, gate = _install_stalled_fake(monkeypatch)
    h = load_plugin("subagent-effort")
    internals = _internals(h)
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


def test_stop_before_classification_retains_observed_evidence(load_plugin, monkeypatch):
    """Stop wins over the stalled classifier: origin/outcome/cost stay on
    the row, classification stays honestly pending; the late verdict
    enriches the SAME retained row without resurrecting live tracking."""
    parked, gate = _install_stalled_fake(monkeypatch)
    h = load_plugin("subagent-effort")
    internals = _internals(h)
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


def test_unload_and_late_results_cannot_resurrect_state(load_plugin, monkeypatch):
    """Native disposal (manager.unload -> ctx.on_unload callback) releases
    live bookkeeping; the parked worker's late completion is inert."""
    parked, gate = _install_stalled_fake(monkeypatch)
    h = load_plugin("subagent-effort")
    internals = _internals(h)
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


def test_evicted_row_cannot_be_resurrected(load_plugin, monkeypatch):
    """The row cap evicts; a late verdict for the evicted child lands
    nowhere, and the live row keeps its own result."""
    h = load_plugin("subagent-effort")
    internals = _internals(h)
    monkeypatch.setattr(h.module, "_MAX_ROWS", 1)  # force eviction headroom

    parked_x, gate_x = _install_stalled_fake(monkeypatch)
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
           child_goal="complex-marker x")
    assert parked_x.wait(timeout=10)  # worker x parked
    assert len(internals["jobs"]) == 1
    # a second child (per-worker fake so both park independently)...
    parked_y, gate_y = _install_stalled_fake(monkeypatch)
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


def test_overload_is_bounded_and_fails_open(load_plugin, monkeypatch):
    """4 concurrent classifier slots, held open; the next 2 children fail
    open to an honest 'overloaded' unavailable row with no backlog."""
    h = load_plugin("subagent-effort")
    internals = _internals(h)
    held = []
    for i in range(4):
        # a fresh per-worker fake per child: the worker parks inside the
        # response read (parked.set proves it) and HOLDS its slot on
        # gate.wait() while the test observes
        parked, gate = _install_stalled_fake(monkeypatch)
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


def test_explicit_effort_untouched_and_origin_captured_stalled(load_plugin, monkeypatch):
    """While the classifier is parked, the child's explicit reasoning
    config is observed untouched, origin is recorded, and the late verdict
    records the raw suggestion WITHOUT clamping to an invented level."""
    parked, gate = _install_stalled_fake(monkeypatch)
    h = load_plugin("subagent-effort")
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


def test_pure_bookkeeping_persistence_is_throttled(load_plugin, monkeypatch):
    """Pending-row bookkeeping is rate-limited; the suppressed row still
    reaches the durable store on the next snapshot write (coalesced, not
    lost), and verdict/worker writes below remain durable anyway."""
    parked, gate = _install_stalled_fake(monkeypatch)
    h = load_plugin("subagent-effort")
    monkeypatch.setattr(h.module, "time", _FakeClock())
    writes = []
    from hermes_cli.plugins_state import PluginState
    real_set = PluginState.set
    monkeypatch.setattr(PluginState, "set",
                        lambda self, key, value: writes.append(key) or real_set(self, key, value))
    for child in ("child-a", "child-b"):
        h.hook("subagent_start", parent_session_id="p", child_session_id=child,
               child_goal="complex-marker do work")
    assert writes == ["shadow"]  # child-b's bookkeeping write coalesced away
    # both rows observable live
    assert set(_rows(h)) == {"child-a", "child-b"}
    clock = h.module.time
    clock.now += h.module._BOOKKEEPING_INTERVAL + 1
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-c",
           child_goal="complex-marker do work")
    assert writes == ["shadow", "shadow"]
    # the coalesced write carried child-b's row after all: coalesced, not lost
    assert "child-b" in h.context.state.get("shadow")
    gate.set()  # cleanup: settle the parked workers
    _settle(h)
