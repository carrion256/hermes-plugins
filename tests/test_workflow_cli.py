"""Real Hermes CLI/SDK/ledger/reviewer with only the HTTP model boundary controlled."""
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def test_cli_plans_verifies_reviews_and_finishes(tmp_path):
    home, workspace = tmp_path / "cli-home", tmp_path / "project"
    (home / "plugins").mkdir(parents=True)
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["."]\n')
    names = ["rules", "keyword-steering", "auto-answer-classifier",
             "auto-review-classifier", "continuation", "prewalk", "loop-guard", "subagent-effort"]
    plugin_root = Path(__file__).resolve().parents[1] / "plugins"
    for name in names:
        (home / "plugins" / name).symlink_to(plugin_root / name, target_is_directory=True)
    rules = workspace / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "python.md").write_text(
        '---\ntools: [write_file]\nglobs: ["*.py"]\n---\nKeep guidance out of executable Python.\n'
    )
    (workspace / "widget.py").write_text("ANSWER = 0\n")
    (workspace / "test_widget.py").write_text(
        "from widget import ANSWER\ndef test_answer():\n    assert ANSWER == 42\n")
    steps = [
        ("prewalk_arm", {}),
        ("todo_list", {"todos": [{"id": "work", "content": "Implement and verify widget", "status": "in_progress"}]}),
        ("acceptance", {"action": "declare", "id": "work",
                        "observable": "widget.ANSWER is 42 under the unchanged test",
                        "check_kind": "terminal", "check_identity": "python3 -m pytest -q",
                        "paths": ["widget.py"]}),
        ("prewalk_ready", {
            "files": ["widget.py", "test_widget.py"], "symbols": ["widget.ANSWER"],
            "callers": ["test_widget.test_answer"], "constraints": ["Keep the test unchanged"],
            "non_goals": ["No unrelated files"], "chosen_approach": "Repair the incorrect constant",
            "rejected_approaches": ["Do not weaken the assertion"],
            "falsification_checks": ["python3 -m pytest -q"],
            "replan_triggers": ["The focused test still fails"], "acceptance_ids": ["work"]}),
        *[("terminal", {"command": "python3 -m pytest -q", "timeout": 30}) for _ in range(4)],
        ("loop_recovery", {"action": "status"}),
        ("loop_recovery", {
            "action": "hypothesis", "hypothesis": "The implementation constant violates the unchanged assertion",
            "next_tool": "write_file",
            "next_arguments": {"path": "widget.py", "content": "ANSWER = 42\n"},
            "expected_observation": "The same pytest command passes after correcting the constant"}),
        # The native stale-overwrite guard: establish the full-content
        # baseline for this task before rewriting the existing file.
        ("read_file", {"path": "widget.py"}),
        ("write_file", {"path": "widget.py", "content": "ANSWER = 42\n"}),
        ("search_files", {"pattern": "ANSWER", "path": ".", "file_glob": "*.py", "limit": 2}),
        ("terminal", {"command": "python3 -m pytest -q", "timeout": 30}),
        ("loop_recovery", {"action": "status"}),
        ("acceptance", {"action": "bind", "id": "work"}),
        ("acceptance", {"action": "status"}),
        ("todo_list", {"todos": [{"id": "work", "content": "Implement and verify widget", "status": "completed"}]}),
    ]
    observed = {"step": 0, "last": None, "models": [], "reviews": 0, "reminders": 0,
                "evidence": [], "criteria": [], "review_context": "", "loop_reports": []}
    lock = threading.Lock()

    class ModelBoundary(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_json({"object": "list", "data": [{"id": name, "object": "model", "context_length": 131072} for name in ["audit-planner", "audit-executor"]]})

        def send_json(self, payload):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = request.get("messages", [])
            text = "\n".join(str(message.get("content") or "") for message in messages)
            reviewer = "You were spawned by the /review command." in text
            with lock:
                if reviewer:
                    observed["reviews"] += 1
                    observed["review_context"] = text
                    call, content = None, json.dumps({"review_verdict": "clean", "findings": [],
                                                     "coverage": {"work": "covered"}})
                elif not request.get("tools"):
                    call, content = None, "Widget implementation"
                else:
                    observed["models"].append(request["model"])
                    last = messages[-1] if messages else {}
                    raw = last.get("content", "") if last.get("role") == "tool" else ""
                    try:
                        result = json.loads(raw) if raw else {}
                    except (ValueError, TypeError):
                        result = {}
                    if isinstance(result, dict) and result.get("verification_evidence"):
                        observed["evidence"].append(result["verification_evidence"])
                    if isinstance(result, dict) and "criteria" in result:
                        observed["criteria"] = result["criteria"]
                    if isinstance(result, dict) and "observed_recoveries" in result:
                        observed["loop_reports"].append(result)
                    guided = "[RULE GUIDANCE" in raw or (isinstance(result, dict) and result.get("rule_guidance"))
                    if guided:
                        observed["reminders"] += 1
                        if observed["step"] < len(steps):
                            call, content = steps[observed["step"]], None
                            if call[0] == "acceptance" and call[1]["action"] == "bind":
                                evidence_id = observed["evidence"][-1]["id"]
                                call = (call[0], {**call[1], "evidence_id": evidence_id})
                            observed["last"] = call
                            observed["step"] += 1
                        else:
                            call, content = None, "Implemented and verified widget."
                    elif observed["step"] < len(steps):
                        call, content = steps[observed["step"]], None
                        if call[0] == "acceptance" and call[1]["action"] == "bind":
                            evidence_id = observed["evidence"][-1]["id"]
                            call = (call[0], {**call[1], "evidence_id": evidence_id})
                        observed["last"] = call
                        observed["step"] += 1
                    else:
                        call, content = None, "Implemented and verified widget."
            if call is None and not reviewer:
                time.sleep(0.25)  # Native async reviewer must publish before the next final answer.
            advertised = {tool["function"]["name"] for tool in request.get("tools", [])}
            if call and call[0] not in advertised:
                call = ("tool_call", {"calls": [{"name": call[0], "arguments": call[1]}]})
            calls = [{"id": f"call-{time.monotonic_ns()}", "type": "function", "function": {"name": call[0], "arguments": json.dumps(call[1])}}] if call else None
            reason = "tool_calls" if calls else "stop"
            usage = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}
            if request.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                delta = {"role": "assistant"}
                if calls:
                    delta["tool_calls"] = [{"index": 0, **calls[0]}]
                else:
                    delta["content"] = content
                for part, finish in [(delta, None), ({}, reason)]:
                    chunk = {"id": "audit", "object": "chat.completion.chunk", "created": 1, "model": request.get("model"), "choices": [{"index": 0, "delta": part, "finish_reason": finish}]}
                    if finish:
                        chunk["usage"] = usage
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                message = {"role": "assistant", "content": content}
                if calls:
                    message["tool_calls"] = calls
                self.send_json({"id": "audit", "object": "chat.completion", "created": 1, "model": request.get("model"), "choices": [{"index": 0, "message": message, "finish_reason": reason}], "usage": usage})

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelBoundary)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    config = {
        "model": "audit-planner",
        "providers": {"audit": {"base_url": f"http://127.0.0.1:{server.server_address[1]}/v1", "api_key": "synthetic-test-key", "api_mode": "chat_completions"}},
        "terminal": {"backend": "local", "cwd": str(workspace)},
        "agent": {"verify_on_stop": True, "max_verify_nudges": 6, "reasoning_effort": "none"},
        "plugins": {"enabled": names, "entries": {name: {"enabled": True, "settings": {}} for name in names}},
    }
    config["plugins"]["entries"]["prewalk"]["settings"] = {"target_model": "audit-executor"}
    config["plugins"]["entries"]["loop-guard"]["settings"] = {"tool_call_streak": 3, "redirect_cap": 1}
    (home / "config.yaml").write_text(json.dumps(config))
    environment = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")}
    environment.update(HERMES_HOME=str(home), HERMES_PROFILE="default", HERMES_VERIFY_ON_STOP="1", PYTHONDONTWRITEBYTECODE="1")
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "chat", "--provider", "audit", "--model", "audit-planner",
             "--reasoning", "none", "--toolsets", "all", "--max-turns", "24", "--run-budget", "90", "--quiet", "--oneshot",
             "--query", "Implement widget.py with ANSWER=42, add a test, plan the work, run verification, and obtain independent review before finishing."],
            cwd=workspace, env=environment, capture_output=True, text=True, timeout=120,
        )
        review_deadline = time.monotonic() + 5
        while observed["reviews"] < 1 and time.monotonic() < review_deadline:
            time.sleep(0.05)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (workspace / "widget.py").read_text() == "ANSWER = 42\n"
    assert observed["reminders"] == 1
    assert "audit-executor" in observed["models"]
    assert any(evidence["status"] == "passed" and evidence["canonical_command"] == "pytest" for evidence in observed["evidence"]), (observed["evidence"], result.stdout)
    db = home / "state.db"
    review_rows = []
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            review_rows = [row for row in conn.execute(
                "SELECT task_json, state FROM async_delegations")
                if "You were spawned by the /review command." in (row[0] or "")]
        finally:
            conn.close()
    assert observed["reviews"] == 1 or review_rows, {
        "criteria": observed["criteria"], "evidence": observed["evidence"],
        "reviews": observed["reviews"], "review_rows": review_rows,
        "stdout": result.stdout[-4000:], "stderr": result.stderr[-2000:]}
    criterion = next(row for row in observed["criteria"] if row["id"] == "work")
    assert criterion["status"] == "verified"
    assert criterion["evidence_id"] in {row["id"] for row in observed["evidence"]}
    assert "ANSWER=42" in observed["review_context"]
    assert "Completion unverified" not in result.stdout, result.stdout
    assert "Implemented and verified widget." in result.stdout
    assert observed["loop_reports"][0]["blocker"] is not None
    assert observed["loop_reports"][0]["delivered_interventions"] == 1
    assert observed["loop_reports"][-1]["blocker"] is None
    assert observed["loop_reports"][-1]["observed_recoveries"] == 1
    assert observed["evidence"][0]["status"] == "failed"
