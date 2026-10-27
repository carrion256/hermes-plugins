"""Behavioral checks for papercuts typed/redacted reporting against the REAL
native binary in an isolated PAPERCUTS_DIR. The binary owns dedup/recurrence;
these tests assert the plugin feeds it typed argv and redacted text only.
"""
import json
import os
import subprocess
import sys

import pytest

BINARY = os.environ.get("PAPERCUTS_BINARY", "/home/common/projects/papercuts/papercuts")

pytestmark = pytest.mark.skipif(
    not os.path.exists(BINARY), reason="native papercuts binary not present")


@pytest.fixture
def papercuts(load_plugin, tmp_path, monkeypatch):
    store = tmp_path / "pc-store"
    monkeypatch.setenv("PAPERCUTS_DIR", str(store))
    monkeypatch.setenv("PAPERCUTS_BINARY", BINARY)
    ctx = load_plugin("papercuts", settings={"binary": BINARY})
    return ctx, store


def _records(store):
    path = store / "records.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_quoted_fields_survive_argv(papercuts):
    ctx, store = papercuts
    tricky = 'tool said "it\'s fine" -- badly -- category=all'
    out = json.loads(ctx.tool("papercuts", {
        "mode": "report", "category": "tooling", "severity": "medium",
        "summary": tricky,
    }))
    assert out["success"], out
    records = _records(store)
    assert len(records) == 1 and records[0]["summary"] == tricky


def test_typed_context_and_snippet_mapping(papercuts):
    ctx, store = papercuts
    out = json.loads(ctx.tool("papercuts", {
        "mode": "report", "category": "tooling", "severity": "medium",
        "summary": "terminal cwd drift",
        "expected": "cwd stays", "observed": "cwd changed",
        "tool_versions": "hermes b141dd18", "repro": "run twice",
        "reference": "session-42",
        "invocation_result": "stderr said no",
    }))
    assert out["success"]
    rec = _records(store)[0]
    for needle in ("expected: cwd stays", "observed: cwd changed",
                   "tool_versions: hermes b141dd18", "repro: run twice",
                   "reference: session-42"):
        assert needle in rec["context"]
    assert rec["snippet"] == "stderr said no"


def test_duplicate_report_increments_recurrence(papercuts):
    ctx, store = papercuts
    args = {"mode": "report", "category": "docs", "severity": "low",
            "summary": "link 404", "blame": "sig:docs|404"}
    first = json.loads(ctx.tool("papercuts", args))
    second = json.loads(ctx.tool("papercuts", args))
    assert first["success"] and second["success"]
    records = _records(store)
    assert len(records) == 1
    assert int(records[0]["count"]) == 2


def test_redaction_blocks_secret_from_storage(papercuts):
    ctx, store = papercuts
    secret = "sk-" + "A" * 40
    out = json.loads(ctx.tool("papercuts", {
        "mode": "report", "category": "tooling", "severity": "medium",
        "summary": "tool leaked env",
        "invocation_result": f"token={secret}",
    }))
    assert out["success"]
    rec = _records(store)[0]
    assert secret not in rec["snippet"]
    assert "token=" in rec["snippet"]  # key kept, value scrubbed


def test_ack_resolve_list_forwarding(papercuts):
    ctx, store = papercuts
    json.loads(ctx.tool("papercuts", {
        "mode": "report", "category": "tooling", "severity": "low",
        "summary": "need ack",
    }))
    rec = _records(store)[0]
    assert json.loads(ctx.tool("papercuts", {"mode": "ack", "id": rec["id"]}))["success"]
    listed = json.loads(ctx.tool("papercuts", {"mode": "list", "status": "ack"}))
    assert rec["id"] in listed["output"]
    assert json.loads(ctx.tool("papercuts", {"mode": "resolve", "id": rec["id"]}))["success"]


