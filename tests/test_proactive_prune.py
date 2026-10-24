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
from test_fast_compaction import (call, msg_assistant, msg_tool, msg_user,  # noqa: E402
                                  transcript)


def _patch_client(monkeypatch, responses):
    """urlopen stand-in: consumes responses per /classify call (exceptions raise)."""
    requests = []
    it = iter(responses)

    class _Reply:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def read(self, _n):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, *_a, **_kw):
        requests.append(json.loads(request.data.decode()))
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return _Reply(item)

    monkeypatch.setenv("CLASSIFIER_API_KEY", "synthetic-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return requests


def _verdicts(eligible, keep=None, keep_result=None):
    keep, keep_result = keep or {}, keep_result or {}
    out = {}
    for record in eligible:
        cid = record["call_id"]
        out[f"{record['tag']}_call"] = "keep" if keep.get(cid) else "drop"
        out[f"{record['tag']}_result"] = "keep" if keep_result.get(cid) else "drop"
    return out


def _prepare(engine):
    """Small windows so the 14-message transcript clears the tail-size guard;
    deterministic estimator; no session DB (archive path skipped)."""
    engine.protect_last_n = 2
    engine.quiet_mode = True
    return engine


# ------------------------------------------------------------ fast-compaction

@pytest.fixture
def fc_engine(load_plugin, monkeypatch):
    def make(**over):
        settings = {"proactive_trigger_tokens": 1000, **over}
        h = load_plugin("fast-compaction", settings)
        from agent.context_engine import ContextEngine
        from hermes_cli.plugins import get_plugin_context_engine
        engine = get_plugin_context_engine()
        assert isinstance(engine, ContextEngine) and engine.name == "fast-compaction"
        return h, _prepare(engine)
    return make


def _status(h):
    return json.loads(h.tool("fast_compaction_status", {}))


def test_off_mode_keeps_host_behaviour(fc_engine, monkeypatch):
    # host proactive_prune_tokens is 0 in tests: base returns (messages, 0),
    # no classifier traffic
    h, engine = fc_engine()  # default proactive_prune_mode: off
    requests = _patch_client(monkeypatch, [])
    msgs = transcript(n_calls=6, result_size=40_000)
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert out is msgs and n == 0 and requests == []
    assert _status(h)["proactive_mode"] == "off"


def test_unknown_mode_falls_back_to_off(fc_engine, monkeypatch):
    h, engine = fc_engine(proactive_prune_mode="sideways")
    msgs = transcript(n_calls=6, result_size=40_000)
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert out is msgs and n == 0
    assert _status(h)["proactive_mode"] == "off"


def test_fifo_drops_oldest_pairs_with_zero_network(fc_engine, monkeypatch):
    h, engine = fc_engine(proactive_prune_mode="fifo", proactive_target_tokens=25_000)
    requests = _patch_client(monkeypatch, [])
    msgs = transcript(n_calls=6, result_size=40_000)
    calls = pruner.collect_tool_calls(msgs, 6)
    eligible = sorted(pruner.eligible_calls(calls), key=lambda r: r["assistant_index"])
    before = _status(h)["proactive"]

    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert out is not msgs and n == len(eligible)
    assert requests == []  # the whole point: no classifier traffic
    ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    for record in eligible:
        assert record["call_id"] not in ids
    # recent window untouched; goal text verbatim
    assert any(m.get("content") == msgs[0]["content"] for m in out)
    kept = [m for m in out if m.get("role") == "tool"]
    assert len(kept) == 6 - len(eligible)
    after = _status(h)["proactive"]
    assert after["fifo"] == before["fifo"] + 1 and after["classifier"] == before["classifier"]


def test_fifo_stops_at_target_budget(fc_engine, monkeypatch):
    h, engine = fc_engine(proactive_prune_mode="fifo", proactive_target_tokens=5_000)
    _patch_client(monkeypatch, [])
    msgs = transcript(n_calls=6, result_size=40_000)  # ~10k tokens per result
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert n == 1  # greedy stops once the estimated reclaim clears the target
    assert len([m for m in out if m.get("role") == "tool"]) == 5


def test_reclaim_floor_gates_commit(fc_engine, monkeypatch):
    # default host floor 4096; a tiny transcript cannot clear it: no-op, input object
    h, engine = fc_engine(proactive_prune_mode="fifo", proactive_target_tokens=10_000)
    _patch_client(monkeypatch, [])
    engine.proactive_prune_min_reclaim_tokens = 10_000  # raise the floor above what we drop
    msgs = transcript(n_calls=6, result_size=1_000)  # ~250 tokens per pair
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert out is msgs and n == 0


def test_rearm_hysteresis_second_call_noops(fc_engine, monkeypatch):
    h, engine = fc_engine(proactive_prune_mode="fifo", proactive_target_tokens=25_000)
    _patch_client(monkeypatch, [])
    msgs = transcript(n_calls=6, result_size=40_000)
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert n > 0
    # same transcript again, unbilled reading: the regrowth runway must suppress it
    out2, n2 = engine.prune_tool_results_only(out, current_tokens=None)
    assert out2 is out and n2 == 0


def test_below_trigger_noops(fc_engine, monkeypatch):
    h, engine = fc_engine(proactive_prune_mode="fifo")
    _patch_client(monkeypatch, [])
    msgs = transcript(n_calls=6, result_size=40_000)
    out, n = engine.prune_tool_results_only(msgs, current_tokens=500)  # < trigger 1000
    assert out is msgs and n == 0


def test_classifier_mode_uses_verdicts(fc_engine, monkeypatch):
    h, engine = fc_engine(proactive_prune_mode="classifier", proactive_target_tokens=25_000)
    msgs = transcript(n_calls=6, result_size=40_000)
    calls = pruner.collect_tool_calls(msgs, 6)
    eligible = pruner.eligible_calls(calls)
    keep_id = eligible[0]["call_id"]
    requests = _patch_client(monkeypatch, [_verdicts(eligible, {keep_id: True}, {keep_id: True})])
    before = _status(h)["proactive"]

    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert len(requests) == 1  # one bounded keep/drop pass
    ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    assert keep_id in ids  # classifier kept it against FIFO's positional order
    for record in eligible[1:]:
        assert record["call_id"] not in ids
    assert n == len(eligible) - 1
    after = _status(h)["proactive"]
    assert after["classifier"] == before["classifier"] + 1


def test_classifier_fault_degrades_to_fifo_same_turn(fc_engine, monkeypatch):
    from urllib.error import URLError
    h, engine = fc_engine(proactive_prune_mode="classifier", proactive_target_tokens=25_000)
    _patch_client(monkeypatch, [URLError("connection refused")])
    msgs = transcript(n_calls=6, result_size=40_000)
    calls = pruner.collect_tool_calls(msgs, 6)
    eligible = pruner.eligible_calls(calls)
    before = _status(h)["proactive"]

    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert n == len(eligible)  # positional floor still cleared the stale pairs
    ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    assert not any(r["call_id"] in ids for r in eligible)
    after = _status(h)["proactive"]
    assert after["degraded"] == before["degraded"] + 1
    assert after["fifo"] == before["fifo"] + 1


# ------------------------------------------------------------ chain-compaction

class _FakePost:
    """Chain-style client: post(endpoint, api_key_env, text, schema, timeout)."""

    def __init__(self, payload_fn):
        self.payload_fn = payload_fn
        self.calls = 0

    def post(self, endpoint, api_key_env, text, schema, timeout=None):
        self.calls += 1
        return self.payload_fn(text, schema)


def _chain_engine(monkeypatch, **over):
    settings = dict(chain_mod.stages.DEFAULTS)
    settings.update({"proactive_trigger_tokens": 1000, **over})
    fp, cm = chain_mod._load_fast_compaction()
    return chain_mod._engine_class()(settings, fp, None), fp


def test_chain_fifo_drops_oldest_with_zero_posts(monkeypatch):
    engine, fp = _chain_engine(monkeypatch, proactive_prune_mode="fifo",
                               proactive_target_tokens=25_000)
    _prepare(engine)
    monkeypatch.setenv("CLASSIFIER_API_KEY", "synthetic-key")
    msgs = transcript(n_calls=6, result_size=40_000)
    eligible = sorted(fp.eligible_calls(fp.collect_tool_calls(msgs, 6)),
                      key=lambda r: r["assistant_index"])
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert out is not msgs and n == len(eligible)
    ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    assert not any(r["call_id"] in ids for r in eligible)
    assert engine._cc_client is None  # FIFO must not need the client at all


def test_chain_classifier_mode_uses_prune_stage(monkeypatch):
    engine, fp = _chain_engine(monkeypatch, proactive_prune_mode="classifier",
                               proactive_target_tokens=25_000)
    _prepare(engine)
    monkeypatch.setenv("CLASSIFIER_API_KEY", "synthetic-key")
    msgs = transcript(n_calls=6, result_size=40_000)
    eligible = fp.eligible_calls(fp.collect_tool_calls(msgs, 6))
    keep_id = eligible[0]["call_id"]
    verdicts = _verdicts(eligible, {keep_id: True}, {keep_id: True})

    def payload(text, schema):
        # the stage builds its own request; answer every head with the verdict map
        return {"labels": {}} | verdicts

    engine._cc_client = _FakePost(lambda t, s: verdicts)
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert engine._cc_client.calls == 1
    ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    assert keep_id in ids and n == len(eligible) - 1


def test_chain_classifier_down_degrades_to_fifo(monkeypatch):
    engine, fp = _chain_engine(monkeypatch, proactive_prune_mode="classifier",
                               proactive_target_tokens=25_000)
    _prepare(engine)
    msgs = transcript(n_calls=6, result_size=40_000)
    eligible = fp.eligible_calls(fp.collect_tool_calls(msgs, 6))

    def boom(text, schema):
        return None  # post() returning None == classifier unavailable

    engine._cc_client = _FakePost(boom)
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert n == len(eligible)
    ids = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    assert not any(r["call_id"] in ids for r in eligible)


def test_chain_off_mode_delegates_to_host(monkeypatch):
    engine, _ = _chain_engine(monkeypatch)  # default proactive_prune_mode: off
    _prepare(engine)
    msgs = transcript(n_calls=6, result_size=40_000)
    out, n = engine.prune_tool_results_only(msgs, current_tokens=50_000)
    assert out is msgs and n == 0  # host base with proactive_prune_tokens=0
