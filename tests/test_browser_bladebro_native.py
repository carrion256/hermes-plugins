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
import base64
import hashlib
import json
import pathlib
import urllib.parse
from types import SimpleNamespace

import pytest

from hermes_cli.plugins import PluginContext
# Imported at collection so the built-in browser_* entries exist in every
# test's registry snapshot (the plugin overrides them; unload restores them).
from tools.browser_tool import BROWSER_TOOL_SCHEMAS


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


# ------------------------------------------------- drop-in backend double

def fake_backend(router, tabs=(TAB, OTHER_TAB)):
    """Stateful bladebro double speaking the real wire shapes (probed live):
    open-tab hands out ids in order; ``run`` executes switch-tab + steps and
    frames each top-level step as ``step N: <body>``; a vanished tab fails
    step 1 with ``tab not found``; ``location.href`` is the normalized URL."""
    backend = SimpleNamespace(pool=list(tabs), live=set(), url={}, current=[None],
                              state_override={}, bodies={})

    def page(tab):
        url = backend.url.get(tab, "about:blank")
        return f"Page: {url} | phase: ready | 3 actionable\ntitle: Fixture\ne1 button \"Commit\""

    def state(a):
        op = a.get("op")
        if op in backend.state_override:
            return backend.state_override[op](a)
        if op == "open-tab":
            tab = backend.pool.pop(0)
            backend.live.add(tab)
            backend.current[0] = tab
            return {"ok": True, "result": f"✓ opened + switched to tab {tab}\n{page(tab)}"}
        if op == "close-tab":
            backend.live.discard(a["target_id"])
            return {"ok": True, "result": f"✓ closed tab {a['target_id']}"}
        if op == "switch-tab":
            if a["target_id"] not in backend.live:
                return {"ok": False, "error": f"tab not found: {a['target_id']}"}
            backend.current[0] = a["target_id"]
            return {"ok": True, "result": f"✓ switched to tab {a['target_id']}"}
        return {"ok": True, "result": ""}

    def body(step):
        tab = backend.current[0]
        action = step.get("action")
        if action in backend.bodies:
            return backend.bodies[action](step)
        if action == "navigate":
            parts = urllib.parse.urlsplit(step["url"])
            backend.url[tab] = urllib.parse.urlunsplit(parts._replace(path=parts.path or "/"))
            return f"navigated → {parts.netloc}\n{page(tab)}"
        if action == "eval" and step["js"] == "location.href":
            return f"js → result: {json.dumps(backend.url.get(tab, 'about:blank'))}"
        if action == "if" and step.get("condition") == "js":
            want = json.loads(step["text"].split("=== ", 1)[1])
            if want != backend.url.get(tab):
                raise _GuardFalse
            return 'if(js "...") → then\nstep 2.0: outcome: ok'
        if action == "see":
            return f"see (60 chars):\n{page(tab)}"
        return f"outcome: dom-changed via mouse\n{page(tab)}"

    def run(a):
        steps = a["steps"]
        lead = steps[0]
        if lead.get("op") == "switch-tab" and lead["target_id"] not in backend.live:
            return {"ok": False, "error": (
                f"✗ error: step 1 failed: step 1 state failed: tab not found: "
                f"{lead['target_id']}\n\n--- current page state ---\n")}
        out = []
        for i, step in enumerate(steps, 1):
            if step.get("action") == "state":
                out.append(f"step {i}: {state(step)['result']}")
                continue
            try:
                out.append(f"step {i}: {body(step)}")
            except _GuardFalse:
                return {"ok": False, "error": false_guard_error("TARGET")}
        return {"ok": True, "result": "\n\n".join(out)}

    router["state"] = state
    router["run"] = run
    return backend


class _GuardFalse(Exception):
    pass


def _call(ctx, name, args=None, session=SESSION, task=TASK):
    raw = ctx.tool(name, args or {}, session_id=session, task_id=task)
    return raw if isinstance(raw, dict) else json.loads(raw)


def _runs(calls):
    return [c[1]["steps"] for c in calls if c[0] == "run"]


# --------------------------------------------------------------- run tests

