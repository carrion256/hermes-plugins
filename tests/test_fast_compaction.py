"""fast-compaction: pure policy (pruner) + engine overlay against a fake classifier."""
from __future__ import annotations

import json

import pytest


def _load_pruner():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "plugins" / "fast-compaction" / "pruner.py"
    spec = importlib.util.spec_from_file_location("fc_pruner_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pruner = _load_pruner()


def msg_user(text, **extra):
    return {"role": "user", "content": text, **extra}


def msg_assistant(text="", calls=None, **extra):
    return {"role": "assistant", "content": text, "tool_calls": calls or [], **extra}


def msg_tool(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def call(cid, name="Read", args='{"file_path":"src/a.ts"}'):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": args}}


def transcript(n_calls=6, result_size=5000, preserve=6):
    """user goal, then n_calls assistant/tool pairs, then a final user turn; the last
    `preserve` messages hold the recent calls (their pairs sit inside the window)."""
    messages = [msg_user("Fix the failing test. Never edit src/generated.")]
    for i in range(n_calls):
        messages.append(msg_assistant("", [call(f"c{i}")]))
        messages.append(msg_tool(f"c{i}", "x" * result_size))
    messages.append(msg_user("Now run the full suite."))
    return messages


# ---------------------------------------------------------------- pairing & policy

def test_collect_pairs_and_pins_recent_and_head():
    messages = transcript(n_calls=6, preserve=6)
    calls = pruner.collect_tool_calls(messages, 6)
    assert len(calls) == 6
    eligible = {c["call_id"] for c in pruner.eligible_calls(calls)}
    # window = [8, 14): c0..c2 sit fully before it and are eligible. A pair is
    # recent if EITHER half is: c3's call row is at 7 (outside) but its result at
    # 8 is inside the window, so dropping it would delete a protected message —
    # pinned (found live 2026-10-24: exactly this boundary dropped the fix's
    # edit_file call). c4/c5 sit in the window and are never touched. The first
    # message itself (index 0) pins any call whose assistant row lands there.
    assert eligible == {"c0", "c1", "c2"}
    assert calls["c3"]["pinned"] is True
    assert calls["c4"]["pinned"] is True and calls["c5"]["pinned"] is True
    head_pinned = pruner.collect_tool_calls(
        [msg_assistant("", [call("hx")])] + [msg_tool("hx", "z" * 400)] * 1
        + [msg_assistant(calls=[call(f"d{i}")]) for i in range(9)]
        + [msg_tool(f"d{i}", "z" * 300) for i in range(9)] + [msg_user("n")], 6)
    assert head_pinned["hx"]["pinned"] is True


def test_orphan_call_pinned_not_dropped():
    messages = [msg_user("goal")]
    for i in range(8):
        messages.append(msg_assistant("", [call(f"c{i}")]))
        if i != 3:  # c3 never gets a result
            messages.append(msg_tool(f"c{i}", "y" * 3000))
    messages.append(msg_user("next"))
    calls = pruner.collect_tool_calls(messages, 6)
    assert calls["c3"]["pinned"] is True and calls["c3"]["result_index"] is None


def test_parse_response_strict_and_batching():
    batch = [{"tag": f"t{i}", "call_id": f"c{i}", "tool": "Read", "arguments": "{}",
              "result_text": "z" * 100} for i in range(2)]
    ok = {"t0_call": "keep", "t0_result": "drop", "t1_call": "drop", "t1_result": "drop"}
    assert pruner.parse_response(ok, batch) == {
        "c0": {"call": True, "result": False}, "c1": {"call": False, "result": False}}
    with pytest.raises(pruner.PruneError):
        pruner.parse_response({**ok, "t1_result": "maybe"}, batch)
    with pytest.raises(pruner.PruneError):
        pruner.parse_response({"t0_call": "keep"}, batch)
    many = [{"tag": f"t{i}", "call_id": f"c{i}", "tool": "T", "arguments": "{}",
             "result_text": ""} for i in range(40)]
    batches = pruner.build_batches(many)
    assert [len(b) for b in batches] == [16, 16, 8]      # 32 heads per request
    for b in batches:
        text, schema = pruner.build_request(b, "STATE")
        assert text == "STATE" and len(schema) == 2 * len(b)


def test_apply_decisions_verbatim_keep_copy_on_write():
    messages = transcript(n_calls=6)
    calls = pruner.collect_tool_calls(messages, 6)
    # c0..c2 are eligible; c3 is pinned (its result half sits inside the recent
    # window) so a drop decision for it is ignored — that IS the guard.
    decisions = {"c0": {"call": True, "result": True},
                 "c1": {"call": True, "result": False},
                 "c2": {"call": False, "result": False},
                 "c3": {"call": False, "result": False}}
    out, stats = pruner.apply_decisions(messages, calls, decisions, truncate_head_chars=300)
    # kept result (messages[2]): the exact same object, not a copy
    assert any(m is messages[2] for m in out)
    # truncated result: head + note, call retained
    truned = [m for m in out if m.get("role") == "tool" and m.get("tool_call_id") == "c1"]
    assert len(truned) == 1 and truned[0]["content"].startswith("x" * 300)
    assert "fast-compaction" in truned[0]["content"] and "x" * 301 not in truned[0]["content"]
    # dropped pair: call and result both gone
    assert not any(m.get("tool_call_id") == "c2" for m in out)
    assert all("c2" not in json.dumps(m.get("tool_calls") or []) for m in out)
    # pinned drop decision did not delete the window-side pair
    assert any(m.get("tool_call_id") == "c3" for m in out)
    # untouched calls remain identical objects
    assert any(m.get("tool_calls") and m["tool_calls"][0]["id"] == "c0" for m in out)
    assert stats["dropped"] == 1 and stats["truncated"] == 1


def test_drop_all_content_removes_message():
    messages = [msg_user("goal")] + [msg_assistant(calls=[call(f"c{i}")]) for i in range(9)]
    messages += [msg_tool(f"c{i}", "r" * 300) for i in range(9)] + [msg_user("next")]
    calls = pruner.collect_tool_calls(messages, 6)
    eligible = {c["call_id"] for c in pruner.eligible_calls(calls)}
    decisions = {cid: {"call": False, "result": False} for cid in calls}
    out, stats = pruner.apply_decisions(messages, calls, decisions, 300)
    for cid in ("c2", "c3"):  # eligible, empty-text pure-call messages vanish
        assert not any(m.get("tool_calls") and m["tool_calls"][0]["id"] == cid for m in out)
    # only the eligible pairs drop; every result inside the recent window is pinned
    assert stats["dropped"] == len(eligible)
    kept_results = [m for m in out if m.get("role") == "tool"]
    assert len(kept_results) == 9 - len(eligible)


def test_goal_and_state_fitting_stages():
    messages = transcript(n_calls=6, result_size=60_000)
    calls = pruner.collect_tool_calls(messages, 6)
    state = pruner.fit_state(messages, calls, "fix tests", 20000, 28000,
                             lambda t: max(1, len(t) // 4))
    assert "ok, 60000 chars (omitted)" in state and "t0 Read" in state
    assert "Goal: fix tests" in state
    with pytest.raises(pruner.PruneError):
        pruner.fit_state(messages, calls, "", 10, 20, lambda t: max(1, len(t) // 4))


# ---------------------------------------------------------------- engine overlay

def _patch_client(monkeypatch, responses):
    """responses: list of dicts consumed per /classify call, or exceptions to raise."""
    from urllib.error import URLError
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
        body = json.loads(request.data.decode())
        requests.append({"body": body, "auth": request.get_header("Authorization")})
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return _Reply(item)

    monkeypatch.setenv("CLASSIFIER_API_KEY", "synthetic-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    if isinstance(responses, list) and responses and isinstance(responses[0], Exception):
        pass
    return requests


def _verdicts(calls, keep=None, keep_result=None):
    """Build the label dict for one batch: keep/keep_result are call_id -> bool."""
    keep = keep or {}
    keep_result = keep_result or {}
    out = {}
    for record in calls:
        cid = record["call_id"]
        out[f"{record['tag']}_call"] = "keep" if keep.get(cid, False) else "drop"
        out[f"{record['tag']}_result"] = "keep" if keep_result.get(cid, False) else "drop"
    return out


def _load_engine(load_plugin, monkeypatch, settings=None):
    h = load_plugin("fast-compaction", settings or {})
    from agent.context_engine import ContextEngine
    from hermes_cli.plugins import get_plugin_context_engine
    engine = get_plugin_context_engine()
    assert isinstance(engine, ContextEngine) and engine.name == "fast-compaction"
    return h, engine


def test_registration_and_status_tool(load_plugin, monkeypatch):
    h, engine = _load_engine(load_plugin, monkeypatch)
    status = json.loads(h.tool("fast_compaction_status", {}))
    assert status["engine"] == "fast-compaction" and status["enabled"] is True
    assert status["attempts"] == 0


def test_prune_success_keeps_text_verbatim(load_plugin, monkeypatch):
    h, engine = _load_engine(load_plugin, monkeypatch)
    messages = transcript(n_calls=6, result_size=40_000)
    calls = pruner.collect_tool_calls(messages, 6)
    eligible = pruner.eligible_calls(calls)
    keep = {eligible[0]["call_id"]: True}
    keep_result = {eligible[0]["call_id"]: True}
    _patch_client(monkeypatch, [_verdicts(eligible, keep, keep_result)])

    out = engine.compress(list(messages), current_tokens=200_000)
    assert out is not messages
    # goal text survives verbatim; dropped results are gone; the kept result object is intact
    assert out[0]["content"] == messages[0]["content"]
    assert any(m.get("tool_call_id") == eligible[0]["call_id"] for m in out)
    assert not any(m.get("tool_call_id") == eligible[-1]["call_id"] for m in out)
    assert engine.compression_count == 1
    status = json.loads(h.tool("fast_compaction_status", {}))
    assert status["pruned"] == 1 and status["fallbacks"] == 0


def test_redaction_runs_before_egress(load_plugin, monkeypatch):
    h, engine = _load_engine(load_plugin, monkeypatch)
    messages = transcript(n_calls=6, result_size=40_000)
    messages[0]["content"] += " token sk-ant-ABCDEF0123456789"
    requests = _patch_client(monkeypatch, [{}])
    engine.compress(list(messages), current_tokens=200_000)
    # malformed empty answer -> fallback, but the state that WAS sent is scrubbed
    if requests:
        assert "sk-ant-ABCDEF0123456789" not in json.dumps(requests[0]["body"])
    status = json.loads(h.tool("fast_compaction_status", {}))
    assert status["fallbacks"] == 1 and status["pruned"] == 0


def test_classifier_down_falls_back_once_then_cooldown(load_plugin, monkeypatch):
    h, engine = _load_engine(load_plugin, monkeypatch)
    from urllib.error import URLError
    _patch_client(monkeypatch, [URLError("connection refused")] * 4)
    messages = transcript(n_calls=6, result_size=40_000)
    out = engine.compress(list(messages), current_tokens=200_000)
    status = json.loads(h.tool("fast_compaction_status", {}))
    assert status["pruned"] == 0 and status["fallbacks"] == 1
    assert "classifier_unavailable" in status["fallback_reasons"]
    # Built-in summary path ran: it either rewrote the middle or refused; never crashed.
    assert isinstance(out, list)


def test_focus_topic_always_uses_summary_path(load_plugin, monkeypatch):
    h, engine = _load_engine(load_plugin, monkeypatch)
    requests = _patch_client(monkeypatch, [])  # zero tolerance: any call fails the test
    messages = transcript(n_calls=6, result_size=40_000)
    engine.compress(list(messages), current_tokens=200_000, focus_topic="release checklist")
    assert requests == []


def test_short_transcript_no_classifier_call(load_plugin, monkeypatch):
    h, engine = _load_engine(load_plugin, monkeypatch)
    requests = _patch_client(monkeypatch, [])
    messages = [msg_user("hi"), msg_assistant("hello!")]
    engine.compress(messages, current_tokens=200_000)
    assert requests == []  # nothing eligible: straight to the built-in path
