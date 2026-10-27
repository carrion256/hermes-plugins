"""chain-compaction: ordered cascade with per-stage miss handling and summary backstop."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "chain-compaction"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"cc_{name}", PLUGIN / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load chain-compaction/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stages = _load("stages")


def asst(cid, name="terminal", args=None, text=None):
    return {"role": "assistant", "content": text,
            "tool_calls": [{"id": cid, "type": "function",
                           "function": {"name": name, "arguments": json.dumps(args or {"c": 1})}}]}


def tool(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def transcript(old=6, big=False):
    """old stale tool pairs + recent activity; big makes old results exceed the clip trigger."""
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "fix the failing loader test"}]
    for i in range(old):
        msgs.append(asst(f"c{i}"))
        msgs.append(tool(f"c{i}", f"output {i} " * (1000 if big else 12)))
    msgs.append(asst("cr", text="working on it"))
    msgs.append(tool("cr", "recent relevant result"))
    msgs.append({"role": "assistant", "content": "done, tests pass"})
    return msgs


CFG = dict(stages.DEFAULTS)


def tokens(text):
    return max(1, len(text) // 4)


# ------------------------------------------------------------------ pure stages

def test_parse_pipeline_validates_against_known_stages():
    assert stages.parse_pipeline("prune,clip , Drop_Stale", extra=("prune",)) == ["prune", "clip", "drop_stale"]
    with pytest.raises(ValueError, match="unknown stage"):
        stages.parse_pipeline("prune,magic", extra=("prune",))
    with pytest.raises(ValueError, match="at least one"):
        stages.parse_pipeline("")
    # prune is engine-owned: only valid when the caller declares it
    with pytest.raises(ValueError, match="unknown stage"):
        stages.parse_pipeline("prune")


def test_clip_only_touches_over_trigger_and_never_rewrites_small():
    msgs = transcript(old=0)
    msgs.insert(3, tool("big", "X" * 30000))
    cfg = dict(CFG, clip_trigger_chars=8000, clip_head_chars=1200)
    out, stats = stages.stage_clip(msgs, cfg, tokens)
    assert stats["stage"] == "clip" and stats["touched"] == 1
    clipped = [m for m in out if m.get("role") == "tool" and m["tool_call_id"] == "big"][0]
    assert clipped["content"].startswith("X" * 1200) and clipped["content"].endswith("X" * 1200)
    assert "omitted" in clipped["content"] and "api_content" not in clipped
    kept_small = [m for m in out if m.get("role") == "tool" and m["tool_call_id"] == "cr"]
    assert kept_small[0] is msgs[-2]  # untouched result returned as the SAME object


def test_clip_raises_when_nothing_over_trigger():
    with pytest.raises(stages.StageError, match="nothing_over_trigger"):
        stages.stage_clip(transcript(old=0), dict(CFG, clip_trigger_chars=100000), tokens)


def test_drop_stale_keeps_recent_window_and_pairs():
    msgs = transcript(old=6)
    cfg = dict(CFG, drop_stale_keep_pairs=2, preserve_recent_messages=2)
    out, stats = stages.stage_drop_stale(msgs, cfg, tokens)
    ids = [tc["id"] for m in out if m.get("role") == "assistant" for tc in (m.get("tool_calls") or [])]
    # two guards: the keep-window keeps the 2 newest pairs (c5, cr), and a pair
    # whose RESULT sits in the protected window is pinned outright (cr's result
    # is the newest message besides the final reply) — c0..c3 go, c4..cr stay;
    # no orphans
    assert set(ids) & {"c0", "c1", "c2", "c3"} == set() and {"c4", "c5", "cr"} <= set(ids)
    orphaned = [m["tool_call_id"] for m in out if m.get("role") == "tool" and m["tool_call_id"] not in ids]
    assert orphaned == []
    assert stats["dropped_pairs"] == 4
    # user/assistant text never touched
    assert out[0] is msgs[0] and out[1] is msgs[1]


def test_drop_stale_raises_beyond_nothing():
    with pytest.raises(stages.StageError, match="nothing_beyond_window"):
        stages.stage_drop_stale(transcript(old=0), CFG, tokens)


# ------------------------------------------------------------------ cascade orchestration

def _load_stub(monkeypatch):
    import sys
    spec = importlib.util.spec_from_file_location("cc_engine_test", PLUGIN / "__init__.py",
                                                 submodule_search_locations=[str(PLUGIN)])
    if spec is None or spec.loader is None:
        raise ImportError("cannot load chain-compaction engine")
    module = importlib.util.module_from_spec(spec)
    sys.modules["cc_engine_test"] = module  # required for the relative `from . import stages`
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine_factory(monkeypatch):
    from agent.context_compressor import ContextCompressor

    calls = {"summary": 0}

    def fake_compress(self, messages, current_tokens=None, focus_topic=None,
                      force=False, memory_context="", bypass_cooldown=False):
        calls["summary"] += 1
        return list(messages)

    monkeypatch.setattr(ContextCompressor, "compress", fake_compress)

    class FakeClient:  # the fake pruner ignores the response body entirely
        def post_questions(self, endpoint, api_key_env, text, questions, timeout=None):
            return {}

    def make(pipeline, pruner=None, client=None, **over):
        mod = _load_stub(monkeypatch)
        settings = dict(CFG)
        settings["pipeline"] = pipeline
        settings.update(over)
        if pruner is not None:  # a usable pruner implies a usable transport+key
            monkeypatch.setenv("CLASSIFIER_API_KEY", "synthetic")
            client = FakeClient() if client is None else client
        engine = mod._engine_class()(settings, pruner, client)
        return mod, engine, calls
    return make


def fake_pruner(drop_every=True, boom=None):
    """A pruner stand-in with fast-compaction's exact surface."""
    class P:
        MAX_TEXT = 100_000
        DEFAULTS = {"max_state_tokens": 20000, "max_request_tokens": 28000,
                    "truncate_head_chars": 300}

        def collect_tool_calls(self, messages, preserve_recent):
            if boom == "collect":
                raise boom and RuntimeError("collect boom")
            calls = {}
            results = {}
            for i, m in enumerate(messages):
                if isinstance(m, dict) and m.get("role") == "tool":
                    results.setdefault(str(m.get("tool_call_id") or ""), i)
            tag = 0
            for i, m in enumerate(messages):
                if isinstance(m, dict) and m.get("role") == "assistant":
                    for tc in m.get("tool_calls") or []:
                        cid = str(tc.get("id") or "")
                        if not cid:
                            continue
                        calls[cid] = {"call_id": cid, "tool": "terminal", "arguments": "{}",
                                      "assistant_index": i, "result_index": results.get(cid),
                                      "result_text": "x" * 50 if results.get(cid) else "",
                                      "pinned": i == 0 or i >= len(messages) - preserve_recent
                                                  or results.get(cid) is None,
                                      "tag": f"t{tag}"}
                        tag += 1
            return calls

        def eligible_calls(self, calls):
            return [c for c in calls.values() if not c["pinned"]]

        def extract_goal(self, messages):
            return "goal"

        def fit_state(self, messages, calls, goal, a, b, tokens):
            return "STATE"

        def build_batches(self, eligible):
            return [eligible]

        def build_request(self, batch, state):
            return state, {}

        def parse_response(self, data, batch, drop_confidence=0.5):
            return {r["call_id"]: {"call": False, "result": False} for r in batch}

        def apply_decisions(self, messages, calls, decisions, trunc):
            drop = {cid for cid, got in decisions.items() if not got["call"]}
            out = [m for m in messages
                   if not (isinstance(m, dict) and m.get("role") == "tool"
                           and str(m.get("tool_call_id") or "") in drop)]
            out = [m for m in out
                   if not (isinstance(m, dict) and m.get("role") == "assistant"
                           and m.get("tool_calls")
                           and all(str(tc.get("id")) in drop for tc in m["tool_calls"] if isinstance(tc, dict)))]
            return out, {"candidates": len(decisions), "dropped": len(drop), "truncated": 0,
                         "kept": 0, "chars_saved": 0}
    return P()