def _directive():
    return {"action": "block", "delivered": True,
            "directives": [{
                "directive_id": "lg-1", "action": "block", "outcome": "delivered",
                "origin": "plugin_hook", "rule_key": "stuck-retry",
                "message": "LOOP GUARD: stop retrying this failing tool call",
            }]}


def test_loop_guard_block_files_deduped_recurrence(load_plugin, tmp_path, monkeypatch):
    store = tmp_path / "pc-store"
    monkeypatch.setenv("PAPERCUTS_DIR", str(store))
    ctx = load_plugin("papercuts", settings={"binary": BINARY, "auto_capture": True})
    ctx.hook("post_tool_directive", tool_name="terminal", tool_input={"command": "x"},
             **_directive())
    ctx.hook("post_tool_directive", tool_name="terminal", tool_input={"command": "x"},
             **_directive())
    records = _records(store)
    assert len(records) == 1
    assert int(records[0]["count"]) == 2
    assert records[0]["blame"].startswith("sig:terminal|stuck-retry")
    assert records[0]["summary"].startswith("tool terminal blocked by loop guard")


def test_non_loop_guard_directive_not_filed(load_plugin, tmp_path, monkeypatch):
    store = tmp_path / "pc-store"
    monkeypatch.setenv("PAPERCUTS_DIR", str(store))
    ctx = load_plugin("papercuts", settings={"binary": BINARY, "auto_capture": True})
    ctx.hook("post_tool_directive", tool_name="terminal", tool_input={},
             action="block", delivered=True,
             directives=[{"directive_id": "d", "action": "block", "outcome": "delivered",
                          "origin": "thread_whitelist", "message": "policy says no"}])
    assert _records(store) == []


def test_auto_capture_off_by_default(load_plugin, tmp_path, monkeypatch):
    store = tmp_path / "pc-store"
    monkeypatch.setenv("PAPERCUTS_DIR", str(store))
    ctx = load_plugin("papercuts", settings={"binary": BINARY})
    ctx.hook("post_tool_call", tool_name="terminal", args={},
             result='{"error": "exit 1"}')
    ctx.hook("post_tool_directive", tool_name="terminal", tool_input={},
             **_directive())
    assert _records(store) == []


def test_tool_error_capture_when_enabled(load_plugin, tmp_path, monkeypatch):
    store = tmp_path / "pc-store"
    monkeypatch.setenv("PAPERCUTS_DIR", str(store))
    ctx = load_plugin("papercuts", settings={"binary": BINARY, "auto_capture": True})
    ctx.hook("post_tool_call", tool_name="read_file", args={},
             result='{"error": "file not found"}', status="error",
             error_type="FileNotFound", error_message="file not found")
    records = _records(store)
    assert len(records) == 1
    # Repeat: dedup increments the native record instead of filing a duplicate.
    ctx.hook("post_tool_call", tool_name="read_file", args={},
             result='{"error": "file not found"}', status="error",
             error_type="FileNotFound", error_message="file not found")
    records = _records(store)
    assert len(records) == 1 and int(records[0]["count"]) == 2


def test_benign_result_text_never_files(load_plugin, tmp_path, monkeypatch):
    """Authoritative host status decides; result text is never parsed. A
    successful result whose text contains "error" must not file."""
    store = tmp_path / "pc-store"
    monkeypatch.setenv("PAPERCUTS_DIR", str(store))
    ctx = load_plugin("papercuts", settings={"binary": BINARY, "auto_capture": True})
    ctx.hook("post_tool_call", tool_name="read_file", args={},
             result='{"text": "0 errors found, all good"}', status="ok")
    assert _records(store) == []


def test_quota_counts_only_filed_failures(load_plugin, tmp_path, monkeypatch):
    """Benign events never consume auto-capture quota."""
    store = tmp_path / "pc-store"
    monkeypatch.setenv("PAPERCUTS_DIR", str(store))
    ctx = load_plugin("papercuts", settings={"binary": BINARY, "auto_capture": True})
    for i in range(30):  # more than AUTO_DAILY_CAP of benign events
        ctx.hook("post_tool_call", tool_name="read_file", args={},
                 result=f"ok {i}", status="ok")
    assert _records(store) == []
    ctx.hook("post_tool_call", tool_name="read_file", args={},
             result="boom", status="error", error_type="E", error_message="boom")
    records = _records(store)
    assert len(records) == 1


