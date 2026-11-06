"""Behavioural checks for the gate-friction sink (#7a): the seam other plugins
report through, and the papercuts policy that decides what a report becomes.

Every test here can fail: no sink ⇒ counted ``no_sink``; sink ⇒ ``filed``; the same
identity twice ⇒ ``duplicate`` and ONE sink call; a raising sink ⇒ counted ``error``
and still retryable; the sink exists only when ``gate_friction`` is configured on;
and at the daily cap a refusal says ``quota`` without ever touching the binary.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins"
FRICTION_MODULE = "hermes_plugins_papercuts_friction"
BINARY = os.environ.get("PAPERCUTS_BINARY", "/home/common/projects/papercuts/papercuts")

# Records every argv it is called with, answers report/list/--help from env data.
FAKE_BINARY = """#!{python}
import json, os, sys

args = sys.argv[1:]
log = os.environ.get("FAKE_PAPERCUTS_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\\n")

if "report" in args:
    print("20261106T000000")
elif "list" in args and "--json" in args:
    print(os.environ.get("FAKE_PAPERCUTS_ROWS", "[]"))
else:
    print("usage: papercuts list [--json]")
"""


def _load_shared(filename: str, module_name: str):
    """Load a plugin-side shared module the way a path-loader must: sys.modules
    first, so one process never ends up with two independent sink slots."""
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_ROOT / "papercuts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def friction():
    """A clean shared-friction instance: no sink, no counters, no dedupe window."""
    saved = sys.modules.pop(FRICTION_MODULE, None)
    module = _load_shared("friction.py", FRICTION_MODULE)
    module.reset()
    try:
        yield module
    finally:
        sys.modules.pop(FRICTION_MODULE, None)
        if saved is not None:
            sys.modules[FRICTION_MODULE] = saved


def _make_entry(module):
    def build(source: str = "loop-guard", kind: str = "stuck-retry",
              identity: str = "thread-1", summary: str = "terminal blocked by gate",
              detail: str = "same call three times", session_id: str = "s-1"):
        return module.GateFriction(source=source, kind=kind,
                                   identity=module.GateIdentity(identity),
                                   summary=summary, detail=detail,
                                   session_id=module.SessionId(session_id))

    return build


def test_report_without_a_sink_is_counted_and_never_raises(friction):
    build = _make_entry(friction)
    first = friction.report(build())
    second = friction.report(build())  # no sink: nothing is deduped away
    assert (first.filed, first.reason) == (False, "no_sink")
    assert (second.filed, second.reason) == (False, "no_sink")
    stats = friction.stats()
    assert stats["sink_installed"] is False
    assert stats["sources"]["loop-guard"] == 2
    assert stats["reasons"]["no_sink"] == 2


def test_sink_files_once_then_the_same_identity_is_duplicate(friction):
    build = _make_entry(friction)
    calls: list[object] = []

    def sink(entry):
        calls.append(entry)
        return friction.FrictionReceipt(filed=True, reason="filed")

    friction.install_sink(sink)
    assert friction.report(build()).reason == "filed"
    duplicate = friction.report(build())
    assert (duplicate.filed, duplicate.reason) == (False, "duplicate")
    assert len(calls) == 1  # the second report never reached the filing policy
    assert friction.stats()["deduped"] == 1
    # A different identity under the same gate still files.
    assert friction.report(build(identity="thread-2")).reason == "filed"


def test_a_refusing_sink_is_counted_not_raised_and_stays_retryable(friction):
    build = _make_entry(friction)

    def broken_sink(entry):
        raise RuntimeError("binary vanished")

    friction.install_sink(broken_sink)
    receipt = friction.report(build())
    assert (receipt.filed, receipt.reason) == (False, "error")
    assert "RuntimeError" in friction.stats()["last_error"]
    assert friction.stats()["reasons"]["error"] == 1

    # A quota/refusal is retryable: the identity was never marked as filed.
    friction.install_sink(lambda entry: friction.FrictionReceipt(True, "quota"))
    assert friction.report(build()).reason == "quota"
    friction.install_sink(lambda entry: friction.FrictionReceipt(True, "filed"))
    assert friction.report(build()).reason == "filed"


def test_a_foreign_sink_returning_junk_is_an_error_not_a_crash(friction):
    build = _make_entry(friction)
    friction.install_sink(lambda entry: "filed")
    assert friction.report(build()).reason == "error"
    assert "FrictionReceipt" in friction.stats()["last_error"]


def test_an_unknown_source_is_counted_rather_than_dropped(friction):
    build = _make_entry(friction)
    friction.report(build(source="some-new-gate"))
    stats = friction.stats()
    assert stats["sources"]["unknown"] == 1
    assert stats["sources"]["continuation"] == 0


def _fake_binary(tmp_path: Path, monkeypatch, rows: str = "[]") -> str:
    binary = tmp_path / "fake-papercuts"
    binary.write_text(FAKE_BINARY.format(python=sys.executable), encoding="utf-8")
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setenv("FAKE_PAPERCUTS_ROWS", rows)
    monkeypatch.setenv("FAKE_PAPERCUTS_LOG", str(tmp_path / "calls.jsonl"))
    return str(binary)


def test_the_sink_exists_only_when_gate_friction_is_configured(friction, load_plugin,
                                                                tmp_path, monkeypatch):
    store = tmp_path / "pc-store"
    monkeypatch.setenv("PAPERCUTS_DIR", str(store))
    binary = _fake_binary(tmp_path, monkeypatch)

    load_plugin("papercuts", settings={"binary": binary, "gate_friction": False})
    assert sys.modules[FRICTION_MODULE] is friction  # one shared module, not a copy
    build = _make_entry(friction)
    assert friction.stats()["sink_installed"] is False
    assert friction.report(build()).reason == "no_sink"

    load_plugin("papercuts", settings={"binary": binary, "gate_friction": True})
    assert friction.stats()["sink_installed"] is True
    # Filing is auto_capture's policy; with it off the refusal is VISIBLE, not silent.
    receipt = friction.report(build())
    assert (receipt.filed, receipt.reason) == (False, "disabled")
    assert friction.stats()["reasons"]["disabled"] == 1

    # Turning the setting off again removes the sink: config stays authoritative.
    load_plugin("papercuts", settings={"binary": binary, "gate_friction": False})
    assert friction.stats()["sink_installed"] is False


def test_filing_shares_the_auto_capture_quota_transaction(friction, load_plugin,
                                                          tmp_path, monkeypatch):
    store = tmp_path / "pc-store"
    monkeypatch.setenv("PAPERCUTS_DIR", str(store))
    binary = _fake_binary(tmp_path, monkeypatch)
    ctx = load_plugin("papercuts", settings={"binary": binary, "auto_capture": True,
                                             "gate_friction": True})
    build = _make_entry(friction)
    assert friction.report(build()).reason == "filed"

    calls = [json.loads(line) for line in
             Path(os.environ["FAKE_PAPERCUTS_LOG"]).read_text(
                 encoding="utf-8").splitlines() if line.strip()]
    report_calls = [argv for argv in calls if argv and argv[0] == "report"]
    assert len(report_calls) == 1
    argv = report_calls[0]
    assert argv[argv.index("--category") + 1] == "workflow"
    blame = argv[argv.index("--blame") + 1]
    assert blame.startswith("sig:loop-guard|stuck-retry")  # native dedup key
    # The same counter auto-capture spends: one slot consumed, not a private one.
    assert ctx.context.state.get("auto_capture") == {
        "day": time.strftime("%Y-%m-%d"), "n": 1}


def test_quota_exhaustion_reports_quota_without_touching_the_binary(
        friction, load_plugin, tmp_path, monkeypatch):
    store = tmp_path / "pc-store"
    monkeypatch.setenv("PAPERCUTS_DIR", str(store))
    binary = _fake_binary(tmp_path, monkeypatch)
    ctx = load_plugin("papercuts", settings={"binary": binary, "auto_capture": True,
                                             "gate_friction": True})
    ctx.context.state.set("auto_capture",
                          {"day": time.strftime("%Y-%m-%d"), "n": ctx.module.AUTO_DAILY_CAP})
    receipt = friction.report(_make_entry(friction)())
    assert (receipt.filed, receipt.reason) == (False, "quota")
    log = Path(os.environ["FAKE_PAPERCUTS_LOG"])
    assert not log.exists() or "report" not in log.read_text(encoding="utf-8")


def test_papercuts_friction_mode_exposes_the_counters(load_plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERCUTS_DIR", str(tmp_path / "pc-store"))
    binary = _fake_binary(tmp_path, monkeypatch)
    ctx = load_plugin("papercuts", settings={"binary": binary, "auto_capture": True,
                                             "gate_friction": True})
    module = sys.modules[FRICTION_MODULE]
    module.report(module.GateFriction(
        source="continuation", kind="stop-hook",
        identity=module.GateIdentity("t-9"), summary="continuation refused",
        detail="budget", session_id=module.SessionId("s-9")))
    payload = json.loads(ctx.tool("papercuts", {"mode": "friction"}))
    assert payload["success"] is True and payload["mode"] == "friction"
    assert payload["sink_installed"] is True
    assert payload["sources"]["continuation"] == 1
    assert payload["reasons"]["filed"] == 1
    assert set(payload["reasons"]) == {"filed", "no_sink", "quota", "duplicate",
                                       "disabled", "error"}
    assert set(payload["sources"]) == {"continuation", "loop-guard", "chain-compaction",
                                       "unknown"}


def test_unusable_sibling_degrades_without_breaking_the_bridge(load_plugin, tmp_path,
                                                               monkeypatch):
    """A missing shared module must not break the existing tool contract."""
    monkeypatch.setenv("PAPERCUTS_DIR", str(tmp_path / "pc-store"))
    binary = _fake_binary(tmp_path, monkeypatch)
    ctx = load_plugin("papercuts", settings={"binary": binary})
    payload = json.loads(ctx.tool("papercuts", {"mode": "list"}))
    assert payload["success"] is True
    assert json.loads(ctx.tool("papercuts", {"mode": "nope"}))["success"] is False