def test_prune_reaches_target_and_stops_before_later_stages(engine_factory):
    mod, engine, calls = engine_factory("prune,clip,drop_stale", pruner=fake_pruner())
    msgs = transcript(old=6, big=True)
    out = engine.compress(msgs)
    assert calls["summary"] == 0                      # never reached the built-in
    trace = mod.STATUS["last"]["trace"]
    done = [t for t in trace if "reduction" in t]
    assert [t["stage"] for t in done] == ["prune"]    # target hit -> later stages skipped
    assert mod.STATUS["committed"] == 1 and mod.STATUS["fallbacks"] == 0
    # user/assistant text and the recent result stay verbatim; later stages never ran
    assert all(any(m is o for m in out) for o in msgs if o.get("role") in ("system", "user"))
    assert any(m is msgs[-2] for m in out)            # recent tool result untouched object
    assert "clip" not in [t["stage"] for t in done] and "drop_stale" not in [t["stage"] for t in done]



def test_engine_survives_the_host_per_agent_copy(engine_factory, monkeypatch):
    """agent_init._select_context_engine() gives every agent its own engine via
    ContextEngine.clone_for_agent() (a copy.deepcopy) and, when that raises, logs a
    warning and silently installs the built-in ContextCompressor — the cascade would
    never run and nothing else would report it. The prune stage holds the
    fast-compaction *module*, which copy cannot clone."""
    probe = _load_stub(monkeypatch)
    pruner_mod, _ = probe._load_fast_compaction()
    assert pruner_mod is not None, "fast-compaction absent: the shared pruner is the point"
    mod, engine, _ = engine_factory("prune", pruner=pruner_mod)
    clone = engine.clone_for_agent()                        # the host's own entry point
    assert type(clone) is type(engine)
    assert clone._pruner is engine._pruner                  # a module: shared, never copied
    assert clone._cc == engine._cc and clone._cc is not engine._cc
    clone.compression_count += 1
    assert engine.compression_count == 0                    # per-agent budget state is not shared


