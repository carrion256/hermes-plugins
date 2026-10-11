"""Behavioral checks for browser-bladebro's direct-artifact certification
protocol on the qualified native run shape.

Canned envelopes replicate browser-batch-observations.json framing exactly:
step 1 open, step 2 exact-URL js guard ->then, nested step 2.0 initial text
predicate ->then, step 2.0.0 dom-changed outcome, step 3 final text predicate
->then with step 3.0 backend see. False guards return whole-response MCP
errors carrying the plugin throw marker. Evidence goes through the REAL host
ledger API (agent.verification_evidence) with actual row ids; the
disabled-ledger truth (unbound) is asserted separately. No live browser is
contacted; the parent owns the native backend smoke.
"""
import hashlib
import json
import pathlib

import pytest

from hermes_cli.plugins import PluginContext


TAB = "EA6FDE15266206A4CF5F7416103D5039"
OTHER_TAB = "73183E98EF64C1AA53BC4268DE2DCC7A"
SESSION = "sess-1"
TASK = "task-t"

# The qualification fixture itself (browser-batch-qualification.py), byte-for-byte.
FIXTURE_HTML = (
    "<!doctype html><title>Qualified fixture</title>"
    '<button id="save" onclick="document.getElementById(\'state\')'
    ".textContent='State: committed'\">Commit</button>"
    '<p id="state" role="status">State: pending</p>'
)


def qualified_run_output(url: str) -> str:
    """The qualified atomic_flow result shape (browser-batch-observations.json).
    The final see sits inside the plugin's exact-URL guard, so the backend
    numbers it step 3.0.0 (nested under the final text predicate)."""
    page = f"Page: {url[:40]}… | phase: ready | 1 actionable"
    return (
        f"step 1: ✓ opened + switched to tab {TAB}\n{page}\n"
        'title: Qualified fixture\ne1 button "Commit"\n\n'
        f'step 2: if(js "location.href === {json.dumps(url)}") → then\n'
        'step 2.0: if(text "State: pending") → then\n'
        "step 2.0.0: outcome: dom-changed (content-only) via mouse\n"
        f"{page}\ntitle: Qualified fixture\n  e1 button \"Commit\"\n\n"
        'step 3: if(text "State: committed") → then\n'
        'step 3.0: if(js "location.href === ...") → then\n'
        "step 3.0.0: see (207 chars):\n"
        f"{page}\ntitle: Qualified fixture\ne1 button \"Commit\"\n\n"
        "--- page content ---\nCommitState: committed\n"
    )


def false_guard_error(branch: str) -> str:
    return (
        f"✗ error: step 2 failed: step 2.0 js failed: eval failed: "
        f"Error: HERMES_BLADEBRO_GUARD_FALSE:{branch}\n"
        "    at eval (eval at <anonymous> (:1:21), <anonymous>:1:7)\n"
        "\n--- current page state ---\nPage: about:blank | phase: ready | 0 actionable\n"
    )


@pytest.fixture
def fake_mcp(monkeypatch):
    calls = []
    router = {}

    def call_mcp(self, server, tool, arguments=None, timeout=30,
                 replay_on_failure=True):
        calls.append((tool, dict(arguments or {}),
                      {"replay_on_failure": replay_on_failure}))
        handler = router.get(tool)
        if handler is None:
            env = {"ok": True, "result": ""}
        else:
            env = handler(dict(arguments or {}))
        # Real host behavior (McpReplayPolicyImpl): successful calls carry the
        # host-minted connection token; failures do not. Callers may pin or
        # omit the token by returning it explicitly from the router handler.
        if env.get("ok") and "connection_token" not in env:
            env = {**env, "connection_token": "tok-host"}
        return env

    monkeypatch.setattr(PluginContext, "call_mcp", call_mcp)
    return calls, router


@pytest.fixture
def browser(load_plugin, fake_mcp, tmp_path, monkeypatch):
    from tools.terminal_tool import record_session_cwd
    record_session_cwd(TASK, str(tmp_path))
    (tmp_path / "fixture.html").write_text(FIXTURE_HTML)
    # The native ledger only exists inside detected code workspaces.
    (tmp_path / "package.json").write_text('{"scripts": {"test": "node --test"}}')
    ctx = load_plugin("browser-bladebro")
    # The module monkeypatcher rides along as browser[3] for fixture-level
    # helpers; tests may also request monkeypatch normally.
    return ctx, fake_mcp[0], fake_mcp[1], monkeypatch


def _capture(ctx, sid="s1"):
    return json.loads(ctx.tool("browser_capture", {"scenario_id": sid},
                               session_id=SESSION, task_id=TASK))


def _approve(monkeypatch):
    monkeypatch.setattr("tools.approval.request_tool_approval",
                        lambda *a, **k: {"approved": True, "message": None})