def test_report_redacts_summary_repo_blame(load_plugin, tmp_path, monkeypatch):
    """All free-text columns are redacted fail-closed before forwarding."""
    store = tmp_path / "pc-store"
    monkeypatch.setenv("PAPERCUTS_DIR", str(store))
    ctx = load_plugin("papercuts", settings={"binary": BINARY})
    out = json.loads(ctx.tool("papercuts", {
        "mode": "report", "category": "tooling", "severity": "low",
        "summary": "fix sk-ABCDEFGHIJKLMNOPQRSTUVW login flow",
        "repo": "https://[email protected]/x.git",
        "blame": "token abcdef0123456789",
    }))
    assert out["success"], out
    records = _records(store)
    assert records
    joined = json.dumps(records)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVW" not in joined
    assert "abcdef0123456789" not in joined


# Cross-process quota race: two REAL processes (pytest process + subprocess)
# each fire one qualifying failure at the cap boundary, synchronizing inside
# the native CLI wrapper so both reach the read-modify-write while the quota
# counter still reads cap-1. Real binary, real store; only scheduling is
# hostile. Without the host transaction lock both file (records == 2).
PLUGIN_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins")

WRAPPER = """#!/bin/sh
touch "%s"
i=0
while [ ! -f "%s" ] && [ $i -lt 60 ]; do sleep 0.05; i=$((i+1)); done
exec "%s" "$@"
"""

RUNNER = """
import os, time, yaml
from pathlib import Path
home = Path(os.environ["HERMES_HOME"])
cfg = yaml.safe_load((home / "config.yaml").read_text())
entry = cfg["plugins"]["entries"]["papercuts"]
entry.setdefault("settings", {})["binary"] = os.environ["PAPERCUTS_WRAPPER2"]
(home / "config.yaml").write_text(yaml.safe_dump(cfg))
from hermes_cli.plugins import PluginManager
from hermes_cli.plugins_discovery import scan_directory
manager = PluginManager()
manager._discovered = True
manifests = {m.name: m for m in scan_directory(os.environ["PLUGIN_ROOT"], "user")}
manager._load_plugin(manifests["papercuts"])
manager.invoke_hook("post_tool_call", tool_name="terminal", args={},
                    result="boom race", status="error", error_type="E",
                    error_message="boom race")
time.sleep(8)  # let any async hook worker finish its filing
"""


def test_quota_race_across_processes(load_plugin, tmp_path, monkeypatch):
    import subprocess
    import sys
    import time as clock
    store = tmp_path / "pc-store"
    monkeypatch.setenv("PAPERCUTS_DIR", str(store))
    sync = tmp_path / "sync"
    sync.mkdir()

    def wrapper(name, own, peer):
        w = tmp_path / name
        w.write_text(WRAPPER % (str(sync / own), str(sync / peer), BINARY))
        w.chmod(w.stat().st_mode | 0o111)
        return w

    w1 = wrapper("pc-w1", "p1", "p2")
    w2 = wrapper("pc-w2", "p2", "p1")
    ctx = load_plugin("papercuts", settings={"binary": str(w1), "auto_capture": True})
    day = clock.strftime("%Y-%m-%d")
    cap = ctx.module.AUTO_DAILY_CAP
    # Both processes enter at the final quota slot.
    ctx.context.state.set("auto_capture", {"day": day, "n": cap - 1})
    runner = tmp_path / "second_process.py"
    runner.write_text(RUNNER)
    monkeypatch.setenv("PAPERCUTS_WRAPPER2", str(w2))
    monkeypatch.setenv("PLUGIN_ROOT", PLUGIN_ROOT)
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", os.pathsep.join(sys.path))
    child = subprocess.Popen([sys.executable, str(runner)], env=env)
    # Parent and child now race the transaction concurrently.
    ctx.hook("post_tool_call", tool_name="terminal", args={},
             result="boom race", status="error", error_type="E",
             error_message="boom race")
    try:
        child.wait(timeout=120)
    finally:
        if child.poll() is None:
            child.kill()
            raise AssertionError("second process hung — possible lock deadlock")
    deadline = clock.time() + 30
    while (clock.time() < deadline
           and ctx.context.state.get("auto_capture", {}).get("n") != cap):
        clock.sleep(0.2)
    records = _records(store)
    # Exactly ONE filing across both processes: quota is atomic, not merely
    # thread-local. The rejected process files nothing and reports failure.
    assert len(records) == 1, records
    assert ctx.context.state.get("auto_capture") == {"day": day, "n": cap}