def test_run_guard_binds_to_the_pages_actual_url(browser):
    # Regression: the guard used the REQUESTED url ("http://x.test"), but the
    # page reports the normalized "http://x.test/", so every batch failed.
    ctx, calls, router, _ = browser
    fake_backend(router)
    assert _call(ctx, "browser_navigate", {"url": "http://x.test"})["success"]
    calls.clear()
    out = _call(ctx, "browser_run", {"steps": [{"action": "see"}]})
    assert out["success"] and out["target"] == "http://x.test/"
    guarded = _runs(calls)[-1]
    assert guarded[0] == {"action": "state", "op": "switch-tab", "target_id": TAB}
    assert guarded[1]["text"] == 'location.href === "http://x.test/"'
    assert guarded[1]["then"] == [{"action": "see"}]


def test_run_guard_false_mutates_nothing_and_keeps_the_tab(browser, monkeypatch):
    ctx, calls, router, _ = browser
    backend = fake_backend(router)
    assert _call(ctx, "browser_navigate", {"url": "http://x.test"})["success"]
    monkeypatch.setattr("tools.approval.request_tool_approval",
                        lambda *a, **k: {"approved": True, "message": None})
    real_run = router["run"]

    def page_moves_after_approval(a):
        if a["steps"][1].get("action") == "if":
            backend.url[TAB] = "http://elsewhere.test/"
        return real_run(a)
    router["run"] = page_moves_after_approval
    calls.clear()
    out = _call(ctx, "browser_run", {"steps": [{"action": "click", "text": "Commit"}]})
    assert out["success"] is False and "guard false" in out["error"]
    assert len([s for s in _runs(calls) if s[1].get("action") == "if"]) == 1  # no replay
    assert _call(ctx, "browser_snapshot")["success"]  # binding survives


def test_run_rejects_oversized_tree_before_dispatch(browser):
    ctx, calls, router, _ = browser
    fake_backend(router)
    assert _call(ctx, "browser_navigate", {"url": "http://x.test"})["success"]
    calls.clear()
    half = 31
    branches = [{"action": "if", "condition": "text", "text": "t",
                 "then": [{"action": "wait", "ms": 1}] * half,
                 "else": [{"action": "wait", "ms": 1}] * half}]
    out = _call(ctx, "browser_run", {"steps": branches})
    assert out["success"] is False and "aggregate" in out["error"]
    assert calls == []


# ------------------------------------------------------------ drop-in tests

def test_dropin_replaces_builtins_and_unload_restores_them(browser):
    ctx, _, _, _ = browser
    from tools.registry import registry
    for schema in BROWSER_TOOL_SCHEMAS:
        entry = registry.get_entry(schema["name"])
        assert entry.toolset == "browser" and entry.schema == schema
        assert entry.handler.__module__ == ctx.module.__name__
    ctx.manager.unload("browser-bladebro")
    for schema in BROWSER_TOOL_SCHEMAS:
        assert registry.get_entry(schema["name"]).handler.__module__ != ctx.module.__name__


def test_without_override_grant_builtins_stay_and_extras_load(load_plugin, fake_mcp, monkeypatch):
    from hermes_cli.plugins import PluginContext
    from tools.registry import registry
    monkeypatch.setattr(PluginContext, "_tool_override_allowed", lambda self, name: False)
    ctx = load_plugin("browser-bladebro")
    assert registry.get_entry("browser_navigate").handler.__module__ != ctx.module.__name__
    assert registry.get_entry("browser_run").handler.__module__ == ctx.module.__name__


def test_each_task_gets_one_reused_tab(browser):
    ctx, calls, router, _ = browser
    fake_backend(router)
    for _ in range(2):
        assert _call(ctx, "browser_navigate", {"url": "http://x.test"})["success"]
    other = _call(ctx, "browser_navigate", {"url": "http://y.test"}, task="task-2")
    assert other["success"] and other["url"] == "http://y.test/"
    opens = [c for c in calls if c[1].get("op") == "open-tab"]
    assert len(opens) == 2  # one per task, never per call
    switched = [steps[0]["target_id"] for steps in _runs(calls)]
    assert switched == [TAB, TAB, OTHER_TAB]


def test_navigate_reopens_when_backend_lost_the_tab(browser):
    ctx, calls, router, _ = browser
    backend = fake_backend(router)
    assert _call(ctx, "browser_navigate", {"url": "http://x.test"})["success"]
    backend.live.clear()  # backend restarted
    calls.clear()
    out = _call(ctx, "browser_navigate", {"url": "http://x.test/next"})
    assert out["success"] and out["url"] == "http://x.test/next"
    navigations = [s for s in _runs(calls) if s[1].get("action") == "navigate"]
    assert [s[0]["target_id"] for s in navigations] == [TAB, OTHER_TAB]  # first failed at switch


