"""Behavioural checks for recurring papercut → inert rule drafts (#8).

The claims under test: a draft cannot take effect by itself (verified against the
REAL rules plugin parser, not a restatement of it), only sufficiently recurrent
papercuts become drafts, an existing draft is never overwritten, no temp file is
ever left behind, a hostile fingerprint stays inside the drafts directory, and the
native ``list --json`` payload is parsed exactly once at the boundary — an unknown
row is counted, an unusable payload is reported rather than read as "nothing to do".
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins"
DRAFTS_MODULE = "hermes_plugins_papercuts_drafts"
BINARY = os.environ.get("PAPERCUTS_BINARY", "/home/common/projects/papercuts/papercuts")

FM_RX = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

FAKE_BINARY = """#!{python}
import json, os, sys

args = sys.argv[1:]
log = os.environ.get("FAKE_PAPERCUTS_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\\n")

if "list" in args and "--json" in args:
    print(os.environ.get("FAKE_PAPERCUTS_ROWS", "[]"))
elif "report" in args:
    print("20261106T000000")
else:
    print("usage: papercuts list [--json]")
"""


def _load(filename: str, module_name: str):
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = PLUGIN_ROOT / "papercuts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def drafts():
    return _load("drafts.py", DRAFTS_MODULE)


@pytest.fixture
def rules_plugin():
    """The real rules plugin, for the one claim that matters: a draft is inert."""
    path = PLUGIN_ROOT / "rules" / "__init__.py"
    spec = importlib.util.spec_from_file_location("hermes_plugins_rules_for_drafts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_plugins_rules_for_drafts"] = module
    spec.loader.exec_module(module)
    return module


def _entry(drafts, fingerprint: str = "20261106T120000", count: int = 7,
           summary: str = "terminal: zig build test times out",
           expected: str = "the build finishes", observed: str = "killed after 30s",
           repro: str = "run zig build test twice", category: str = "tooling",
           severity: str = "medium"):
    return drafts.PapercutEntry(fingerprint=drafts.Fingerprint(fingerprint),
                                summary=summary, count=count,
                                category=category, severity=severity, expected=expected,
                                observed=observed, repro=repro)


def _native_row(fingerprint: str, count: int, summary: str = "tool errors") -> dict:
    """A row shaped exactly like the native binary's own record columns."""
    return {
        "id": fingerprint, "schema_version": 1, "status": "open",
        "category": "workflow", "severity": "low", "summary": summary,
        "context": f"expected: it should work\nobserved: {summary}\nrepro: do it again",
        "repo": "", "blame": "sig:tool|x", "snippet": "traceback", "count": count,
        "created": "2026-11-06T00:00:00+00:00", "updated": "2026-11-06T00:00:00+00:00",
    }


def _fake_binary(tmp_path: Path, monkeypatch, rows: str) -> str:
    binary = tmp_path / "fake-papercuts"
    binary.write_text(FAKE_BINARY.format(python=sys.executable), encoding="utf-8")
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setenv("FAKE_PAPERCUTS_ROWS", rows)
    monkeypatch.setenv("FAKE_PAPERCUTS_LOG", str(tmp_path / "calls.jsonl"))
    return str(binary)