def _native_supports(mode, flag):
    """True when the installed native build defines ``flag`` for ``mode``."""
    proc = subprocess.run([BINARY, mode, "--help"], capture_output=True, text=True)
    return flag in f"{proc.stdout or ''}{proc.stderr or ''}"


def test_reported_loss_accumulates_without_imputing_missing_timing(papercuts):
    ctx, store = papercuts
    args = {"mode": "report", "category": "workflow", "severity": "high",
            "summary": "repeated setup delay"}
    for extra in ({"time_lost_seconds": 42}, {"time_lost_seconds": 8}, {}):
        result = json.loads(ctx.tool("papercuts", {**args, **extra}))
        assert result["success"], result
    rows = _records(store)
    assert len(rows) == 1
    assert rows[0]["count"] == 3
    if _native_supports("report", "--time-lost-seconds"):
        # A build that owns the timing field accumulates the reported loss itself.
        assert (rows[0]["time_lost_seconds"], rows[0]["timed_report_count"]) == (50.0, 2)
    else:
        # v0.1.0 defines no timing field. Emitting the flag anyway made argparse reject the
        # whole report, losing the papercut; the plugin must file it and carry the observed
        # loss in the context instead, and must never invent native columns that do not exist.
        assert "time_lost_seconds" not in rows[0]
        assert rows[0]["context"] == "observed time lost: 42.0s"


@pytest.mark.parametrize("invalid", [True, -1, float("nan"), float("inf"), "42", 10**400])
def test_invalid_reported_loss_preserves_native_store(papercuts, invalid):
    ctx, store = papercuts
    args = {"mode": "report", "category": "workflow", "severity": "low",
            "summary": "existing report"}
    assert json.loads(ctx.tool("papercuts", args))["success"]
    path = store / "records.jsonl"
    before = path.read_bytes()
    rejected = json.loads(ctx.tool("papercuts", {**args, "time_lost_seconds": invalid}))
    assert rejected["success"] is False
    assert path.read_bytes() == before


def test_cli_passthrough_bounds_the_subprocess(papercuts, monkeypatch):
    # The passthrough subprocess must carry a timeout and report timeout
    # observably (exit 124) instead of hanging the CLI.
    ctx, _ = papercuts
    seen = {}
    real_run = __import__("subprocess").run

    def spy(argv, **kw):
        seen.update(argv=argv, **kw)
        return real_run(argv, **kw)

    monkeypatch.setattr(ctx.module.subprocess, "run", spy)
    assert ctx.module._passthrough(["true"]) == 0
    assert seen["timeout"] == ctx.module.PASSTHROUGH_TIMEOUT_SECONDS

    # A subprocess outliving the timeout yields exit code 124, not a hang.
    monkeypatch.setattr(ctx.module, "PASSTHROUGH_TIMEOUT_SECONDS", 1)
    assert ctx.module._passthrough(
        [sys.executable, "-c", "import time; time.sleep(30)"]) == 124