def test_navigate_result_matches_builtin_shape(browser):
    ctx, _, router, _ = browser
    fake_backend(router)
    out = _call(ctx, "browser_navigate", {"url": "http://x.test"})
    assert out == {"success": True, "url": "http://x.test/", "title": "Fixture",
                   "snapshot": out["snapshot"], "element_count": 3}
    assert 'e1 button "Commit"' in out["snapshot"]


def test_navigate_refuses_secret_bearing_url_before_dispatch(browser):
    ctx, calls, router, _ = browser
    fake_backend(router)
    out = _call(ctx, "browser_navigate",
                {"url": "https://x.test/?k=ghp_" + "a" * 36})
    assert out["success"] is False and "API key or token" in out["error"]
    assert calls == []


def test_actions_need_a_navigated_tab(browser):
    ctx, calls, _, _ = browser
    for name, args in (("browser_snapshot", {}), ("browser_click", {"ref": "@e1"}),
                       ("browser_console", {}), ("browser_vision", {"question": "q"})):
        out = _call(ctx, name, args)
        assert out["success"] is False and "browser_navigate" in out["error"]
    assert calls == []


def test_builtin_refs_reach_the_backend_in_native_form(browser):
    # bladebro rejects "@e5" as a stale ref; the built-in schema teaches "@e5"
    # and annotate labels "[5]".
    ctx, calls, router, _ = browser
    fake_backend(router)
    _call(ctx, "browser_navigate", {"url": "http://x.test"})
    for ref in ("@e5", "e5", "5", "[5]"):
        out = _call(ctx, "browser_click", {"ref": ref})
        assert out["success"] and out["clicked"] == "@e5"
        assert _runs(calls)[-1][1] == {"action": "click", "ref": "e5"}


def test_backend_step_errors_surface_without_framing(browser):
    ctx, _, router, _ = browser
    backend = fake_backend(router)
    _call(ctx, "browser_navigate", {"url": "http://x.test"})
    real_run = router["run"]

    def stale(a):
        if a["steps"][1].get("action") == "click":
            return {"ok": False, "error": "✗ error: step 2 failed: step 2 failed: stale ref: e9"}
        return real_run(a)
    router["run"] = stale
    out = _call(ctx, "browser_click", {"ref": "e9"})
    assert out == {"success": False, "error": "stale ref: e9"}
    assert TAB in backend.live


def test_console_splits_exceptions_and_clear_hides_seen_messages(browser):
    ctx, _, router, _ = browser
    backend = fake_backend(router)
    _call(ctx, "browser_navigate", {"url": "http://x.test"})
    log = ["error: boom", "exception: Uncaught Error: bad\n    at f (a.js:1)"]
    backend.bodies["see"] = lambda step: (
        f"see (90 chars):\nconsole ({len(log)} entries):\n" + "\n".join(log))
    first = _call(ctx, "browser_console", {"clear": True})
    assert first["console_messages"] == [{"type": "error", "text": "boom", "source": "console"}]
    assert first["js_errors"] == [{"message": "Uncaught Error: bad\n    at f (a.js:1)",
                                   "source": "exception"}]
    log.append("error: boom")  # same text again after the clear
    second = _call(ctx, "browser_console")
    assert second["total_messages"] == 1 and second["total_errors"] == 0
    backend.bodies["see"] = lambda step: "see (16 chars):\nconsole: (empty)"
    assert _call(ctx, "browser_console")["total_messages"] == 0


def test_console_expression_returns_parsed_value(browser):
    ctx, calls, router, _ = browser
    backend = fake_backend(router)
    _call(ctx, "browser_navigate", {"url": "http://x.test"})
    backend.bodies["eval"] = lambda step: 'js → result: {"n": 2}'
    out = _call(ctx, "browser_console", {"expression": "({n: 2})"})
    assert out == {"success": True, "result": {"n": 2}, "result_type": "dict"}


def test_vision_attaches_the_backend_screenshot_natively(browser, tmp_path, monkeypatch):
    ctx, calls, router, _ = browser
    fake_backend(router)
    _call(ctx, "browser_navigate", {"url": "http://x.test"})
    png = tmp_path / "shot.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="))
    router["vision"] = lambda a: {"ok": True, "result": f"MEDIA:{png}"}
    monkeypatch.setattr("tools.vision_tools._should_use_native_vision_fast_path", lambda: True)
    calls.clear()
    out = _call(ctx, "browser_vision", {"question": "layout?", "annotate": True})
    assert out["_multimodal"] is True and out["meta"]["screenshot_path"] == str(png)
    assert [c[0] for c in calls] == ["state", "vision"]
    assert calls[0][1] == {"op": "switch-tab", "target_id": TAB}
    assert calls[1][1] == {"marks": True}