def _fake_build_ok(root, cmd="bun run build", body="<p>built</p>"):
    """Native terminal envelope (terminal_tool_result.py): output/exit_code/
    error/verification_evidence — no success key. Returns a handle_function_call
    fake that produces the artifact and the native build row."""
    def fake(fn, fa, **kw):
        (root / "dist.html").write_text("<!doctype html>" + body)
        return json.dumps({"output": "", "exit_code": 0, "error": None,
                           "verification_evidence": {"id": 42, "status": "passed",
                                                     "kind": "build",
                                                     "check_id": cmd}})
    return fake


def _approve_build_ledger(monkeypatch, cmd="bun run build"):
    """Accept the mutating build dispatch and serve the matching native row."""
    _approve(monkeypatch)
    from agent import verification_evidence as ledger
    monkeypatch.setattr(
        ledger, "verification_evidence_by_id",
        lambda eid, *, session_id, cwd: {
            "status": "passed",
            "evidence": {"id": eid, "kind": "build", "exit_code": 0,
                         "command": cmd, "cwd": str(cwd),
                         "session_id": session_id}})


def _canonical(sources: dict) -> str:
    payload = b"".join(f"{k}\0{v}\n".encode() for k, v in sorted(sources.items()))
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _declare(ctx, **over):
    args = {
        "action": "declare", "scenario_id": "s1",
        "sources": ["fixture.html"], "entry_artifact": "fixture.html",
        "initial_predicate": {"text": "State: pending"},
        "final_predicate": {"text": "State: committed"},
        "interaction": {"action": "click", "text": "Commit"},
    }
    args.update(over)
    return json.loads(ctx.tool("browser_scenario", args,
                               session_id=SESSION, task_id=TASK))


# ------------------------------------------------------------- declare tests

def test_declare_pins_sources_and_canonical_revision(browser):
    ctx, calls, router, browser_mp = browser
    declared = _declare(ctx)
    assert declared["success"] and declared["mode"] == "direct_html"
    file_sha = hashlib.sha256(
        pathlib.Path(ctx.home).parent.joinpath("fixture.html").read_bytes()).hexdigest()
    assert declared["source_revision"] == _canonical({"fixture.html": file_sha})
    assert calls == []  # declare touches no native surface


def test_declare_refuses_selector_role_predicates(browser):
    ctx, _, _, browser_mp = browser
    out = _declare(ctx, initial_predicate={"selector": "#state"})
    assert out["success"] is False and "natively observable" in out["error"]


def test_declare_refuses_js_or_multi_interaction(browser):
    ctx, _, _, browser_mp = browser
    for bad in ({"action": "eval", "js": "1+1"},
                {"action": "navigate", "url": "http://x"},
                {"action": "click", "text": "Commit", "optional": True}):
        out = _declare(ctx, interaction=bad)
        assert out["success"] is False, bad


def test_declare_compiled_mode_separates_build_inputs(browser):
    ctx, _, _, browser_mp = browser
    (pathlib.Path(ctx.home).parent / "build.ts").write_text("// src")
    out = _declare(ctx, sources=["build.ts"], entry_artifact="dist.html",
                   build_command="bun build ./build.ts --outfile dist.html")
    assert out["success"] and out["mode"] == "compiled"
    # build path + direct-HTML collision refused
    clash = _declare(ctx, scenario_id="s2", sources=["dist.html", "build.ts"],
                     entry_artifact="dist.html", build_command="bun build x")
    assert clash["success"] is False and "direct-HTML" in clash["error"]



def test_declare_accepts_keyboard_interactions(browser):
    ctx, _, _, browser_mp = browser
    ok = _declare(ctx, interaction={"action": "press", "text": "Commit"})
    assert ok["success"]
    ok = _declare(ctx, scenario_id="k2", interaction={"action": "press", "key": "Enter"})
    assert ok["success"]
    ok = _declare(ctx, scenario_id="k3",
                  interaction={"action": "fill",
                               "fields": [{"label": "Note", "text": "typed"}],
                               "submit": "Commit"})
    assert ok["success"]
    bad = _declare(ctx, scenario_id="k4",
                   interaction={"action": "fill",
                                "fields": [{"label": "Note", "js": "x"}]})
    assert bad["success"] is False and "typed fields" in bad["error"]


