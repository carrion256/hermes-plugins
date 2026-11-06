"""Rule-declared acceptance obligations (plugins/rules + obligations.py).

Contract under test: a rule that names ``check_refs``/``verification_refs`` and
matches a REAL file mutation records one opaque, session-scoped obligation row
(never executed here), deduplicated per rule with a path union and a hard
per-session bound; a read, a non-matching path, or a disabled setting record
nothing; ``rules_explain`` reports what is owed and the store's degradation
counters; session reset drops the rows; an unreadable store degrades to
"nothing owed" visibly through counters instead of silently.

The store is reached exactly the way a sibling plugin reaches it — by
path-loading ``plugins/rules/obligations.py`` with a ``sys.modules`` check
first — so these tests also pin the shared-module contract.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import ModuleType

from hermes_cli.plugins_state import PluginState

PLUGIN_FILE = (Path(__file__).resolve().parents[1] / "plugins" / "rules"
               / "obligations.py")
MODULE_NAME = "hermes_plugins_rules_obligations"
REF = "pytest tests/test_x.py"
SESSION = "sess-1"


def _store() -> ModuleType:
    """The shared store module, loaded the sibling-plugin way."""
    shared = sys.modules.get(MODULE_NAME)
    if isinstance(shared, ModuleType):
        return shared
    spec = importlib.util.spec_from_file_location(MODULE_NAME, PLUGIN_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _counters(store: ModuleType) -> tuple[int, int, int, int]:
    stats = store.stats()
    return (stats.read_failures, stats.write_failures,
            stats.skipped_rows, stats.dropped_over_cap)


def _meta(cwd, session_id=SESSION, profile="main"):
    return {"session_id": session_id, "model": "m", "provider": "p",
            "platform": "cli", "profile_name": profile, "cwd": str(cwd)}


def _write_rule(path, front_matter, body="GUARD"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front_matter}\n---\n{body}\n", encoding="utf-8")


def _harness(load_plugin, tmp_path, monkeypatch, settings=None):
    monkeypatch.chdir(tmp_path)
    merged = {"rule_dirs": [str(tmp_path / "global")], "ttsr_enabled": True,
              "disabled_rules": [], "rules_explain_enabled": True}
    merged.update(settings or {})
    return load_plugin("rules", settings=merged)


def _seed(harness, cwd, session_id=SESSION):
    harness.manager.render_system_prompt_sections(_meta(cwd, session_id=session_id))


def _edit(harness, path, tool_name="patch"):
    return harness.middleware(
        "tool_execution", tool_name=tool_name,
        args={"path": path, "mode": "replace", "content": "x"},
        next_call=lambda a=None: "EXECUTED", session_id=SESSION, task_id="t1",
        tool_call_id="c1", turn_id="turn-1", api_request_id="api-1")


def _explain(harness, paths, tool_name="patch", session_id=SESSION):
    return json.loads(harness.tool("rules_explain",
                                   {"paths": paths, "tool_name": tool_name},
                                   session_id=session_id, task_id="t1"))


REFS_FM = f'globs: ["*.py"]\ntools: [patch]\ncheck_refs: ["{REF}"]'


def test_matching_rule_records_one_obligation(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", REFS_FM)
    harness = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(harness, tmp_path)

    assert "GUARD" in _edit(harness, "src/a.py")  # guidance lane unaffected
    rows = _store().for_session(SESSION)
    assert len(rows) == 1
    row = rows[0]
    assert row.rule_name == "guard" and row.rule_id.startswith("rule:")
    assert row.source == str(tmp_path / "global" / "guard.md")
    assert row.refs == (REF,)  # opaque: recorded, never resolved
    assert len(row.paths) == 1 and row.paths[0].endswith("src/a.py")
    # An ISO aware-UTC instant, comparable with the native ledgers.
    assert row.recorded_at.endswith("+00:00")


def test_non_matching_path_records_nothing(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", REFS_FM)
    harness = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(harness, tmp_path)

    _edit(harness, "docs/notes.md")
    assert _store().for_session(SESSION) == ()


def test_read_is_not_a_mutation(load_plugin, tmp_path, monkeypatch):
    """Only the host's file-mutating tools can create an obligation. The rule is
    glob-only (no ``tools:`` scope), so ``_matches`` accepts ANY tool here: the
    read records nothing while the same path through a real edit does."""
    _write_rule(tmp_path / "global" / "guard.md",
                f'globs: ["*.py"]\ncheck_refs: ["{REF}"]')
    harness = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(harness, tmp_path)

    _edit(harness, "src/a.py", tool_name="read_file")
    assert _store().for_session(SESSION) == ()

    _edit(harness, "src/a.py")
    assert len(_store().for_session(SESSION)) == 1


def test_repeat_edits_dedupe_and_union_paths(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", REFS_FM)
    harness = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(harness, tmp_path)

    _edit(harness, "src/a.py")
    _edit(harness, "src/b.py")
    rows = _store().for_session(SESSION)
    assert len(rows) == 1, "repeated matches must not grow the ledger"
    assert [p.rsplit("/", 1)[-1] for p in rows[0].paths] == ["a.py", "b.py"]
    assert rows[0].refs == (REF,)


def test_explain_lists_owed_obligations(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", REFS_FM)
    harness = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(harness, tmp_path)
    store = _store()
    before = asdict(store.stats())
    _edit(harness, "src/a.py")

    report = _explain(harness, ["src/a.py"])
    assert [o["rule_name"] for o in report["obligations"]] == ["guard"]
    assert report["obligations"][0]["refs"] == [REF]
    assert report["obligations"][0]["paths"][0].endswith("src/a.py")
    assert report["obligation_store"] == before, "a healthy record path degrades nothing"
    # A session that never matched reports nothing owed.
    assert _explain(harness, ["src/a.py"], session_id="other")["obligations"] == []


def test_session_reset_drops_obligations(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", REFS_FM)
    harness = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(harness, tmp_path)
    _edit(harness, "src/a.py")
    assert _store().for_session(SESSION)

    harness.hook("on_session_reset", session_id=SESSION)
    assert _store().for_session(SESSION) == ()
    assert _explain(harness, ["src/a.py"])["obligations"] == []


def test_setting_off_records_nothing_but_keeps_guidance(load_plugin, tmp_path,
                                                        monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", REFS_FM)
    harness = _harness(load_plugin, tmp_path, monkeypatch,
                      settings={"acceptance_obligations": False})
    _seed(harness, tmp_path)

    assert "GUARD" in _edit(harness, "src/a.py")
    assert _store().for_session(SESSION) == ()
    assert _explain(harness, ["src/a.py"])["obligation_store"] == {"disabled": True}


def test_malformed_setting_keeps_the_safe_default(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", REFS_FM)
    harness = _harness(load_plugin, tmp_path, monkeypatch,
                      settings={"acceptance_obligations": "sure"})
    _seed(harness, tmp_path)

    _edit(harness, "src/a.py")
    assert len(_store().for_session(SESSION)) == 1, "garbage config never disables a gate"


def test_per_session_bound_and_visible_drop(load_plugin, tmp_path, monkeypatch):
    for index in range(65):
        _write_rule(tmp_path / "global" / f"guard{index}.md",
                    f'globs: ["*.py"]\ntools: [patch]\ncheck_refs: ["check {index}"]')
    harness = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(harness, tmp_path)
    store = _store()
    before = _counters(store)

    _edit(harness, "src/a.py")

    rows = store.for_session(SESSION)
    assert len(rows) == store.MAX_PER_SESSION
    assert _counters(store)[3] == before[3] + 1, "over-cap drops are counted"


def test_unreadable_store_degrades_visibly_to_nothing_owed(load_plugin, tmp_path,
                                                           monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", REFS_FM)
    harness = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(harness, tmp_path)
    _edit(harness, "src/a.py")
    store = _store()
    assert store.for_session(SESSION)
    PluginState("rules").path.write_text("{ not json", encoding="utf-8")
    before = _counters(store)

    assert store.for_session(SESSION) == ()
    assert _counters(store)[0] == before[0] + 1, "a failed read is counted, not silent"
    assert _explain(harness, ["src/a.py"])["obligations"] == []


def test_malformed_rows_are_skipped_and_counted():
    store = _store()
    store.record(SESSION, store.Obligation(
        rule_id=store.RuleId("rule:good"), rule_name="good", source="/g.md",
        refs=(REF,),
        paths=("src/a.py",), recorded_at=store.now_iso()))
    state = PluginState("rules")
    stored = state.get(store.state_key(SESSION))
    stored.append({"v": 99, "rule_id": "rule:future"})
    stored.append({"v": store.STATE_VERSION, "rule_id": ""})
    state.set(store.state_key(SESSION), stored)
    before = _counters(store)

    rows = store.for_session(SESSION)
    assert [r.rule_id for r in rows] == ["rule:good"]
    assert _counters(store)[2] == before[2] + 2


def test_empty_input_never_reaches_the_store(load_plugin, tmp_path, monkeypatch):
    store = _store()
    assert store.record("", store.Obligation(
        rule_id=store.RuleId("rule:x"), rule_name="x", source="/x.md",
        refs=(REF,),
        paths=("a.py",), recorded_at=store.now_iso())) == ()
    assert store.record(SESSION, store.Obligation(
        rule_id=store.RuleId("rule:x"), rule_name="x", source="/x.md", refs=(),
        paths=("a.py",), recorded_at=store.now_iso())) == ()
    assert store.for_session("") == ()
    store.clear("")
    assert store.for_session(SESSION) == ()