def test_guard_active_private_page_refuses_content_and_input(browser, monkeypatch):
    # Built-in parity: with the SSRF guard active in this browser mode, a page
    # that moved itself to an internal address (JS location=, meta refresh, an
    # eval) yields no content and takes no input.
    from tools import browser_tool as _bt
    ctx, calls, router, _ = browser
    backend = fake_backend(router)
    assert _call(ctx, "browser_navigate", {"url": "http://x.test"})["success"]
    monkeypatch.setattr(_bt._eval_policy, "_eval_ssrf_guard_active", lambda task_id: False)
    assert _call(ctx, "browser_snapshot", {})["success"]  # control: guard inactive

    monkeypatch.setattr(_bt._eval_policy, "_eval_ssrf_guard_active", lambda task_id: True)
    backend.url[TAB] = "http://169.254.169.254/latest/meta-data"
    calls.clear()
    paths = (
        ("browser_snapshot", {}), ("browser_get_images", {}), ("browser_console", {}),
        ("browser_console", {"expression": "location.href"}),
        ("browser_vision", {"question": "q"}),
        ("browser_click", {"ref": "e1"}), ("browser_type", {"ref": "e1", "text": "x"}),
        ("browser_press", {"key": "Enter"}),
        ("browser_run", {"steps": [{"action": "see"}]}),
    )
    for name, args in paths:
        out = _call(ctx, name, args)
        assert out["success"] is False, (name, out)
        assert "private or internal address" in out["error"], (name, out)
    # Nothing the page could act on was dispatched: no click/type/press left.
    assert not [steps for steps in _runs(calls)
                if any(step.get("action") in {"click", "type", "press"} for step in steps)]


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


# ------------------------------------------------------- lifecycle cleanup

def _open(ctx, router, session=SESSION, task=TASK):
    if "run" not in router:
        fake_backend(router)
    return json.loads(ctx.tool("browser_navigate", {"url": "http://x.test"},
                               session_id=session, task_id=task))


def _snapshot(ctx, session=SESSION, task=TASK):
    return json.loads(ctx.tool("browser_snapshot", {},
                               session_id=session, task_id=task))


CLOSED_TAB = ("state", {"op": "close-tab", "target_id": TAB},
              {"replay_on_failure": False})


def test_lifecycle_hooks_registered(browser):
    ctx, _, _, _ = browser
    from hermes_cli.plugins import VALID_HOOKS
    for name in ("on_session_finalize", "on_session_reset", "subagent_stop"):
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
    # the plugin must not reap tabs there — multi-turn browsing depends on it.
    ctx, calls, router, _ = browser
    assert _open(ctx, router)["success"]
    calls.clear()
    ctx.hook("on_session_end", session_id=SESSION, task_id=TASK, turn_id=1,
             completed=True, failed=False, interrupted=False,
             turn_exit_reason="model_stop")
    assert calls == []  # zero native dispatch on a per-turn boundary
    assert _snapshot(ctx)["success"]  # same tab, next turn
    assert calls[-1][1]["steps"][0]["target_id"] == TAB


def test_finalize_reaps_tab_after_turn_survival(browser):
    ctx, calls, router, _ = browser
    assert _open(ctx, router)["success"]
    ctx.hook("on_session_end", session_id=SESSION, task_id=TASK, turn_id=1,
             completed=True, failed=False, interrupted=False,
             turn_exit_reason="model_stop")
    calls.clear()
    ctx.hook("on_session_finalize", session_id=SESSION, reason="cli_exit")
    assert CLOSED_TAB in calls
    calls.clear()
    out = _snapshot(ctx)
    assert out["success"] is False and "browser_navigate" in out["error"]
    assert calls == []


def test_finalize_and_reset_close_owned_tabs(browser):
    ctx, calls, router, _ = browser
    backend = fake_backend(router, tabs=(TAB, TAB))
    assert _open(ctx, router)["success"]
    calls.clear()
    ctx.hook("on_session_finalize", session_id=SESSION, reason="end")
    assert CLOSED_TAB in calls and TAB not in backend.live
    # session state empty; a fresh navigate then a reset boundary closes again
    assert _open(ctx, router)["success"]
    calls.clear()
    ctx.hook("on_session_reset", session_id=SESSION, reason="reset")
    assert CLOSED_TAB in calls


