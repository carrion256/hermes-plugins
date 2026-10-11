"""Actual Hermes CLI over a controlled loopback model boundary + real Covey.

AC9/AC2 end-to-end: a real hermes process plans with native todos, executes a
tiny code task with real verification, records a durable Covey receipt, and a
FRESH hermes process discovers/resumes exactly the same persisted state through
the plugin itself (raw covey CLI is used only for independent byte-level
cross-checks). Prewalk is armed with a real target_model: Covey tool calls
alone must never count as native todo progress or hand the session to the
executor. Only the HTTP model boundary is controlled; everything else (loader,
dispatcher, todo gates, covey binary, subprocesses) is real.
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

COVEY_BIN = os.environ.get("COVEY_TEST_BIN", "")
TASK_PROMPT = "cli integration task"
WIDGET = "CLI_ANSWER = 42\n"
TEST_SOURCE = ("from cliwidget import CLI_ANSWER\n"
               "def test_cli_answer():\n    assert CLI_ANSWER == 42\n")


@pytest.fixture
def covey_bin():
    if not COVEY_BIN:
        pytest.fail(
            "COVEY_TEST_BIN is required for this module: point it at a covey "
            "binary with meta list + subtask finish support "
            "(cargo build --bin covey). It must not be silently skipped.")
    if not Path(COVEY_BIN).exists():
        pytest.fail(f"COVEY_TEST_BIN={COVEY_BIN} does not exist")
    return COVEY_BIN


def covey_cli(db_path, *args):
    result = subprocess.run(
        [COVEY_BIN, "--db", str(db_path), "--json", *args],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True, payload
    return payload["data"]


def tool_results(messages):
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(message.get("content", ""))
        except (ValueError, TypeError):
            payload = None
        if payload is None and isinstance(message.get("content"), dict):
            payload = message["content"]
        if isinstance(payload, dict):
            yield payload


def plugin_results(messages):
    for result in tool_results(messages):
        if isinstance(result.get("data"), dict):
            yield result


def todo_call(status):
    return ["todo_list", {"todos": [{
        "id": "work", "content": "Implement and verify cli widget", "status": status}]}]


def test_cli_covey_task_with_native_verification_and_resume(tmp_path, covey_bin):
    home, workspace = tmp_path / "cli-home", tmp_path / "project"
    (home / "plugins").mkdir(parents=True)
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["."]\n')
    (workspace / "cliwidget.py").write_text("CLI_ANSWER = 0\n")
    (workspace / "test_cliwidget.py").write_text(TEST_SOURCE)
    names = ["continuation", "prewalk", "covey-tasks"]
    plugin_root = Path(__file__).resolve().parents[1] / "plugins"
    for name in names:
        (home / "plugins" / name).symlink_to(plugin_root / name, target_is_directory=True)
    db_path = tmp_path / "covey.db"

    # The boundary's policy is derived from the conversation transcript and
    # OBSERVED TOOL RESULTS (parsed tool-role messages), never from transcript
    # text, a fixed schedule, or flags flipped at call-emit time.
    observed = {"models": [], "evidence": [], "executor_after": None,
                "resume_requests": 0,
                "covey": {"create": None, "add": None, "start": None, "finish": None},
                "resume": {"lists": None, "show": None}}
    lock = threading.Lock()
    state = {"phase": 0}
    process = {"n": 0}  # 0 = worklist-creation process, 1 = fresh resume process

    def covey_ok(result, name):
        return name == "covey_tasks" and result.get("ok") is True

    # Each entry: (tool call, predicate over tool results that proves THIS
    # step completed). The driver emits a step and does not advance until its
    # predicate holds on real results — success is never marked before the
    # result exists.
    def done_create(results):
        for result in results:
            if covey_ok(result, "covey_tasks") and isinstance(result["data"].get("meta_task_id"), str):
                observed["covey"]["create"] = result
                return True
        return False

    def done_add(results):
        for result in results:
            if covey_ok(result, "covey_tasks") and isinstance(result["data"].get("subtask_id"), str):
                observed["covey"]["add"] = result
                return True
        return False

    def done_start(results):
        for result in results:
            if covey_ok(result, "covey_tasks") and result["data"].get("state") in ("claimed", "in_progress"):
                observed["covey"]["start"] = result
                return True
        return False

    def done_todo_in_progress(results):
        for result in results:
            if isinstance(result.get("todos"), list) and any(
                    t.get("id") == "work" and t.get("status") == "in_progress"
                    for t in result["todos"] if isinstance(t, dict)):
                return True
        return False

    def done_widget(results):
        return (workspace / "cliwidget.py").read_text() == WIDGET if (workspace / "cliwidget.py").exists() else False

    def done_pytest(results):
        for result in results:
            text = json.dumps(result)
            if "1 passed" in text and "1 failed" not in text:
                return True
        return False

    def done_todo_completed(results):
        for result in results:
            if isinstance(result.get("todos"), list) and any(
                    t.get("id") == "work" and t.get("status") == "completed"
                    for t in result["todos"] if isinstance(t, dict)):
                return True
        return False

    def done_finish(results):
        for result in results:
            if covey_ok(result, "covey_tasks") and result["data"].get("state") == "completed":
                observed["covey"]["finish"] = result
                return True
        return False

    steps = [
        # Prewalk arms on the native todo gate, NOT on covey tool calls: the
        # arm itself is a plugin tool result.
        (["prewalk_arm", {}], lambda results: True),
        (["covey_tasks", {"action": "create", "prompt": TASK_PROMPT}], done_create),
        (["covey_tasks", {"action": "add",
                          "meta_task_id": lambda: observed["covey"]["create"]["data"]["meta_task_id"],
                          "title": "implement cli widget", "priority": 100}], done_add),
        (["covey_tasks", {"action": "start",
                          "subtask_id": lambda: observed["covey"]["add"]["data"]["subtask_id"]}], done_start),
        (todo_call("in_progress"), done_todo_in_progress),
        (["acceptance", {"action": "declare", "id": "work",
                         "observable": "CLI_ANSWER equals 42 under the unchanged test",
                         "check_kind": "terminal", "check_identity": "python3 -m pytest -q",
                         "paths": ["cliwidget.py", "test_cliwidget.py"]}],
         lambda results: any(r.get("success") is True and r.get("id") == "work"
                             for r in results)),
        (["prewalk_ready", {
            "files": ["cliwidget.py", "test_cliwidget.py"], "symbols": ["cliwidget.CLI_ANSWER"],
            "callers": ["test_cliwidget.test_cli_answer"], "constraints": ["Keep the test unchanged"],
            "non_goals": ["No unrelated changes"], "chosen_approach": "Correct the constant",
            "rejected_approaches": ["Weakening the test"], "falsification_checks": ["python3 -m pytest -q"],
            "replan_triggers": ["The focused test fails"], "acceptance_ids": ["work"]}],
         lambda results: any(r.get("handed_off") is True for r in results)),
        # The native stale-overwrite guard: establish the full-content
        # baseline for this task before rewriting the existing file.
        (["read_file", {"path": "cliwidget.py"}], lambda results: True),
        (["write_file", {"path": "cliwidget.py", "content": WIDGET}], done_widget),
        (["terminal", {"command": "python3 -m pytest -q", "timeout": 60}], done_pytest),
        (["acceptance", {"action": "bind", "id": "work",
                         "evidence_id": lambda: observed["evidence"][-1]["id"]}],
         lambda results: any(r.get("success") is True and r.get("id") == "work"
                             and isinstance(r.get("evidence_id"), int) for r in results)),
        (todo_call("completed"), done_todo_completed),
        (["covey_tasks", {"action": "finish",
                          "subtask_id": lambda: observed["covey"]["add"]["data"]["subtask_id"],
                          "summary": "implemented and verified via CLI"}], done_finish),
    ]

    def resolve(call):
        return [call[0], {key: (value() if callable(value) else value) for key, value in call[1].items()}]

    def next_action(messages):
        results = list(tool_results(messages))
        for result in results:
            evidence = result.get("verification_evidence")
            if evidence:
                observed["evidence"].append(evidence)
        # prewalk_arm fires unconditionally on the first request: it has no
        # result predicate (its success is the arm itself).
        if state["phase"] == 0:
            state["phase"] = 1
            return resolve(steps[0][0]), None
        if state["phase"] < len(steps) and steps[state["phase"]][1](results):
            state["phase"] += 1
        if state["phase"] < len(steps):
            return resolve(steps[state["phase"]][0]), None
        return None, ("Covey worklist completed and verified with pytest; "
                      "verification evidence is in the session ledger, the "
                      "Covey receipt records the completion only (not review).")

    def next_resume_action(messages):
        results = {name: result for name, result in
                   (("lists", observed["resume"]["lists"]), ("show", observed["resume"]["show"]))
                   if result is not None}
        if "lists" not in results:
            return ["covey_tasks", {"action": "lists", "limit": 500}], None
        meta_id = observed["covey"]["create"]["data"]["meta_task_id"]
        if "show" not in results:
            return ["covey_tasks", {"action": "show", "meta_task_id": meta_id}], None
        return None, ("Resumed: the completed Covey task and its receipt are "
                      "visible in this fresh process.")

    def capture_resume(messages):
        for result in plugin_results(messages):
            data = result["data"]
            if "items" in data and observed["resume"]["lists"] is None:
                observed["resume"]["lists"] = result
            elif "subtasks" in data:
                observed["resume"]["show"] = result

    class ModelBoundary(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_json({"object": "list", "data": [
                {"id": name, "object": "model", "context_length": 131072}
                for name in ["covey-planner", "covey-executor"]]})

        def send_json(self, payload):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = request.get("messages", [])
            reviewer = "You were spawned by the /review command." in json.dumps(messages)
            with lock:
                if reviewer:
                    call, content = None, json.dumps({"review_verdict": "clean", "findings": [],
                                                     "coverage": {"work": "covered"}})
                elif not request.get("tools"):
                    # Title/summary requests are not agent execution turns and
                    # must never consume a scripted tool action.
                    call, content = None, "Covey workflow qualification"
                else:
                    observed["models"].append(request["model"])
                    if observed["models"][-1] == "covey-executor" and observed["executor_after"] is None:
                        observed["executor_after"] = done_todo_in_progress(list(tool_results(messages)))
                    if process["n"] >= 1:
                        observed["resume_requests"] += 1
                        capture_resume(messages)
                        call, content = next_resume_action(messages)
                    else:
                        call, content = next_action(messages)
            if call is None:
                time.sleep(0.25)  # Native async verify/review must publish before the final answer.
            advertised = {tool["function"]["name"] for tool in request.get("tools", [])}
            if call and call[0] not in advertised:
                call = ["tool_call", {"calls": [{"name": call[0], "arguments": call[1]}]}]
            calls = [{"id": f"call-{time.monotonic_ns()}", "type": "function",
                      "function": {"name": call[0], "arguments": json.dumps(call[1])}}] if call else None
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
                    chunk = {"id": "covey-e2e", "object": "chat.completion.chunk", "created": 1,
                             "model": request.get("model"),
                             "choices": [{"index": 0, "delta": part, "finish_reason": finish}]}
                    if finish:
                        chunk["usage"] = usage
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                message = {"role": "assistant", "content": content}
                if calls:
                    message["tool_calls"] = calls
                self.send_json({"id": "covey-e2e", "object": "chat.completion", "created": 1,
                                "model": request.get("model"),
                                "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                                "usage": usage})

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelBoundary)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    config = {
        "model": "covey-planner",
        "providers": {"covey-e2e": {"base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
                                    "api_key": "synthetic-test-key", "api_mode": "chat_completions"}},
        "terminal": {"backend": "local", "cwd": str(workspace)},
        "agent": {"verify_on_stop": True, "max_verify_nudges": 6, "reasoning_effort": "none"},
        "plugins": {"enabled": names,
                    "entries": {name: {"enabled": True, "settings": {}} for name in names}},
    }
    config["plugins"]["entries"]["covey-tasks"]["settings"] = {
        "binary": covey_bin, "db_path": str(db_path)}
    config["plugins"]["entries"]["prewalk"]["settings"] = {"target_model": "covey-executor"}
    (home / "config.yaml").write_text(json.dumps(config))
    environment = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")}
    environment.update(HERMES_HOME=str(home), HERMES_PROFILE="default",
                       HERMES_VERIFY_ON_STOP="1", PYTHONDONTWRITEBYTECODE="1")
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    hermes_command = [sys.executable, "-m", "hermes_cli.main", "chat", "--provider", "covey-e2e",
                      "--reasoning", "none", "--toolsets", "all", "--quiet", "--oneshot"]
    try:
        result = subprocess.run(
            hermes_command + ["--model", "covey-planner", "--max-turns", "24", "--run-budget", "120",
                              "--query", "Create a covey worklist for this, plan with todos, implement "
                                         "cliwidget.py with CLI_ANSWER=42 plus its test, run verification, "
                                         "then record the durable completion."],
            cwd=workspace, env=environment, capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stderr + result.stdout

        process["n"] = 1  # the boundary now drives the fresh resume process
        # Fresh-process resume THROUGH THE PLUGIN: a second, independent hermes
        # process sees the same worklist and terminal task.
        resume = subprocess.run(
            hermes_command + ["--model", "covey-planner", "--max-turns", "8", "--run-budget", "60",
                              "--query", "Resume: list covey worklists and show the task I worked on."],
            cwd=workspace, env=environment, capture_output=True, text=True, timeout=120)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
    assert resume.returncode == 0, resume.stderr + resume.stdout

    # Phase machine completed end to end: every step advanced on a real result.
    assert observed["covey"]["create"] and observed["covey"]["add"]
    assert observed["covey"]["start"] and observed["covey"]["finish"]
    meta_id = observed["covey"]["create"]["data"]["meta_task_id"]
    subtask_id = observed["covey"]["add"]["data"]["subtask_id"]
    assert (workspace / "cliwidget.py").read_text() == WIDGET

    # Prewalk: native todo gate only. The executor model appears only once the
    # NATIVE todo store holds a planned in_progress item; covey tool calls
    # alone never advance that gate.
    models = observed["models"]
    assert "covey-executor" in models, models
    assert observed["executor_after"] is True, observed

    # Real verification evidence precedes the finish (native ledger).
    assert any(e.get("status") == "passed" and e.get("canonical_command") == "pytest"
               for e in observed["evidence"]), observed["evidence"]

    # Fresh process saw exactly the same terminal receipt via the plugin.
    assert observed["resume"]["lists"] is not None and observed["resume"]["show"] is not None
    listed_ids = [item["meta_task_id"] for item in observed["resume"]["lists"]["data"]["items"]]
    assert listed_ids == [meta_id]
    shown = observed["resume"]["show"]["data"]
    assert shown["meta_task"]["state"] in ("completed", "closed")
    assert shown["counts"].get("completed") == 1
    assert [t for t in shown["subtasks"] if t["subtask_id"] == subtask_id][0]["state"] == "completed"

    # Independent raw-CLI cross-check (exact flags), and exactly one receipt.
    view = covey_cli(db_path, "subtask", "status", "--subtask-id", subtask_id)
    assert view["subtask"]["state"] == "completed"
    assert view["claim"] is None  # release on teardown: no live claim left
    receipts = [o for o in view["attempt_outcomes"] if o.get("outcome_kind") == "succeeded"]
    assert len(receipts) == 1, view
    assert receipts[0]["summary"] == "implemented and verified via CLI"
    listed = covey_cli(db_path, "meta", "list", "--limit", "500")
    assert [item["meta_task_id"] for item in listed] == [meta_id]
    # No review claim: the completion receipt is not asserted as independent review.
    assert "review" not in json.dumps(listed).lower()