def test_capture_oversized_artifact_refused_before_dispatch(browser):
    ctx, calls, router, browser_mp = browser
    root = pathlib.Path(ctx.home).parent
    root.joinpath("fixture.html").write_text(
        "<!doctype html><p>" + "x" * 70_000 + "</p>")
    declared = _declare(ctx)
    assert declared["success"]
    calls.clear()
    out = json.loads(ctx.tool("browser_capture", {"scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "byte bound" in out["error"]
    assert calls == []  # bounded refusal precedes any native dispatch


def test_capture_artifact_above_old_cap_accepted(browser, monkeypatch):
    # The 64 KiB local bound replaced the old 8 KiB cap: a 6 KiB payload
    # (over the old 8 KiB byte-to-base64 crossover, inside both the local
    # bound and the serialized-run guard) is certified normally.
    ctx, calls, router, browser_mp = browser
    root = pathlib.Path(ctx.home).parent
    root.joinpath("fixture.html").write_text(
        "<!doctype html><p>" + "x" * 6144 + "</p>")
    _approve(browser[3])
    assert _declare(ctx)["success"]
    router["run"] = lambda a: {"ok": True,
                               "result": qualified_run_output(
                                   "data:text/html;base64,TESTURL")}
    router["state"] = lambda a: {"ok": True, "result": ""}
    out = _capture(ctx)
    assert out["success"] and out["passed"] is True


# ------------------------------------------------------------- capture tests

def test_capture_passes_with_native_ledger_round_trip(browser):
    ctx, calls, router, browser_mp = browser
    _approve(browser[3])
    declared = _declare(ctx)
    router["run"] = lambda a: {"ok": True,
                               "result": qualified_run_output(
                                   "data:text/html;base64,TESTURL")}
    router["state"] = lambda a: (
        {"ok": True, "result": f"✓ closed tab {TAB} (was current; switched to {OTHER_TAB})"}
        if a.get("op") == "close-tab" else {"ok": True, "result": ""})
    out = json.loads(ctx.tool("browser_capture", {"scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] and out["passed"] is True
    assert out["native_recorded"] is True
    assert isinstance(out["evidence_id"], int)
    # artifact_digest is the TRANSPORTED bytes digest, raw digest recorded too
    raw = hashlib.sha256(
        pathlib.Path(ctx.home).parent.joinpath("fixture.html").read_bytes()).hexdigest()
    assert out["raw_artifact_digest"] == "sha256:" + raw
    assert out["source_revision"] == declared["source_revision"]
    assert out["evidence_binding"] == "bound"
    from agent.verification_evidence import verification_evidence_by_id
    res = verification_evidence_by_id(
        out["evidence_id"], session_id=SESSION, cwd=str(pathlib.Path(ctx.home).parent))
    assert res["status"] == "passed"
    row = res["evidence"]
    assert row["canonical_command"] == "external:browser-bladebro:browser:s1"
    assert row["source_revision"] == declared["source_revision"]
    assert row["artifact_digest"] == out["artifact_digest"]



def test_supplementary_role_snapshot_skipped_when_run_carries_it(browser):
    # The qualified run framing already contains the see(model) accessibility
    # snapshot; the plugin must not re-issue the role/label see call.
    ctx, calls, router, browser_mp = browser
    _approve(browser[3])
    _declare(ctx)
    router["run"] = lambda a: {"ok": True,
                               "result": qualified_run_output("data:u")}
    router["state"] = lambda a: {"ok": True, "result": ""}
    out = _capture(ctx)
    assert out["success"] and out["passed"] is True
    sees = [c for c in calls if c[0] == "see"]
    assert len(sees) == 1  # console_errors only
    assert sees[0][1] == {"logs": "console"}

def test_capture_transported_digest_matches_csp_bytes(browser):
    ctx, calls, router, browser_mp = browser
    _approve(browser[3])
    _declare(ctx)
    router["run"] = lambda a: {"ok": True,
                               "result": qualified_run_output("data:text/html;base64,U")}
    router["state"] = lambda a: {"ok": True, "result": ""}
    out = json.loads(ctx.tool("browser_capture", {"scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    import base64 as _b64
    # The run payload IS the CSP-prefixed artifact: decode step 0's url.
    run_call = [c for c in calls if c[0] == "run"][-1]
    open_step = run_call[1]["steps"][0]
    transported = _b64.b64decode(open_step["url"].split(",", 1)[1])
    raw = pathlib.Path(ctx.home).parent.joinpath("fixture.html").read_bytes()
    # Qualified wire shape: DOCTYPE stays first, CSP meta follows it, and the
    # remainder is the declared raw bytes verbatim (no doctype duplication).
    assert transported.startswith(b"<!doctype html>")
    assert transported.startswith(
        b"<!doctype html><meta http-equiv=\"Content-Security-Policy\"")
    assert transported.endswith(raw[len(b"<!doctype html>"):])
    assert out["artifact_digest"] == "sha256:" + hashlib.sha256(transported).hexdigest()


def test_capture_replay_off_and_single_run(browser):
    ctx, calls, router, browser_mp = browser
    _approve(browser[3])
    _declare(ctx)
    router["run"] = lambda a: {"ok": True, "result": qualified_run_output("data:u")}
    router["state"] = lambda a: {"ok": True, "result": ""}
    ctx.tool("browser_capture", {"scenario_id": "s1"},
             session_id=SESSION, task_id=TASK)
    assert all(k["replay_on_failure"] is False for _, _, k in calls)
    run_calls = [c for c in calls if c[0] == "run"]
    assert len(run_calls) == 1
    steps = run_calls[0][1]["steps"]
    assert steps[0]["op"] == "open-tab"
    assert steps[1]["condition"] == "js" and "location.href === " in steps[1]["text"]
    assert steps[2]["text"] == "State: committed"  # final predicate


def test_capture_guard_false_records_failed_row(browser):
    ctx, calls, router, browser_mp = browser
    _approve(browser[3])
    _declare(ctx)
    router["run"] = lambda a: {"ok": False, "error": false_guard_error("INITIAL")}
    router["state"] = lambda a: {"ok": True, "result": ""}
    out = json.loads(ctx.tool("browser_capture", {"scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "guard INITIAL false" in out["error"]
    assert "binding=failed" in out["error"]
    # Negative observations still mint a native row — failed stays failed.
    import re
    m = re.search(r"negative evidence row=(\d+)", out["error"])
    assert m, out["error"]
    from agent.verification_evidence import verification_evidence_by_id
    res = verification_evidence_by_id(
        int(m.group(1)), session_id=SESSION, cwd=str(pathlib.Path(ctx.home).parent))
    assert res["status"] == "failed"  # negative stays failed on the ledger
    status = json.loads(ctx.tool("browser_scenario",
                                 {"action": "status", "scenario_id": "s1"},
                                 session_id=SESSION, task_id=TASK))
    assert status["status"] == "failed"


def test_capture_ambiguous_error_never_replays(browser):
    ctx, calls, router, browser_mp = browser
    _declare(ctx)
    _approve(browser[3])
    router["run"] = lambda a: {"ok": False, "error": "connection closed mid-call"}
    out = json.loads(ctx.tool("browser_capture", {"scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "unbound, no replay" in out["error"]
    run_calls = [c for c in calls if c[0] == "run"]
    assert len(run_calls) == 1  # exactly one attempt, never replayed


def test_capture_unrecognized_framing_refused(browser):
    ctx, calls, router, browser_mp = browser
    _approve(browser[3])
    _declare(ctx)
    router["run"] = lambda a: {"ok": True, "result": "step 1: done, looking good"}
    router["state"] = lambda a: {"ok": True, "result": ""}
    out = json.loads(ctx.tool("browser_capture", {"scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "unknown" in out["error"]


def test_capture_truncated_framing_refused(browser):
    ctx, calls, router, browser_mp = browser
    _approve(browser[3])
    _declare(ctx)
    text = qualified_run_output("data:u")
    router["run"] = lambda a: {
        "ok": True, "result": text[:text.index("step 3: if(text")]}
    router["state"] = lambda a: {"ok": True, "result": ""}
    out = json.loads(ctx.tool("browser_capture", {"scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "unknown" in out["error"]


def test_capture_disabled_ledger_reports_unbound_truth(browser, monkeypatch):
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    _approve(browser[3])
    ctx, calls, router, browser_mp = browser
    _declare(ctx)
    router["run"] = lambda a: {"ok": True, "result": qualified_run_output("data:u")}
    router["state"] = lambda a: {"ok": True, "result": ""}
    out = json.loads(ctx.tool("browser_capture", {"scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    assert out["passed"] is True  # observation itself matched
    assert out["native_recorded"] is False
    assert out["evidence_id"] is None
    assert out["evidence_binding"].startswith("unbound")


def test_capture_stale_source_refused(browser):
    ctx, calls, router, browser_mp = browser
    _declare(ctx)
    pathlib.Path(ctx.home).parent.joinpath("fixture.html").write_text("body { color: blue }")
    calls.clear()
    out = json.loads(ctx.tool("browser_capture", {"scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "stale source evidence refused" in out["error"]
    assert calls == []  # no native dispatch against drifted sources


def test_capture_artifact_outside_workspace_refused(browser):
    ctx, _, _, browser_mp = browser
    declared = _declare(ctx)
    assert declared["success"]
    # Escape via symlink into a fresh scenario: point sources at /etc.
    root = pathlib.Path(ctx.home).parent
    (root / "escape.html").symlink_to("/etc/hostname")
    out = _declare(ctx, scenario_id="s9", sources=["escape.html"],
                   entry_artifact="escape.html")
    assert out["success"] is False and "outside the workspace" in out["error"]


def test_capture_foreign_session_refused(browser):
    ctx, calls, router, browser_mp = browser
    _declare(ctx)
    calls.clear()
    out = json.loads(ctx.tool("browser_capture", {"scenario_id": "s1"},
                              session_id="sess-EVIL", task_id=TASK))
    assert out["success"] is False and "different session" in out["error"]
    assert calls == []


# --------------------------------------------------------------- run tests

def test_ordinary_run_one_rpc_guard_wraps_steps(browser):
    ctx, calls, router, browser_mp = browser
    router["state"] = lambda a: {
        "ok": True, "connection_token": "tok-1",
        "result": f"✓ opened + switched to tab {TAB}\nPage: about:blank | phase: ready\n"}
    opened = json.loads(ctx.tool("browser_open", {"url": "data:text/html;base64,ORIG"},
                                 session_id=SESSION, task_id=TASK))
    calls.clear()
    router["run"] = lambda a: {"ok": True, "result": "step 1: ✓ switched\nstep 2: if → then\nstep 2.0: ok\n"}
    out = json.loads(ctx.tool("browser_run",
                              {"tab_id": opened["tab_id"],
                               "steps": [{"action": "see"}]},
                              session_id=SESSION, task_id=TASK))
    assert out["success"]
    assert len(calls) == 1 and calls[0][0] == "run"
    steps = calls[0][1]["steps"]
    assert steps[0]["op"] == "switch-tab" and steps[0]["target_id"] == TAB
    assert steps[1]["condition"] == "js"
    assert "location.href === " in steps[1]["text"]
    assert steps[1]["then"] == [{"action": "see"}]
    assert steps[1]["else"][0]["action"] == "eval"


def test_ordinary_run_guard_false_mutates_nothing(browser, monkeypatch):
    ctx, calls, router, browser_mp = browser
    router["state"] = lambda a: {
        "ok": True, "connection_token": "tok-1",
        "result": f"✓ opened + switched to tab {TAB}\nPage: about:blank | phase: ready\n"}
    opened = json.loads(ctx.tool("browser_open", {"url": "http://x.test"},
                                 session_id=SESSION, task_id=TASK))
    # Approval gate approves; guard then fails honestly.
    monkeypatch.setattr("tools.approval.request_tool_approval",
                        lambda *a, **k: {"approved": True, "message": None})
    router["run"] = lambda a: {"ok": False, "error": false_guard_error("TARGET")}
    out = json.loads(ctx.tool("browser_run",
                              {"tab_id": opened["tab_id"],
                               "steps": [{"action": "click", "text": "Commit"}]},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "guard false" in out["error"]
    closed = json.loads(ctx.tool("browser_close", {"tab_id": opened["tab_id"]},
                                 session_id=SESSION, task_id=TASK))
    assert closed["success"] is False  # binding voided after unknown outcome
    assert sum(1 for c in calls if c[0] == "run") == 1  # no replay


def test_ordinary_run_rejects_oversized_tree(browser):
    ctx, calls, router, browser_mp = browser
    router["state"] = lambda a: {
        "ok": True, "connection_token": "tok-1",
        "result": f"✓ opened + switched to tab {TAB}\n"}
    opened = json.loads(ctx.tool("browser_open", {"url": "http://x"},
                                 session_id=SESSION, task_id=TASK))
    calls.clear()
    half = 31
    branches = [{"action": "if", "condition": "text", "text": "t",
                 "then": [{"action": "wait", "ms": 1}] * half,
                 "else": [{"action": "wait", "ms": 1}] * half}]
    out = json.loads(ctx.tool("browser_run",
                              {"tab_id": opened["tab_id"], "steps": branches},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "aggregate" in out["error"]
    assert calls == []


def test_capture_requires_host_connection_token(browser):
    # Host mints no token on the run envelope: evidence cannot be certified.
    ctx, calls, router, browser_mp = browser
    _approve(browser[3])
    _declare(ctx)

    def no_token(a):
        return {"ok": True, "connection_token": None,
                "result": qualified_run_output("data:u")}

    router["run"] = no_token
    router["state"] = lambda a: {"ok": True, "result": ""}
    out = _capture(ctx)
    assert out["success"] is False and "connection token" in out["error"]


def test_capture_approval_denied_refuses_before_dispatch(browser, monkeypatch):
    ctx, calls, router, browser_mp = browser
    monkeypatch.setattr("tools.approval.request_tool_approval",
                        lambda *a, **k: {"approved": False, "message": "no"})
    _declare(ctx)
    calls.clear()
    out = _capture(ctx)
    assert out["success"] is False and "approval gate denied" in out["error"]
    assert calls == []  # denial precedes any native dispatch


def test_capture_approval_gate_unavailable_refuses(browser, monkeypatch):
    ctx, calls, router, browser_mp = browser
    import builtins
    # No approval gate reachable (ImportError): capture refuses, fail closed.
    real_import = builtins.__import__
    def blocked_import(name, *a, **k):
        if name == "tools.approval":
            raise ImportError("gate unavailable")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", blocked_import)
    _declare(ctx)
    calls.clear()
    out = _capture(ctx)
    assert out["success"] is False and "approval gate unavailable" in out["error"]
    assert calls == []


def test_capture_run_carries_final_target_guard(browser, monkeypatch):
    ctx, calls, router, browser_mp = browser
    _approve(monkeypatch)
    _declare(ctx)
    router["run"] = lambda a: {"ok": True, "result": qualified_run_output("data:u")}
    router["state"] = lambda a: {"ok": True, "result": ""}
    out = _capture(ctx)
    assert out["success"] and out["passed"] is True
    # steps: [0]=open-tab [1]=TARGET guard [2]=final text predicate; the
    # nested exact-URL guard sits inside the final branch (see framing).
    steps = [c for c in calls if c[0] == "run"][0][1]["steps"]
    final_branch = steps[2]["then"][0]
    assert final_branch["condition"] == "js"
    assert "location.href === " in final_branch["text"]
    assert final_branch["then"] == [{"action": "see", "mode": "model", "budget": 1200}]
    assert final_branch["else"][0]["action"] == "eval"




def test_capture_oversized_run_payload_refused_before_dispatch(browser, monkeypatch):
    # The 32 KiB serialized-run guard is defense-in-depth: it binds the exact
    # RPC dispatch even when the local artifact bound allows the file itself,
    # so its own bound is exercised by lowering the constant:
    ctx, calls, router, browser_mp = browser
    _approve(monkeypatch)
    _declare(ctx)
    mod = browser[0]
    monkeypatch.setattr(mod.module, "MAX_RUN_JSON_CHARS", 64)
    calls.clear()
    out = _capture(ctx)
    assert out["success"] is False and "JSON characters" in out["error"]
    assert calls == []


# --------------------------------------------------------------- build tests

def test_build_envelope_consumes_native_shape(browser, monkeypatch):
    """Native terminal envelope is {output, exit_code, error,
    verification_evidence?} — no success key. exit_code=0 with error present
    is NOT a completed foreground build."""
    ctx, _, router, browser_mp = browser
    (pathlib.Path(ctx.home).parent / "build.ts").write_text("// src")
    _declare(ctx, sources=["build.ts"], entry_artifact="dist.html",
             build_command="bun build ./build.ts --outfile dist.html")
    _approve(browser[3])  # native terminal owns the command approval
    monkeypatch.setattr(
        "model_tools.handle_function_call",
        lambda *a, **k: json.dumps({"output": "boom", "exit_code": 0,
                                    "error": "boom"}))
    out = json.loads(ctx.tool("browser_scenario",
                              {"action": "build", "scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "did not complete" in out["error"]




def test_build_artifact_digest_revalidated_before_mint(browser, monkeypatch):
    """Build snapshots the artifact digest; capture must refuse if the
    artifact bytes changed afterwards (stale compiled evidence)."""
    ctx, calls, router, browser_mp = browser
    root = pathlib.Path(ctx.home).parent
    (root / "build.ts").write_text("// src")
    _declare(ctx, sources=["build.ts"], entry_artifact="dist.html",
             build_command="bun run build")
    monkeypatch.setattr("model_tools.handle_function_call",
                        _fake_build_ok(root, cmd="bun run build"))
    _approve_build_ledger(browser[3], cmd="bun run build")
    out = json.loads(ctx.tool("browser_scenario",
                              {"action": "build", "scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is True and out["newly_generated"] is True
    calls.clear()
    # Artifact mutated after the native build row — capture must refuse.
    (root / "dist.html").write_text("<!doctype html><p>changed</p>")
    captured = _capture(ctx)
    assert captured["success"] is False and "native build snapshot" in captured["error"]
    assert calls == []  # refused before any transport/dispatch


def test_build_requires_native_evidence_row(browser, monkeypatch):
    ctx, _, router, browser_mp = browser
    (pathlib.Path(ctx.home).parent / "build.ts").write_text("// src")
    declared = _declare(ctx, sources=["build.ts"], entry_artifact="dist.html",
                        build_command="bun build ./build.ts --outfile dist.html")
    _approve(browser[3])  # build is mutating: approval precedes dispatch
    monkeypatch.setattr(
        "model_tools.handle_function_call",
        lambda *a, **k: json.dumps({"output": "", "exit_code": 0, "error": None}))
    out = json.loads(ctx.tool("browser_scenario",
                              {"action": "build", "scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "no native evidence row" in out["error"]


def test_build_refuses_foreground_failure(browser, monkeypatch):
    ctx, _, router, browser_mp = browser
    (pathlib.Path(ctx.home).parent / "build.ts").write_text("// src")
    _declare(ctx, sources=["build.ts"], entry_artifact="dist.html",
             build_command="bun build ./build.ts --outfile dist.html")
    monkeypatch.setattr(
        "model_tools.handle_function_call",
        lambda *a, **k: json.dumps({"output": "", "exit_code": 1,
                                    "error": "background not allowed"}))
    out = json.loads(ctx.tool("browser_scenario",
                              {"action": "build", "scenario_id": "s1"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "foreground" in out["error"]


def test_build_invokes_gateway_with_exact_identity(browser, monkeypatch):
    ctx, _, router, browser_mp = browser
    root = pathlib.Path(ctx.home).parent
    (root / "build.ts").write_text("// src")
    _declare(ctx, sources=["build.ts"], entry_artifact="dist.html",
             build_command="bun build ./build.ts --outfile dist.html")
    seen = {}

    def fake_hfc(function_name, function_args, task_id=None, session_id=None, **_kw):
        seen.update({"tool": function_name, "args": dict(function_args),
                     "task_id": task_id, "session_id": session_id})
        return json.dumps({"output": "", "exit_code": 0, "error": None})

    monkeypatch.setattr("model_tools.handle_function_call", fake_hfc)
    ctx.tool("browser_scenario", {"action": "build", "scenario_id": "s1"},
             session_id=SESSION, task_id=TASK)
    assert seen["tool"] == "terminal"
    assert seen["args"]["background"] is False
    assert seen["args"]["workdir"] == str(root)
    assert seen["task_id"] == TASK and seen["session_id"] == SESSION


# --------------------------------------------------------------- tab tests

def test_open_requires_host_connection_token(browser):
    ctx, calls, router, browser_mp = browser
    router["state"] = lambda a: {
        "ok": True, "connection_token": None,  # host mints none: refuse
        "result": f"✓ opened + switched to tab {TAB}\n"}
    out = json.loads(ctx.tool("browser_open", {"url": "http://x"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "connection token" in out["error"]


def test_open_tracks_native_tab_identity_and_token(browser):
    ctx, calls, router, browser_mp = browser
    router["state"] = lambda a: {
        "ok": True, "connection_token": "tok-1",
        "result": f"✓ opened + switched to tab {TAB}\n"}
    opened = json.loads(ctx.tool("browser_open", {"url": "http://x"},
                                 session_id=SESSION, task_id=TASK))
    assert opened["success"] and opened["tab_id"] == TAB
    assert router["state"]  # token present in env


def test_connection_token_change_invalidates_binding(browser):
    ctx, calls, router, browser_mp = browser

    def state_handler(a):
        if a.get("op") == "open-tab":
            return {"ok": True, "connection_token": "tok-1",
                    "result": f"✓ opened + switched to tab {TAB}\n"}
        return {"ok": True, "connection_token": "tok-2",
                "result": f"✓ switched to tab {TAB}\n"}
    router["state"] = state_handler
    opened = json.loads(ctx.tool("browser_open", {"url": "http://x"},
                                 session_id=SESSION, task_id=TASK))
    out = json.loads(ctx.tool("browser_screenshot", {"tab_id": opened["tab_id"]},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "connection changed" in out["error"]
    # binding gone: subsequent ops refuse without dispatching
    calls.clear()
    again = json.loads(ctx.tool("browser_screenshot", {"tab_id": opened["tab_id"]},
                                session_id=SESSION, task_id=TASK))
    assert again["success"] is False and "not owned" in again["error"]
    assert calls == []


def test_tab_id_unparseable_refuses_ownership(browser):
    ctx, calls, router, browser_mp = browser
    router["state"] = lambda a: {"ok": True, "result": "opened, tab somewhere"}
    out = json.loads(ctx.tool("browser_open", {"url": "http://x"},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "tab id" in out["error"]
    assert calls[0][2]["replay_on_failure"] is False


# ------------------------------------------------------- lifecycle cleanup

def _open_state_router(tab=TAB, token="tok-1"):
    def state_handler(a):
        if a.get("op") == "open-tab":
            return {"ok": True, "connection_token": token,
                    "result": f"✓ opened + switched to tab {tab}\n"}
        return {"ok": True, "connection_token": token, "result": ""}
    return state_handler


def _open(ctx, session=SESSION, tab=TAB):
    return json.loads(ctx.tool("browser_open", {"url": "http://x"},
                               session_id=session, task_id=TASK))


CLOSED_TAB = ("state", {"op": "close-tab", "target_id": TAB},
              {"replay_on_failure": False})


def test_lifecycle_hooks_registered(browser):
    ctx, _, _, _ = browser
    from hermes_cli.plugins import VALID_HOOKS
    for name in ("on_session_finalize", "on_session_reset"):
        assert name in VALID_HOOKS
        ours = [cb for cb in ctx.manager.iter_hook_callbacks(name)
                if cb.__module__ == ctx.module.__name__]
        assert ours, f"{name} not registered by browser-bladebro"
    # on_session_end fires per TURN: registering cleanup there would reap
    # tabs between turns; the plugin must stay silent on it.
    ours = [cb for cb in ctx.manager.iter_hook_callbacks("on_session_end")
            if cb.__module__ == ctx.module.__name__]
    assert ours == []


def test_session_end_survives_turn_boundary(browser):
    # on_session_end fires at the end of EVERY turn (turn_finalizer/cli);
    # the plugin must not reap tabs or state there — multi-turn browsing
    # and scenario continuity depend on the tab surviving per-turn fires.
    ctx, calls, router, _ = browser
    router["state"] = _open_state_router()
    assert _open(ctx)["success"]
    calls.clear()
    ctx.hook("on_session_end", session_id=SESSION, task_id=TASK, turn_id=1,
             completed=True, failed=False, interrupted=False,
             turn_exit_reason="model_stop")
    assert calls == []  # zero native dispatch on a per-turn boundary
    # tab binding intact and fully operable after the turn
    out = json.loads(ctx.tool("browser_close", {"tab_id": TAB},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] and out["closed"] is True


def test_finalize_reaps_tab_after_turn_survival(browser):
    # The same tab that survives per-turn on_session_end fires is closed at
    # the true session boundary.
    ctx, calls, router, _ = browser
    router["state"] = _open_state_router()
    assert _open(ctx)["success"]
    ctx.hook("on_session_end", session_id=SESSION, task_id=TASK, turn_id=1,
             completed=True, failed=False, interrupted=False,
             turn_exit_reason="model_stop")
    calls.clear()
    ctx.hook("on_session_finalize", session_id=SESSION, reason="cli_exit")
    assert CLOSED_TAB in calls
    calls.clear()
    out = json.loads(ctx.tool("browser_close", {"tab_id": TAB},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "not owned" in out["error"]
    assert calls == []


def test_finalize_and_reset_close_owned_tabs(browser):
    ctx, calls, router, _ = browser
    router["state"] = _open_state_router()
    assert _open(ctx)["success"]
    calls.clear()
    ctx.hook("on_session_finalize", session_id=SESSION, reason="end")
    assert CLOSED_TAB in calls
    # session state empty; a fresh open then a reset boundary closes again
    assert _open(ctx)["success"]
    calls.clear()
    ctx.hook("on_session_reset", session_id=SESSION, reason="reset")
    assert CLOSED_TAB in calls


def test_finalize_clears_scenario_records_for_session(browser):
    ctx, _, _, _ = browser
    assert _declare(ctx)["success"]
    again = _declare(ctx)
    assert again["success"] is False and "already declared" in again["error"]
    ctx.hook("on_session_finalize", session_id=SESSION, reason="end")
    assert _declare(ctx)["success"]  # record tombstoned; id declarable again


def test_cleanup_never_touches_foreign_session_tabs(browser):
    ctx, calls, router, _ = browser
    seq = {"n": 0}

    def state_handler(a):
        if a.get("op") == "open-tab":
            seq["n"] += 1
            tab = TAB if seq["n"] == 1 else OTHER_TAB
            return {"ok": True, "connection_token": "tok-1",
                    "result": f"✓ opened + switched to tab {tab}\n"}
        return {"ok": True, "connection_token": "tok-1", "result": ""}
    router["state"] = state_handler
    assert _open(ctx, session=SESSION)["tab_id"] == TAB
    assert _open(ctx, session="sess-2")["tab_id"] == OTHER_TAB
    calls.clear()
    ctx.hook("on_session_finalize", session_id=SESSION, reason="cli_exit")
    closed = [c[1]["target_id"] for c in calls
              if c[1].get("op") == "close-tab"]
    assert closed == [TAB]  # exactly the one owned tab
    # foreign binding intact and operable by its owner
    out = json.loads(ctx.tool("browser_close", {"tab_id": OTHER_TAB},
                              session_id="sess-2", task_id=TASK))
    assert out["success"] and out["closed"] is True


def test_cleanup_records_error_without_retry_and_still_clears(browser, caplog):
    ctx, calls, router, _ = browser

    def state_handler(a):
        if a.get("op") == "open-tab":
            return {"ok": True, "connection_token": "tok-1",
                    "result": f"✓ opened + switched to tab {TAB}\n"}
        if a.get("op") == "close-tab":
            return {"ok": False, "error": "host refused: tab pinned"}
        return {"ok": True, "connection_token": "tok-1", "result": ""}
    router["state"] = state_handler
    assert _open(ctx)["success"]
    calls.clear()
    with caplog.at_level("WARNING"):
        ctx.hook("on_session_finalize", session_id=SESSION, reason="cli_exit")
    close_ops = [c for c in calls if c[1].get("op") == "close-tab"]
    assert len(close_ops) == 1  # one honest attempt, never retried or replayed
    assert "outcome=error" in caplog.text and "tab pinned" in caplog.text
    out = json.loads(ctx.tool("browser_close", {"tab_id": TAB},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "not owned" in out["error"]  # still cleared


def test_cleanup_records_unknown_outcome_without_retry(browser, caplog):
    ctx, calls, router, _ = browser

    def state_handler(a):
        if a.get("op") == "open-tab":
            return {"ok": True, "connection_token": "tok-1",
                    "result": f"✓ opened + switched to tab {TAB}\n"}
        if a.get("op") == "close-tab":
            raise RuntimeError("transport severed mid-call")
        return {"ok": True, "connection_token": "tok-1", "result": ""}
    router["state"] = state_handler
    assert _open(ctx)["success"]
    calls.clear()
    with caplog.at_level("WARNING"):
        ctx.hook("on_session_finalize", session_id=SESSION, reason="end")
    close_ops = [c for c in calls if c[1].get("op") == "close-tab"]
    assert len(close_ops) == 1  # unknown outcome recorded, never retried
    assert "outcome=unknown" in caplog.text and "transport severed" in caplog.text
    out = json.loads(ctx.tool("browser_close", {"tab_id": TAB},
                              session_id=SESSION, task_id=TASK))
    assert out["success"] is False and "not owned" in out["error"]


def test_fresh_session_starts_empty_after_cleanup(browser):
    ctx, calls, router, _ = browser
    router["state"] = _open_state_router()
    assert _open(ctx)["success"]
    assert _declare(ctx)["success"]
    ctx.hook("on_session_reset", session_id=SESSION, reason="reset")
    fresh = json.loads(ctx.tool(
        "browser_open", {"url": "http://y"},
        session_id="sess-2", task_id=TASK))
    assert fresh["success"] and fresh["tab_id"] == TAB
    fresh_scenario = json.loads(ctx.tool(
        "browser_scenario",
        {"action": "declare", "scenario_id": "s1", "sources": ["fixture.html"],
         "entry_artifact": "fixture.html",
         "initial_predicate": {"text": "State: pending"},
         "final_predicate": {"text": "State: committed"},
         "interaction": {"action": "click", "text": "Commit"}},
        session_id="sess-2", task_id=TASK))
    assert fresh_scenario["success"]  # stale id freed, new session owns it