def test_subagent_stop_closes_only_the_childs_tabs(browser):
    ctx, calls, router, _ = browser
    fake_backend(router)
    assert _open(ctx, router, session=SESSION)["success"]
    assert _open(ctx, router, session="child-1", task="child-task")["success"]
    calls.clear()
    ctx.hook("subagent_stop", parent_session_id=SESSION, child_session_id="child-1",
             child_status="completed")
    closed = [c[1]["target_id"] for c in calls if c[1].get("op") == "close-tab"]
    assert closed == [OTHER_TAB]
    assert _snapshot(ctx)["success"]  # the parent keeps browsing


def test_finalize_clears_scenario_records_for_session(browser):
    ctx, _, _, _ = browser
    assert _declare(ctx)["success"]
    again = _declare(ctx)
    assert again["success"] is False and "already declared" in again["error"]
    ctx.hook("on_session_finalize", session_id=SESSION, reason="end")
    assert _declare(ctx)["success"]  # record tombstoned; id declarable again


def test_cleanup_never_touches_foreign_session_tabs(browser):
    ctx, calls, router, _ = browser
    fake_backend(router)
    assert _open(ctx, router, session=SESSION)["success"]
    assert _open(ctx, router, session="sess-2")["success"]
    calls.clear()
    ctx.hook("on_session_finalize", session_id=SESSION, reason="cli_exit")
    closed = [c[1]["target_id"] for c in calls if c[1].get("op") == "close-tab"]
    assert closed == [TAB]  # exactly the one owned tab
    out = _snapshot(ctx, session="sess-2")
    assert out["success"]  # foreign tab intact and operable by its owner
    assert calls[-1][1]["steps"][0]["target_id"] == OTHER_TAB


def test_cleanup_records_error_without_retry_and_still_clears(browser, caplog):
    ctx, calls, router, _ = browser
    backend = fake_backend(router)
    assert _open(ctx, router)["success"]
    backend.state_override["close-tab"] = lambda a: {
        "ok": False, "error": "host refused: tab pinned"}
    calls.clear()
    with caplog.at_level("WARNING"):
        ctx.hook("on_session_finalize", session_id=SESSION, reason="cli_exit")
    close_ops = [c for c in calls if c[1].get("op") == "close-tab"]
    assert len(close_ops) == 1  # one honest attempt, never retried or replayed
    assert "outcome=error" in caplog.text and "tab pinned" in caplog.text
    assert "browser_navigate" in _snapshot(ctx)["error"]  # still cleared


def test_cleanup_records_unknown_outcome_without_retry(browser, caplog):
    ctx, calls, router, _ = browser
    backend = fake_backend(router)
    assert _open(ctx, router)["success"]

    def severed(a):
        raise RuntimeError("transport severed mid-call")
    backend.state_override["close-tab"] = severed
    calls.clear()
    with caplog.at_level("WARNING"):
        ctx.hook("on_session_finalize", session_id=SESSION, reason="end")
    close_ops = [c for c in calls if c[1].get("op") == "close-tab"]
    assert len(close_ops) == 1  # unknown outcome recorded, never retried
    assert "outcome=unknown" in caplog.text and "transport severed" in caplog.text
    assert "browser_navigate" in _snapshot(ctx)["error"]


def test_fresh_session_starts_empty_after_cleanup(browser):
    ctx, calls, router, _ = browser
    fake_backend(router, tabs=(TAB, TAB))
    assert _open(ctx, router)["success"]
    assert _declare(ctx)["success"]
    ctx.hook("on_session_reset", session_id=SESSION, reason="reset")
    assert _open(ctx, router, session="sess-2")["success"]
    fresh_scenario = json.loads(ctx.tool(
        "browser_scenario",
        {"action": "declare", "scenario_id": "s1", "sources": ["fixture.html"],
         "entry_artifact": "fixture.html",
         "initial_predicate": {"text": "State: pending"},
         "final_predicate": {"text": "State: committed"},
         "interaction": {"action": "click", "text": "Commit"}},
        session_id="sess-2", task_id=TASK))
    assert fresh_scenario["success"]  # stale id freed, new session owns it