def test_chain_advances_when_prune_fails_and_clips_to_target(engine_factory):
    mod, engine, calls = engine_factory("prune,clip,drop_stale", pruner=None)
    msgs = transcript(old=3, big=True)
    out = engine.compress(msgs)
    assert calls["summary"] == 0
    trace = mod.STATUS["last"]["trace"]
    assert trace[0]["stage"] == "prune" and "miss" in trace[0]      # unavailable -> miss
    assert any(t["stage"] == "clip" and "reduction" in t for t in trace)
    big = [m for m in out if m.get("role") == "tool" and len(str(m.get("content", ""))) > 3000]
    assert big == []                                                # clip actually ran
    assert mod.STATUS["committed"] == 1
    assert "prune:prune_unavailable:not_installed" in mod.STATUS["stage_misses"]


def test_whole_cascade_insufficient_falls_back_to_builtin_summary(engine_factory):
    # tiny transcript: prune has nothing eligible, clip nothing over trigger,
    # drop_stale nothing beyond the window -> summary path, untouched input
    mod, engine, calls = engine_factory("prune,clip,drop_stale", pruner=None)
    msgs = transcript(old=0)
    out = engine.compress(msgs)
    assert calls["summary"] == 1
    assert mod.STATUS["fallbacks"] == 1
    assert all(any(m is o for m in out) for o in msgs)   # built-in received the originals
    misses = mod.STATUS["stage_misses"]
    assert any(k.startswith("fallback:cascade_insufficient") for k in misses)


def test_stage_crash_is_a_miss_not_a_failure(engine_factory):
    mod, engine, calls = engine_factory("prune,clip", pruner=fake_pruner(boom="collect"))
    msgs = transcript(old=2, big=True)
    out = engine.compress(msgs)
    assert calls["summary"] == 0
    assert mod.STATUS["stage_misses"].get("prune:crash") == 1
    assert any(t["stage"] == "clip" and "reduction" in t for t in mod.STATUS["last"]["trace"])


def test_below_stage_min_counts_as_miss_and_next_stage_finishes_the_job(engine_factory):
    # prune runs cleanly but decides nothing worth keeping (zero contribution);
    # clip must still get its turn and do the real work
    p = fake_pruner()
    def no_op_apply(messages, calls, decisions, trunc):
        return messages, {"candidates": len(decisions), "dropped": 0, "truncated": 0,
                          "kept": len(decisions), "chars_saved": 0}
    p.apply_decisions = no_op_apply
    mod, engine, calls = engine_factory("prune,clip", pruner=p, stage_min_reduction=0.05)
    msgs = transcript(old=6, big=True)
    out = engine.compress(msgs)
    assert calls["summary"] == 0
    trace = mod.STATUS["last"]["trace"]
    assert "prune:below_stage_min" in mod.STATUS["stage_misses"]
    assert [t for t in trace if t["stage"] == "clip" and "reduction" in t]  # clip still committed


def test_disabled_or_focus_topic_goes_straight_to_builtin(engine_factory):
    mod, engine, calls = engine_factory("clip", enabled=False)
    engine.compress(transcript(old=1))
    assert calls["summary"] == 1 and mod.STATUS["attempts"] == 0
    before = calls["summary"]
    engine.compress(transcript(old=1), focus_topic="anything")
    assert calls["summary"] == before + 1 and mod.STATUS["attempts"] == 0


def test_register_rejects_bad_pipeline_and_status_tool_reports():
    mod = _load_stub(pytest)  # module import itself must work
    import yaml
    manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
    assert manifest["name"] == "chain-compaction"
    assert "chain_compaction_status" in manifest["provides_tools"]
    assert manifest["config_schema"]["pipeline"]["default"] == "prune,clip,drop_stale"
    with pytest.raises(ValueError):
        stages.parse_pipeline("nonsense", extra=mod.ENGINE_STAGES)
