"""Temporary acceptance check for at-most-once reminder delivery."""
from __future__ import annotations

import sys
import threading


def _meta(cwd, session_id="sess-1", profile="main"):
    return {"session_id": session_id, "model": "m", "provider": "p",
            "platform": "cli", "profile_name": profile, "cwd": str(cwd)}


def _write_rule(path, front_matter="", body="BODY"):
    text = (f"---\n{front_matter}\n---\n{body}\n") if front_matter else f"{body}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _harness(load_plugin, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return load_plugin("rules", settings={
        "rule_dirs": [str(tmp_path / "global")],
        "ttsr_enabled": True, "disabled_rules": [],
        "rules_explain_enabled": True})


def _seed(h, cwd, session_id="sess-1"):
    h.manager.render_system_prompt_sections(_meta(cwd, session_id=session_id))


def _call(h, session_id, next_call):
    return h.middleware("tool_execution", tool_name="terminal",
                        args={"command": "ls"}, next_call=next_call,
                        session_id=session_id, task_id="t1", tool_call_id="c1",
                        turn_id="turn-1", api_request_id="api-1")


def test_parallel_batch_delivers_reminder_once(load_plugin, tmp_path, monkeypatch):
    for i in range(300):
        _write_rule(tmp_path / "global" / f"guard{i}.md", "tools: [terminal]",
                    body=f"GUARD{i}")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)

    n = 48
    start = threading.Barrier(n)
    delivered = []
    lock = threading.Lock()

    def next_call(next_args=None):
        return "EXECUTED"

    def worker():
        start.wait(10)
        out = _call(h, "sess-1", next_call)
        with lock:
            delivered.append("GUARD0" in out)

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # force interleaving inside the selection loop
    try:
        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        sys.setswitchinterval(old)
    assert len(delivered) == n
    assert sum(delivered) == 1, f"reminder delivered {sum(delivered)} times"


def test_failed_tool_does_not_consume_reminder(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", "tools: [terminal]", body="GUARD")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)

    def boom(next_args=None):
        raise RuntimeError("tool blew up")

    try:
        _call(h, "sess-1", boom)
    except RuntimeError:
        pass
    else:
        raise AssertionError("middleware must re-raise tool failures")
    assert "GUARD" in _call(h, "sess-1", lambda a=None: "EXECUTED")


def test_structural_result_does_not_consume_reminder(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", "tools: [terminal]", body="GUARD")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)

    assert _call(h, "sess-1", lambda a=None: {"ok": True}) == {"ok": True}
    out = _call(h, "sess-1", lambda a=None: "EXECUTED")
    assert out.startswith("EXECUTED") and "GUARD" in out
