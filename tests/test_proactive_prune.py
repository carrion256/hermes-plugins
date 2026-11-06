"""Proactive mid-session eviction on the host's turn_preflight seam.

Covers the ProactivePruneMixin policy for BOTH engines (fast-compaction and
chain-compaction): mode off keeps the host behaviour, fifo drops oldest
unpinned pairs with zero network calls, classifier runs the keep/drop pass and
degrades to fifo per-turn when the classifier faults. Host gates (trigger,
reclaim floor, regrowth rearm) must behave exactly as the base defines them.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
PLUGINS = TESTS.parent / "plugins"


def _load(path, name, pkg=False):
    if pkg:
        spec = importlib.util.spec_from_file_location(name, path / "__init__.py",
                                                      submodule_search_locations=[str(path)])
    else:
        spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pruner = _load(PLUGINS / "fast-compaction" / "pruner.py", "pp_pruner")
chain_mod = _load(PLUGINS / "chain-compaction", "pp_chain", pkg=True)

import sys
sys.path.insert(0, str(TESTS))
from test_fast_compaction import _scripted, _verdicts, transcript  # noqa: E402


def _prepare(engine):
    """Small windows so the 14-message transcript clears the tail-size guard;
    deterministic estimator; no session DB (archive path skipped)."""
    engine.protect_last_n = 2
    engine.quiet_mode = True
    return engine


# ------------------------------------------------------------ fast-compaction

@pytest.fixture
def fc_engine(load_plugin, classifier_stub):
    """Load the overlay over the shared classifier stub.

    ``make(responses=[...])`` scripts one answer per request; with no script every
    request is declined, which is the fail-open path the degraded tests need. The
    recorded transport calls are reachable as ``fc_engine.stub.calls``.
    """
    def make(responses=(), **over):
        stub = (classifier_stub.install(_scripted(responses)) if responses
                else classifier_stub.install())
        make.stub = stub
        settings = {"proactive_trigger_tokens": 1000, **over}
        h = load_plugin("fast-compaction", settings)
        from agent.context_engine import ContextEngine
        from hermes_cli.plugins import get_plugin_context_engine
        engine = get_plugin_context_engine()
        assert isinstance(engine, ContextEngine) and engine.name == "fast-compaction"
        return h, _prepare(engine)
    make.stub = None
    return make


def _status(h):
    return json.loads(h.tool("fast_compaction_status", {}))


def test_off_mode_keeps_host_behaviour(fc_engine):
    # host proactive_prune_tokens is 0 in tests: base returns (messages, 0),
    # no classifier traffic
    h, engine = fc_engine()  # default proactive_prune_mode: off
    msgs = transcript(n_calls=6, result_size=40_000)
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert out is msgs and n == 0 and fc_engine.stub.calls == []
    assert _status(h)["proactive_mode"] == "off"


def test_unknown_mode_falls_back_to_off(fc_engine):
    h, engine = fc_engine(proactive_prune_mode="sideways")
    msgs = transcript(n_calls=6, result_size=40_000)
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert out is msgs and n == 0 and fc_engine.stub.calls == []
    assert _status(h)["proactive_mode"] == "off"


def test_fifo_drops_oldest_pairs_with_zero_network(fc_engine):
    h, engine = fc_engine(proactive_prune_mode="fifo", proactive_target_tokens=25_000)
    msgs = transcript(n_calls=6, result_size=40_000)
    calls = pruner.collect_tool_calls(msgs, 6)
    eligible = sorted(pruner.eligible_calls(calls), key=lambda r: r["assistant_index"])
    before = _status(h)["proactive"]

    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert out is not msgs and n == len(eligible)
    assert fc_engine.stub.calls == []  # the whole point: no classifier traffic
    ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    for record in eligible:
        assert record["call_id"] not in ids
    # recent window untouched; goal text verbatim
    assert any(m.get("content") == msgs[0]["content"] for m in out)
    kept = [m for m in out if m.get("role") == "tool"]
    assert len(kept) == 6 - len(eligible)
    after = _status(h)["proactive"]
    assert after["fifo"] == before["fifo"] + 1 and after["classifier"] == before["classifier"]


def test_fifo_stops_at_target_budget(fc_engine):
    h, engine = fc_engine(proactive_prune_mode="fifo", proactive_target_tokens=5_000)
    msgs = transcript(n_calls=6, result_size=40_000)  # ~10k tokens per result
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert n == 1  # greedy stops once the estimated reclaim clears the target
    assert len([m for m in out if m.get("role") == "tool"]) == 5
    assert fc_engine.stub.calls == []


def test_reclaim_floor_gates_commit(fc_engine):
    # default host floor 4096; a tiny transcript cannot clear it: no-op, input object
    h, engine = fc_engine(proactive_prune_mode="fifo", proactive_target_tokens=10_000)
    engine.proactive_prune_min_reclaim_tokens = 10_000  # raise the floor above what we drop
    msgs = transcript(n_calls=6, result_size=1_000)  # ~250 tokens per pair
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert out is msgs and n == 0
    assert fc_engine.stub.calls == []


def test_rearm_hysteresis_second_call_noops(fc_engine):
    h, engine = fc_engine(proactive_prune_mode="fifo", proactive_target_tokens=25_000)
    msgs = transcript(n_calls=6, result_size=40_000)
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert n > 0
    # same transcript again, unbilled reading: the regrowth runway must suppress it
    out2, n2 = engine.prune_tool_results_only(out, current_tokens=None)
    assert out2 is out and n2 == 0
    assert fc_engine.stub.calls == []


def test_below_trigger_noops(fc_engine):
    h, engine = fc_engine(proactive_prune_mode="fifo")
    msgs = transcript(n_calls=6, result_size=40_000)
    out, n = engine.prune_tool_results_only(msgs, current_tokens=500)  # < trigger 1000
    assert out is msgs and n == 0
    assert fc_engine.stub.calls == []


def test_classifier_mode_uses_verdicts(fc_engine):
    msgs = transcript(n_calls=6, result_size=40_000)
    eligible = pruner.eligible_calls(pruner.collect_tool_calls(msgs, 6))
    keep_id = eligible[0]["call_id"]
    verdicts = _verdicts(eligible, {keep_id: True}, {keep_id: True})
    h, engine = fc_engine(responses=[verdicts],
                          proactive_prune_mode="classifier", proactive_target_tokens=25_000)
    before = _status(h)["proactive"]

    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert len(fc_engine.stub.calls) == 1  # one bounded keep/drop pass
    assert [c["caller"] for c in fc_engine.stub.calls] == ["fast-compaction"]
    assert [c["method"] for c in fc_engine.stub.calls] == ["ask"]  # never classify()
    ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    assert keep_id in ids  # classifier kept it against FIFO's positional order
    for record in eligible[1:]:
        assert record["call_id"] not in ids
    assert n == len(eligible) - 1
    after = _status(h)["proactive"]
    assert after["classifier"] == before["classifier"] + 1


def test_classifier_fault_degrades_to_fifo_same_turn(fc_engine):
    # No scripted answer: every request is declined, which is what a dead service looks
    # like through the shared client. The positional floor still has to clear the turn.
    msgs = transcript(n_calls=6, result_size=40_000)
    eligible = pruner.eligible_calls(pruner.collect_tool_calls(msgs, 6))
    h, engine = fc_engine(proactive_prune_mode="classifier", proactive_target_tokens=25_000)
    before = _status(h)["proactive"]

    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert n == len(eligible)  # positional floor still cleared the stale pairs
    assert len(fc_engine.stub.calls) == 1  # one attempt, then straight to FIFO
    ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    assert not any(r["call_id"] in ids for r in eligible)
    after = _status(h)["proactive"]
    assert after["degraded"] == before["degraded"] + 1
    assert after["fifo"] == before["fifo"] + 1


# ------------------------------------------------------------ chain-compaction

class _FakeAsk:
    """Stand-in for the shared classifier client, injected straight into the engine.

    Records what the prune stage asked, so a test can pin attribution and the explicit
    budget without any transport.
    """

    def __init__(self, answer_fn):
        self._answer_fn = answer_fn
        self.calls: list[dict[str, object]] = []

    def ask(self, state: str, questions: dict[str, object], *, caller: str,
            timeout: float | None = None) -> dict[str, object] | None:
        self.calls.append({"state": state, "questions": questions,
                           "caller": caller, "timeout": timeout})
        return self._answer_fn(state, questions)


def _chain_engine(**over):
    settings = dict(chain_mod.stages.DEFAULTS)
    settings.update({"proactive_trigger_tokens": 1000, **over})
    fp = chain_mod._load_fast_compaction()
    return chain_mod._engine_class()(settings, fp, None), fp


def test_chain_fifo_drops_oldest_with_zero_posts():
    engine, fp = _chain_engine(proactive_prune_mode="fifo", proactive_target_tokens=25_000)
    _prepare(engine)
    msgs = transcript(n_calls=6, result_size=40_000)
    eligible = sorted(fp.eligible_calls(fp.collect_tool_calls(msgs, 6)),
                      key=lambda r: r["assistant_index"])
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert out is not msgs and n == len(eligible)
    ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    assert not any(r["call_id"] in ids for r in eligible)
    assert engine._cc_client is None  # FIFO must not need the client at all


def test_chain_classifier_mode_uses_prune_stage():
    engine, fp = _chain_engine(proactive_prune_mode="classifier", proactive_target_tokens=25_000)
    _prepare(engine)
    msgs = transcript(n_calls=6, result_size=40_000)
    eligible = fp.eligible_calls(fp.collect_tool_calls(msgs, 6))
    keep_id = eligible[0]["call_id"]
    verdicts = _verdicts(eligible, {keep_id: True}, {keep_id: True})

    client = _FakeAsk(lambda state, questions: verdicts)
    engine._cc_client = client
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert len(client.calls) == 1
    assert [c["caller"] for c in client.calls] == ["chain-compaction"]
    assert all(isinstance(c["timeout"], float) for c in client.calls)  # its own budget
    ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    assert keep_id in ids and n == len(eligible) - 1


def test_chain_classifier_down_degrades_to_fifo():
    engine, fp = _chain_engine(proactive_prune_mode="classifier", proactive_target_tokens=25_000)
    _prepare(engine)
    msgs = transcript(n_calls=6, result_size=40_000)
    eligible = fp.eligible_calls(fp.collect_tool_calls(msgs, 6))

    client = _FakeAsk(lambda state, questions: None)  # None == service declined the request
    engine._cc_client = client
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert n == len(eligible)
    assert len(client.calls) == 1  # one attempt, no retry loop
    ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    assert not any(r["call_id"] in ids for r in eligible)


def test_chain_off_mode_delegates_to_host():
    engine, _ = _chain_engine()  # default proactive_prune_mode: off
    _prepare(engine)
    msgs = transcript(n_calls=6, result_size=40_000)
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert out is msgs and n == 0  # host base with proactive_prune_tokens=0