def _calls(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# --------------------------------------------------------------------- rendering


def test_draft_markdown_frontmatter_parses_and_is_inert(drafts):
    entry = _entry(drafts)
    text = drafts.draft_rule_markdown(entry)
    match = FM_RX.match(text)
    assert match is not None, "draft must open with a YAML front-matter block"
    meta = yaml.safe_load(match.group(1))
    assert meta["enabled"] is False
    assert meta["id"] == f"papercut-{entry.fingerprint}"
    assert meta["description"].startswith(f"DRAFT from papercut {entry.fingerprint}:")

    body = text[match.end():]
    for fragment in (entry.summary, entry.expected, entry.observed, entry.repro,
                     str(entry.count), "enabled: true", "globs"):
        assert fragment in body


def test_a_written_draft_is_inert_to_the_rules_plugin(drafts, rules_plugin, tmp_path):
    report = drafts.emit_drafts([_entry(drafts)], tmp_path / "drafts", threshold=3)
    assert len(report.written) == 1
    rule = rules_plugin._load_rule_file(str(report.written[0]))
    assert rule is not None, "the draft must be readable as a rule file"
    assert rule.enabled is False, "a draft must never self-activate"
    assert rule.always is False


def test_rule_discovery_does_not_descend_into_the_drafts_directory(drafts, rules_plugin,
                                                                   tmp_path):
    """The default drafts dir lives under ~/.hermes/rules; discovery lists only the
    direct files of a scanned dir, so the subdirectory is never read."""
    scanned = tmp_path / "rules"
    report = drafts.emit_drafts([_entry(drafts)], scanned / "drafts", threshold=3)
    assert len(report.written) == 1
    assert rules_plugin._scan_dir(str(scanned)) == {}


# ------------------------------------------------------------------------- emit


def test_emit_writes_only_at_or_above_the_threshold(drafts, tmp_path):
    target = tmp_path / "drafts"
    report = drafts.emit_drafts(
        [_entry(drafts, fingerprint="hit", count=3),
         _entry(drafts, fingerprint="miss", count=2)],
        target, threshold=3)
    assert [path.name for path in report.written] == ["papercut-hit.md"]
    assert (report.skipped_existing, report.below_threshold) == (0, 1)
    assert sorted(p.name for p in target.iterdir()) == ["papercut-hit.md"]


def test_emit_never_overwrites_and_leaves_no_temp_files(drafts, tmp_path):
    target = tmp_path / "drafts"
    drafts.emit_drafts([_entry(drafts, fingerprint="dup")], target, threshold=1)
    written = target / "papercut-dup.md"
    human = written.read_text(encoding="utf-8").replace("DRAFT", "ADOPTED")
    written.write_text(human, encoding="utf-8")

    second = drafts.emit_drafts([_entry(drafts, fingerprint="dup")], target, threshold=1)
    assert second.written == ()
    assert (second.skipped_existing, second.below_threshold) == (1, 0)
    assert written.read_text(encoding="utf-8") == human  # the human edit survives
    assert [p.name for p in target.iterdir()] == ["papercut-dup.md"]  # no .tmp debris


def test_emit_keeps_a_hostile_fingerprint_inside_the_directory(drafts, tmp_path):
    target = tmp_path / "drafts"
    report = drafts.emit_drafts([_entry(drafts, fingerprint="../../escape")],
                                target, threshold=1)
    assert len(report.written) == 1
    path = report.written[0]
    assert path.parent == target
    assert path.name.startswith("papercut-") and path.name.endswith(".md")
    assert [p.name for p in target.iterdir()] == [path.name]
    assert "escape" in path.name  # readable, not hashed away


def test_a_fingerprint_of_only_punctuation_still_produces_one_draft(drafts, tmp_path):
    target = tmp_path / "drafts"
    first = drafts.emit_drafts([_entry(drafts, fingerprint="///")], target, threshold=1)
    second = drafts.emit_drafts([_entry(drafts, fingerprint="///")], target, threshold=1)
    assert len(first.written) == 1 and second.skipped_existing == 1  # deterministic name
    assert len(list(target.iterdir())) == 1


def test_a_bad_threshold_is_refused_by_type_then_by_value(drafts, tmp_path):
    for bad in (True, "3", 2.5, None):  # wrong TYPE, including the int/bool trap
        with pytest.raises(TypeError):
            drafts.parse_threshold(bad)
        with pytest.raises(TypeError):
            drafts.emit_drafts([_entry(drafts)], tmp_path / "d", threshold=bad)
    for bad in (0, -1):  # right type, unusable value
        with pytest.raises(ValueError):
            drafts.parse_threshold(bad)
    assert not (tmp_path / "d").exists()  # refused before the directory was made
    assert drafts.parse_threshold(1) == 1  # the accepted edge


# ----------------------------------------------------------------------- parsing


def test_parse_maps_native_rows_once(drafts):
    payload = json.dumps([_native_row("20261106T120000", 4, "spine panics")])
    entries, skipped = drafts.parse_list_rows(payload)
    assert skipped == 0 and len(entries) == 1
    entry = entries[0]
    assert entry.fingerprint == "20261106T120000"
    assert entry.count == 4
    assert entry.summary == "spine panics"
    assert entry.category == "workflow" and entry.severity == "low"
    # context is the native column the plugin writes these into
    assert entry.expected == "it should work"
    assert entry.observed == "spine panics"
    assert entry.repro == "do it again"


def test_parse_counts_unknown_rows_instead_of_guessing(drafts):
    payload = json.dumps([
        _native_row("good", 2),
        {"summary": "no identity at all", "count": 5},
        _native_row("bool-count", True),
        "not-a-row",
        _native_row("zero-count", 0),
    ])
    entries, skipped = drafts.parse_list_rows(payload)
    assert [entry.fingerprint for entry in entries] == ["good"]
    assert skipped == 4


def test_parse_rejects_a_payload_that_is_not_native_list_json(drafts):
    with pytest.raises(ValueError):
        drafts.parse_list_rows("traceback: boom")
    with pytest.raises(ValueError):
        drafts.parse_list_rows(json.dumps({"count": 3}))
    assert drafts.parse_list_rows("   ") == ((), 0)  # empty output is nothing to do


# ----------------------------------------------------------------- tool surface


def _papercuts(load_plugin, tmp_path, monkeypatch, rows: str, **settings):
    monkeypatch.setenv("PAPERCUTS_DIR", str(tmp_path / "pc-store"))
    binary = _fake_binary(tmp_path, monkeypatch, rows)
    base = {"binary": binary, "draft_rule_dir": str(tmp_path / "drafts"),
            "draft_rule_after": 3}
    base.update(settings)
    return load_plugin("papercuts", settings=base), binary


def test_drafts_mode_reports_what_it_did(load_plugin, tmp_path, monkeypatch):
    rows = json.dumps([_native_row("recur", 9), _native_row("rare", 1)])
    ctx, _ = _papercuts(load_plugin, tmp_path, monkeypatch, rows)
    payload = json.loads(ctx.tool("papercuts", {"mode": "drafts"}))
    assert payload["success"] is True and payload["mode"] == "drafts"
    assert payload["written"] == [str(tmp_path / "drafts" / "papercut-recur.md")]
    assert payload["skipped_existing"] == 0 and payload["below_threshold"] == 1
    assert payload["skipped_rows"] == 0

    again = json.loads(ctx.tool("papercuts", {"mode": "drafts"}))
    assert again["written"] == [] and again["skipped_existing"] == 1


def test_drafts_mode_reports_a_bad_native_payload_as_failure(load_plugin, tmp_path,
                                                            monkeypatch):
    ctx, _ = _papercuts(load_plugin, tmp_path, monkeypatch, "traceback: boom")
    payload = json.loads(ctx.tool("papercuts", {"mode": "drafts"}))
    assert payload["success"] is False
    assert "drafts" in payload["error"]
    assert not (tmp_path / "drafts").exists() or list((tmp_path / "drafts").iterdir()) == []


def test_drafts_mode_refuses_a_build_without_list_json(load_plugin, tmp_path, monkeypatch):
    """An old native build must not lose the whole feature to an argparse exit 2."""
    monkeypatch.setenv("PAPERCUTS_DIR", str(tmp_path / "pc-store"))
    binary = tmp_path / "old-papercuts"
    binary.write_text(FAKE_BINARY.format(python=sys.executable).replace(
        "usage: papercuts list [--json]", "usage: papercuts list"), encoding="utf-8")
    binary.chmod(binary.stat().st_mode | 0o111)
    ctx = load_plugin("papercuts", settings={"binary": str(binary),
                                             "draft_rule_dir": str(tmp_path / "drafts")})
    payload = json.loads(ctx.tool("papercuts", {"mode": "drafts"}))
    assert payload["success"] is False and "--json" in payload["error"]
    assert not (tmp_path / "drafts").exists()


def test_a_bad_threshold_refuses_to_load_rather_than_guess(load_plugin, tmp_path,
                                                           monkeypatch):
    """A mis-typed setting is a refusal at register — the loader reports why and the
    plugin stays disabled; it never falls back to a default silently."""
    monkeypatch.setenv("PAPERCUTS_DIR", str(tmp_path / "pc-store"))
    binary = _fake_binary(tmp_path, monkeypatch, "[]")
    with pytest.raises(AssertionError,
                       match="refused papercuts: papercuts: draft_rule_after must be an integer"):
        load_plugin("papercuts", settings={"binary": binary, "draft_rule_after": "3"})


def test_session_end_runs_the_step_once_and_only_when_enabled(load_plugin, tmp_path,
                                                              monkeypatch):
    rows = json.dumps([_native_row("recur", 9)])

    off, _ = _papercuts(load_plugin, tmp_path, monkeypatch, rows, draft_rules=False)
    off.hook("on_session_end", session_id="s-1", completed=True)
    assert _calls(tmp_path) == []
    assert (tmp_path / "drafts").exists() is False

    ctx, _ = _papercuts(load_plugin, tmp_path, monkeypatch, rows, draft_rules=True)
    ctx.hook("on_session_end", session_id="s-1", completed=True)
    list_calls = [argv for argv in _calls(tmp_path) if argv == ["list", "--json"]]
    assert len(list_calls) == 1  # bounded: ONE list call per boundary, not a scan
    assert (tmp_path / "drafts" / "papercut-recur.md").is_file()
    recorded = ctx.context.state.get("drafts_last", {})
    assert recorded["success"] is True and recorded["written"]  # visible, not silent

    # A second boundary is idempotent: no second draft, no overwrite.
    ctx.hook("on_session_end", session_id="s-1", completed=True)
    assert [p.name for p in (tmp_path / "drafts").iterdir()] == ["papercut-recur.md"]
    assert ctx.context.state.get("drafts_last", {})["skipped_existing"] == 1
