"""bladebro browser acceptance over the native Hermes MCP client.

CLI transport retired: browser calls go through the host's native MCP path
(``ctx.call_mcp``), gated by the operator-granted
``plugins.entries.browser-bladebro.mcp_allowlist`` (default deny).

Certified evidence uses the direct-artifact protocol (qualified wire shape):
scenario declare pins immutable source hashes + a workspace-confined HTML
artifact; capture constructs the navigation payload in trusted Python
(qualified CSP prefix + declared artifact bytes), and runs open-tab ->
plugin exact-URL guard -> initial text predicate -> declared interaction ->
final text predicate in ONE native run RPC. Every false branch is a
plugin-generated throw, so any branch that does not hold aborts the run with a
distinguishable marker — there is no success-by-omission. Provenance comes
from the native backend's step framing, never from page JavaScript asserting
its own pass or from caller metadata. Arbitrary HTTP/file tabs remain ordinary
usable browsing but can NEVER mint certifiable source+build proof.

Limits (documented, not claimed): this proves the browser loaded
producer-hashed bytes behind the CSP prefix and the backend observed the
declared text/interaction flow; it does not attest reproducible builds,
undeclared toolchain inputs, or hostile-host races, and native text conditions
cover page text only (selector/role predicates are refused at declare as not
natively observable). The build mode is qualified only for genuine single-file
compiler outputs; multi-file bundles and dependency graphs remain unverified.
HTTP deployments and origin-dependent applications remain unsupported/unbound.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import stat
import threading
import time

logger = logging.getLogger(__name__)

TAB_ID_RE_HINT = "opened + switched to tab <32 hex>"

CONTROL_ACTIONS = frozenset({"if", "while"})
READ_ONLY_ACTIONS = frozenset({"see", "read", "wait", "tabs", "cookies", "ls", "ss"})
KNOWN_ACTIONS = frozenset({
    "click", "type", "clear", "select", "press", "scroll", "navigate", "read",
    "wait", "back", "forward", "reload", "hover", "upload", "fill", "eval",
    "pdf", "download", "collect", "open-tab", "close-tab", "switch-tab",
    "save", "load", "see", "state",
}) | CONTROL_ACTIONS
MAX_DEPTH = 5
MAX_STEPS = 60
MAX_LOOP = 20
MAX_EXPANDED = 10_000

MAX_ARTIFACT_BYTES = 64 * 1024
# The artifact itself is read locally for digesting; the RPC dispatch is bound
# separately by MAX_RUN_JSON_CHARS on the exact serialized run payload, so the
# local bound matches the host's 65536-character JSON envelope.
MAX_RUN_JSON_CHARS = 32768
TRUNCATION_MARKERS = ("[truncated", "(truncated", "truncated by the MCP")

INTERACTION_ACTIONS = frozenset({
    "click", "type", "press", "select", "scroll",
    "hover", "fill",  # keyboard-capable flows: fill(fields+submit) in one call
})
INTERACTION_FIELDS = {
    "click": ("text", "label", "selector"),
    "type": ("text", "label", "selector"),
    "press": ("text", "key"),
    "select": ("text", "label", "selector", "option"),
    "hover": ("text", "label", "selector"),
    "fill": ("fields", "submit"),
}
# The interaction is the single qualified DOM action executed inside the
# initial-predicate branch (native framing step 2.0/2.0.0).
INTERACTION_OUTCOME_STEP = "2.0.0"

# Plugin-generated throw used for every false branch. The native backend
# turns it into "eval failed: Error: <MARKER>" inside a whole-response MCP
# error — distinguishable from ambiguous transport failures.
GUARD_THROW_PREFIX = "HERMES_BLADEBRO_GUARD_FALSE:"


def _false_guard(name: str) -> list:
    return [{"action": "eval",
             "js": f"throw new Error(\"{GUARD_THROW_PREFIX}{name}\")"}]



CSP_PREFIX = (
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
    'base-uri \'none\'; form-action \'none\'">'
)

DOCTYPE_RE = re.compile(r"\s*<!doctype\s+html>", re.IGNORECASE)


def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg})


def _ok(**fields) -> str:
    return json.dumps({"success": True, **fields})


def _result_text(env: dict) -> str:
    """Native MCP envelope -> plain result text ('' when non-text)."""
    result = env.get("result")
    return result if isinstance(result, str) else ""


def _parse_tab_id(text: str) -> str | None:
    """Tab id from native open/switch text ``... switched to tab <32hex>``."""
    marker = "switched to tab "
    if marker not in text:
        return None
    for line in text.splitlines():
        if marker in line:
            rest = line.split(marker, 1)[1].strip()
            token = rest.split(" ", 1)[0].strip()
            if len(token) == 32 and all(
                    c in "0123456789abcdefABCDEF" for c in token):
                return token.upper()
            return None
    return None


def _walk_steps(steps, depth=0, path="", total=None):
    """Yield (path, action, step) for every node; ONE shared node budget."""
    if total is None:
        total = [0]
    if depth > MAX_DEPTH:
        raise ValueError(f"step nesting exceeds max depth {MAX_DEPTH}")
    for i, step in enumerate(steps or []):
        total[0] += 1
        if total[0] > MAX_STEPS:
            raise ValueError(f"batch exceeds max {MAX_STEPS} total actions (aggregate)")
        if not isinstance(step, dict):
            raise ValueError(f"step {path}{i} is not an object")
        action = str(step.get("action", ""))
        here = f"{path}{i}"
        yield here, action, step
        for branch in ("steps", "then", "else"):
            if isinstance(step.get(branch), list):
                yield from _walk_steps(step[branch], depth + 1, f"{here}.{branch}.", total)


def _worst_case_executions(steps):
    """Worst-case expanded executions of a tree: loops multiply, branches add.
    Cap-aware (returns cap+1 sentinel instead of overflowing huge products)."""
    total = 0
    for step in steps or []:
        action = str(step.get("action", ""))
        if action == "while":
            body = step.get("steps") or []
            max_iter = step.get("max") if isinstance(step.get("max"), int) else MAX_LOOP
            total += max_iter * (1 + _worst_case_executions(body))
        elif action == "if":
            total += 1 + max(_worst_case_executions(step.get("then") or []),
                             _worst_case_executions(step.get("else") or []))
        else:
            total += 1
        if total > MAX_EXPANDED:
            return MAX_EXPANDED + 1
    return total


def _validate_run_tree(steps):
    """Aggregate bounds + conservative side-effect classification.

    Type/depth/shape validation runs BEFORE the recursive worst-case loop
    cost: malformed trees must not reach cost computation.
    """
    errors: list[str] = []
    effects: list[str] = []
    try:
        for path, action, step in _walk_steps(steps):
            if action not in KNOWN_ACTIONS:
                errors.append(f"unknown step kind {action!r} at {path}")
                continue
            if action == "while":
                max_iter = step.get("max")
                if not isinstance(max_iter, int) or not (1 <= max_iter <= MAX_LOOP):
                    errors.append(
                        f"loop at {path} must carry an integer max between 1 and {MAX_LOOP}"
                    )
            if action == "if":
                if not isinstance(step.get("then"), list):
                    errors.append(f"if at {path} is missing a then branch")
            if action not in READ_ONLY_ACTIONS and action not in CONTROL_ACTIONS:
                effects.append(path)
    except ValueError as exc:
        errors.append(str(exc))
        return errors, effects
    expanded = _worst_case_executions(steps)
    if expanded > MAX_EXPANDED:
        errors.append(
            f"batch worst-case loop expansion exceeds {MAX_EXPANDED} executions "
            "(nested loop maxima multiply)"
        )
    return errors, effects


OUTSIDE_WORKSPACE = "OUTSIDE_WORKSPACE"
NOT_REGULAR = "NOT_REGULAR"
GUARD_UNAVAILABLE = "GUARD_UNAVAILABLE"
LOCAL_BACKENDS = ("local", "host")
SENSITIVE = "SENSITIVE_FILE"


def _terminal_backend(task_id) -> str:
    """Native task-aware backend type (docker/ssh scoped tasks do NOT share
    this host's filesystem view; local file reads must not silently run on
    the wrong backend)."""
    from tools.file_tools_paths import _terminal_env_type_for_task
    return _terminal_env_type_for_task(str(task_id or "default"))


def _local_backend_error(task_id) -> str | None:
    """Fail closed unless the effective terminal backend is local/host."""
    try:
        backend = _terminal_backend(task_id)
    except Exception as exc:  # native resolver unavailable: no guessing
        return f"terminal backend for this task is unverifiable: {exc}"
    if backend not in LOCAL_BACKENDS:
        return (f"terminal backend '{backend}' is unsupported: artifact/source "
                "reads are local-file operations; refuse rather than read the "
                "wrong filesystem")
    return None


def _native_read_block(path: str):
    """Canonical native read guard; None = allowed, GUARD_UNAVAILABLE = fail
    closed (never drift to a weaker copied guard)."""
    try:
        from agent.file_safety import get_read_block_error
    except Exception:
        return GUARD_UNAVAILABLE
    return get_read_block_error(path)


def _workspace_resolve(src: str, cwd: str) -> tuple[str, str | None]:
    """Resolve workspace-confined; returns (resolved, error_sentinel_or_None)."""
    path = src if os.path.isabs(src) else os.path.join(cwd, src)
    resolved = os.path.realpath(path)
    root = os.path.realpath(cwd)
    if resolved != root and not resolved.startswith(root + os.sep):
        return resolved, OUTSIDE_WORKSPACE
    return resolved, None


def _source_hashes(sources, cwd):
    """Streamed per-file sha256 (64KiB chunks); non-regular or workspace-
    escaping paths are un-certifiable sentinels, not silent skips. The
    native read guard (fail-closed) runs BEFORE any read/hashing."""
    out = {}
    for src in sources or []:
        resolved, sentinel = _workspace_resolve(src, cwd)
        if sentinel:
            out[src] = sentinel
            continue
        blocked = _native_read_block(resolved)
        if blocked:
            out[src] = GUARD_UNAVAILABLE if blocked == GUARD_UNAVAILABLE else SENSITIVE
            continue
        try:
            if not stat.S_ISREG(os.stat(resolved).st_mode):
                out[src] = NOT_REGULAR
                continue
            digest = hashlib.sha256()
            with open(resolved, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    digest.update(chunk)
            out[src] = digest.hexdigest()
        except OSError:
            out[src] = None
    return out


def _snapshot_error(hashes, label):
    missing = [k for k, v in hashes.items() if v is None]
    if missing:
        return f"{label}: source file(s) unreadable: {missing}"
    esc = [k for k, v in hashes.items() if v == OUTSIDE_WORKSPACE]
    if esc:
        return f"{label}: source(s) resolve outside the workspace: {esc}"
    reg = [k for k, v in hashes.items() if v == NOT_REGULAR]
    if reg:
        return f"{label}: source(s) are not regular files: {reg}"
    sens = [k for k, v in hashes.items() if v == SENSITIVE]
    if sens:
        return f"{label}: source(s) refused by the native read guard: {sens}"
    if any(v == GUARD_UNAVAILABLE for v in hashes.values()):
        return (f"{label}: native read guard unavailable; refusing rather than "
                "drifting to a weaker copied guard")
    return None




def _parse_predicate(raw, name: str) -> tuple[str | None, str | None]:
    """Certified predicates use native text conditions. Role/label snapshots
    remain available as observations, not role-based condition assertions."""
    if not isinstance(raw, dict):
        return None, f"{name} must be an object"
    unknown = sorted(set(raw) - {"text"})
    if unknown:
        return None, (f"{name} has field(s) {unknown}; only text predicates are "
                      "natively observable as qualified run conditions; "
                      "role/label snapshots are supplementary")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, f"{name}.text must be a non-empty string"
    return text, None


def _validate_interaction(step):
    """Declared single DOM interaction (qualified single-action flow): the
    native backend addresses it itself; no custom JS, no navigation, no url,
    no state/storage steps."""
    if not isinstance(step, dict):
        return None, "interaction must be exactly one declared DOM action object"
    action = str(step.get("action", ""))
    if action not in INTERACTION_ACTIONS:
        return None, (f"interaction action {action!r} not allowed (allowed: "
                      f"{sorted(INTERACTION_ACTIONS)}; no js/navigate/state/storage)")
    if step.get("url") or step.get("js") or step.get("steps") or step.get("then"):
        return None, "interaction step may not carry urls, js, or nested steps"
    if action == "fill":
        # Backend fill executes natively (multi-field form in one call,
        # submit dispatched once); keep the field-payload check first so a
        # bad field payload is never masked by the generic field error.
        allowed = set(INTERACTION_FIELDS[action]) | {"action"}
        extra = sorted(set(step) - allowed)
        if extra:
            return None, f"interaction step has disallowed fields {extra}"
        fields = step.get("fields")
        allowed_fields = {"label", "selector", "text", "option", "check"}
        submit = step.get("submit")
        if (submit is not None and (not isinstance(submit, str) or not submit.strip())
                or not isinstance(fields, list) or not fields or len(fields) > 10
                or any(not isinstance(f, dict) or set(f) - allowed_fields
                       or not any(isinstance(f.get(k), str) and f[k].strip()
                                  for k in ("label", "selector"))
                       or not any(k in f for k in ("text", "option", "check"))
                       or any(not isinstance(v, bool if k == "check" else str)
                              for k, v in f.items())
                       for f in fields)):
            return None, ("fill needs typed fields (1-10 "
                          "{label|selector, text|option|check}) "
                          "and an optional non-empty submit")
        return dict(step), None
    allowed = set(INTERACTION_FIELDS[action]) | {"action"}
    extra = sorted(set(step) - allowed)
    if extra:
        return None, f"interaction step has disallowed fields {extra}"
    for f in INTERACTION_FIELDS[action]:
        if f in step:
            if not isinstance(step[f], str) or not step[f].strip():
                return None, f"interaction field {f!r} must be a non-empty string"
    if not any(f in step for f in INTERACTION_FIELDS[action]):
        return None, (f"interaction step missing targeting field "
                      f"(one of {sorted(INTERACTION_FIELDS[action])})")
    return dict(step), None


def _canonical_source_revision(source_hashes):
    """Fixed continuation encoding (byte-exact):
    sha256: + sha256(b"".join(f"{path}\\0{per_file_sha256_hex}\\n" for sorted paths))"""
    payload = b"".join(
        f"{src}\0{source_hashes[src]}\n".encode() for src in sorted(source_hashes)
    )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _guard_js(url: str) -> str:
    # Plugin-generated EXACT equality guard (native condition:url is
    # substring — insufficient for binding). Qualified wire shape puts the js
    # expression in `text` with condition=js.
    return "location.href === " + json.dumps(url)


def register(ctx) -> None:
    server = str(ctx.get_config("server", "bladebro") or "bladebro")
    timeout = float(ctx.get_config("timeout", 60) or 60)
    state = ctx.state
    _browser_lock = threading.Lock()

    def _mcp(tool: str, args: dict) -> dict:
        with _browser_lock:
            return ctx.call_mcp(server, tool, args, timeout=timeout,
                                replay_on_failure=False)

    def _tabs() -> dict:
        tabs = state.get("tabs", {})
        return tabs if isinstance(tabs, dict) else {}

    def _owner_ok(owner: dict | None, kw: dict) -> str | None:
        """Session/task identity must match the recorded owner; unknown
        identity is refused (fail closed). PluginState is profile-scoped, so
        every op re-checks ownership — no shared-map membership shortcuts."""
        if not isinstance(owner, dict):
            return "record has no owner identity"
        sid, tid = kw.get("session_id"), kw.get("task_id")
        if not sid or not tid:
            return "session/task identity unavailable; operation refused"
        if owner.get("session_id") != sid or owner.get("task_id") != tid:
            return "record is owned by a different session/task; operation refused"
        return None

    def _drop_tab(tab_id: str) -> None:
        tabs = _tabs()
        if tab_id in tabs:
            del tabs[tab_id]
            state.set("tabs", tabs)

    def _switch(tab_id: str):
        """Switch to an owned tab; returns (True, env) or (False, error_text).
        On tab-not-found the owned binding is dropped and callers MUST stop.
        The host connection_token is host-minted and cannot be forged by
        server output: a token mismatch OR an ABSENT token voids the binding
        (fail closed — a stale connection that recycles tab ids must not be
        able to reuse ownership; McpReplayPolicyImpl)."""
        env = _mcp("state", {"op": "switch-tab", "target_id": tab_id})
        if env.get("ok"):
            token = env.get("connection_token")
            tab = _tabs().get(tab_id)
            bound = (tab or {}).get("connection_token")
            if not isinstance(token, str) or not token:
                _drop_tab(tab_id)
                return False, ("host connection token unavailable; ownership "
                               "cannot be verified, binding voided")
            if not isinstance(bound, str) or not bound or bound != token:
                _drop_tab(tab_id)
                return False, (f"host connection changed since tab opened; "
                               f"tab id preserved but session is not the same")
            return True, env
        err = str(env.get("error", ""))
        if "tab not found" in err:
            _drop_tab(tab_id)
        return False, err

    # ------------------------------------------------------------ lifecycle
    def _cleanup_session(trigger: str, session_id) -> None:
        """Session-boundary cleanup: close every tab owned by the ending
        session through the same native state surface the tools use, then
        drop ALL of that session's plugin state (tabs, ownership, connection
        tokens, scenario records). Best effort and honest: each close outcome
        is recorded in the log receipt (closed / already-closed / error /
        unknown); uncertain outcomes are NEVER retried or replayed (the _mcp
        dispatch is timeout-bounded and replay_on_failure=False); local
        state is cleared regardless so nothing is retained; tabs owned by
        other sessions are never touched."""
        sid = str(session_id or "")
        if not sid:
            return
        owned = sorted(
            tab_id for tab_id, tab in _tabs().items()
            if isinstance(tab, dict) and tab.get("session_id") == sid)
        receipt = []
        for tab_id in owned:
            try:
                env = _mcp("state", {"op": "close-tab", "target_id": tab_id})
            except Exception as exc:
                # Ambiguous transport outcome: unknown, never retried.
                receipt.append({"tab_id": tab_id, "outcome": "unknown",
                                "error": str(exc)})
                continue
            if env.get("ok"):
                receipt.append({"tab_id": tab_id, "outcome": "closed"})
            elif "tab not found" in str(env.get("error", "")):
                # Determinate: the host no longer holds the tab.
                receipt.append({"tab_id": tab_id, "outcome": "already-closed"})
            else:
                receipt.append({"tab_id": tab_id, "outcome": "error",
                                "error": str(env.get("error", ""))})
        tabs = _tabs()
        for tab_id in owned:
            tabs.pop(tab_id, None)
        state.set("tabs", tabs)
        index = state.get("scenarios", {})
        index = index if isinstance(index, dict) else {}
        owned_scenarios = sorted(k for k, v in index.items() if v == sid)
        for key in owned_scenarios:
            # PluginState has no delete; None reads as absent everywhere.
            state.set(f"scenario:{key}", None)
            index.pop(key, None)
        state.set("scenarios", index)
        for entry in receipt:
            if entry["outcome"] in ("error", "unknown"):
                logger.warning(
                    "browser-bladebro session cleanup (%s, session=%s): "
                    "close-tab %s outcome=%s error=%r (recorded honestly; "
                    "not retried)", trigger, sid, entry["tab_id"],
                    entry["outcome"], entry.get("error"))
        logger.info(
            "browser-bladebro session cleanup (%s, session=%s): tabs=%s, "
            "scenarios_cleared=%d", trigger, sid,
            {e["tab_id"]: e["outcome"] for e in receipt} or "none owned",
            len(owned_scenarios))

    def _lifecycle_hook(trigger: str):
        """on_session_finalize/on_session_reset(session_id, old_session_id,
        reason) fire at true session boundaries (CLI shutdown, /new, gateway
        shutdown, session close) as the unbounded last-chance flush.
        on_session_end is DELIBERATELY NOT registered: it fires at the end of
        every turn, so acting on it would reap tabs between turns and break
        multi-turn browsing and scenario continuity. Kwargs may grow; **_
        tolerates that. Never raises (best effort) so the boundary itself is
        never disturbed."""
        def hook(session_id="", old_session_id="", **_):
            try:
                _cleanup_session(trigger, old_session_id or session_id)
            except Exception as exc:
                logger.warning("browser-bladebro %s cleanup failed: %s",
                               trigger, exc)
        return hook

    for _hook_name in ("on_session_finalize", "on_session_reset"):
        ctx.register_hook(_hook_name, _lifecycle_hook(_hook_name))

    def _task_cwd(task_id) -> str | None:
        """Recorded cwd for THIS task only; no ambient fallback."""
        if not task_id:
            return None
        try:
            from tools.terminal_tool import get_session_cwd
            return get_session_cwd(str(task_id))
        except Exception:
            return None

    # ---------------------------------------------------------------- open
    def browser_open(args: dict, **kw) -> str:
        url = str(args.get("url", "")).strip()
        if not url:
            return _err("url is required")
        sid, tid = kw.get("session_id"), kw.get("task_id")
        if not sid or not tid:
            return _err("session/task identity unavailable; cannot own a tab")
        env = _mcp("state", {"op": "open-tab", "url": url})
        if not env.get("ok"):
            return _err(f"open-tab failed: {env.get('error')}")
        tab_id = _parse_tab_id(_result_text(env))
        if not tab_id:
            return _err(f"native open-tab result did not carry a tab id (expected: {TAB_ID_RE_HINT})")
        token = env.get("connection_token")
        if not isinstance(token, str) or not token:
            # Fail closed: an unverified connection cannot own a certifiable
            # tab. The host mints the token; its absence means ownership is
            # unverifiable, not unenforced.
            return _err("host connection token unavailable; refusing to own a tab")
        tabs = _tabs()
        tabs[tab_id] = {"url": url, "opened": time.time(),
                        "session_id": sid, "task_id": tid,
                        # Host-minted connection identity (cannot be forged
                        # by server output; verified on every switch).
                        "connection_token": token}
        state.set("tabs", tabs)
        return _ok(tab_id=tab_id, url=url, result=_result_text(env))

    # ---------------------------------------------------------------- close
    def browser_close(args: dict, **kw) -> str:
        tab_id = str(args.get("tab_id", ""))
        tab = _tabs().get(tab_id)
        denied = _owner_ok(tab, kw) if tab is not None else "tab is not owned by this plugin session"
        if denied:
            return _err(denied)
        env = _mcp("state", {"op": "close-tab", "target_id": tab_id})
        if not env.get("ok"):
            return _err(f"close-tab failed: {env.get('error')}")
        _drop_tab(tab_id)
        return _ok(tab_id=tab_id, closed=True)

    # ---------------------------------------------------------------- run
    def browser_run(args: dict, **kw) -> str:
        tab_id = str(args.get("tab_id", ""))
        tab = _tabs().get(tab_id)
        denied = _owner_ok(tab, kw) if tab is not None else "tab is not owned by this plugin session"
        if denied:
            return _err(denied)
        steps = args.get("steps")
        if not isinstance(steps, list) or not steps:
            return _err("steps must be a non-empty list of native run/act action objects")
        errors, effects = _validate_run_tree(steps)
        if errors:
            return _err("; ".join(errors))
        expected_url = str(tab["url"])
        if effects:
            # Approval binds session+task+workspace+server+tab+target+the FULL
            # plugin-generated guarded batch; the model cannot self-authorize.
            cwd = _task_cwd(kw.get("task_id")) or ""
            canonical = json.dumps(
                {"session": kw.get("session_id"), "task": kw.get("task_id"),
                 "cwd": cwd, "server": server, "tab": tab_id,
                 "target": expected_url, "steps": steps},
                sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            reason = (f"browser batch [{digest[:16]}] targeting {expected_url} "
                      f"(tab {tab_id}) performs side effects at {effects[:5]}"
                      f"{'...' if len(effects) > 5 else ''}")
            try:
                from tools.approval import request_tool_approval
            except ImportError:
                return _err("native approval gate unavailable; mutating batch refused")
            verdict = request_tool_approval(
                "browser_run", reason, rule_key=f"browser-bladebro:run:{digest[:16]}")
            if not (isinstance(verdict, dict) and verdict.get("approved")):
                blocked = verdict.get("message") if isinstance(verdict, dict) else None
                return _err(f"approval gate denied mutating batch: {blocked or 'not approved'}")
        # ONE native run RPC (qualified shape): switch-tab + plugin exact-URL
        # guard wrapping the approved steps; false guard aborts with the
        # plugin marker and mutates nothing. Unknown outcome is never replayed.
        guarded = [
            {"action": "state", "op": "switch-tab", "target_id": tab_id},
            {"action": "if", "condition": "js", "text": _guard_js(expected_url),
             "then": list(steps), "else": _false_guard("TARGET")},
        ]
        env = _mcp("run", {"steps": guarded})
        text = _result_text(env)
        if not env.get("ok"):
            err = str(env.get("error", ""))
            _drop_tab(tab_id)  # ambiguous OR false guard: binding is void
            if GUARD_THROW_PREFIX in err:
                return _err("exact-URL guard false; user steps did not run and "
                            "nothing was mutated (target mismatch)")
            return _err(f"native run failed (outcome unknown; tab binding "
                        f"invalidated, no replay): {err}")
        if any(marker in text for marker in TRUNCATION_MARKERS):
            return _err("run output truncated by the MCP envelope cap; outcome unknown, "
                        "refused (no success from partial framing)")
        out = {"run_output": text, "tab_id": tab_id, "target": expected_url}
        if "sign in" in text.lower() or "log in" in text.lower():
            out["steering"] = ("Login wall encountered: do not guess credentials — "
                               "stop and ask the user (the operator sets up site accounts).")
        return _ok(**out)

    # ---------------------------------------------------------------- scenario
    def _hashes_drift(record, label: str) -> str | None:
        fresh = _source_hashes(list(record["sources"]), record["cwd"])
        snap_err = _snapshot_error(fresh, label)
        if snap_err:
            return snap_err
        for src, want in record["sources"].items():
            if fresh.get(src) != want:
                return f"{label}: source {src} differs from the declared snapshot"
        return None

    def _scenario_build(sid: str, kw) -> str:
        record = state.get(f"scenario:{sid}")
        if not record:
            return _err(f"unknown scenario {sid!r}")
        denied = _owner_ok(record, kw)
        if denied:
            return _err(denied)
        if record["mode"] != "compiled" or not record.get("build_command"):
            return _err("compiled-mode scenario with a declared build_command required")
        backend_err = _local_backend_error(record.get("task_id"))
        if backend_err:
            return _err(backend_err)
        if record["status"] != "declared":
            return _err(f"scenario already built/observed (status={record['status']}); rebuild refused")
        drift = _hashes_drift(record, "pre-build snapshot mismatch")
        if drift:
            return _err(drift)
        artifact_absent_pre = not os.path.exists(
            os.path.join(record["cwd"], record["entry_artifact"]))
        cmd = record["build_command"]
        try:
            import model_tools
        except ImportError:
            return _err("model_tools unavailable; native-observed build refused")
        try:
            raw = model_tools.handle_function_call(
                "terminal",
                {"command": cmd, "background": False, "workdir": record["cwd"]},
                task_id=record["task_id"], session_id=record["session_id"],
            )
        except Exception as exc:
            return _err(f"native terminal dispatch failed: {exc}")
        try:
            res = json.loads(raw)
        except (ValueError, TypeError):
            return _err("terminal result was not valid JSON; outcome unknown, refused")
        if (not isinstance(res, dict) or res.get("exit_code") != 0
                or res.get("error") or res.get("process_id")):
            return _err(f"build did not complete in the foreground: {res}")
        evidence = res.get("verification_evidence")
        row_id = evidence.get("id") if isinstance(evidence, dict) else None
        if not isinstance(row_id, int):
            return _err("build produced no native evidence row; refusal, not inference")
        try:
            from agent.verification_evidence import verification_evidence_by_id
        except ImportError:
            return _err("native evidence lookup unavailable; build refused")
        lookup = verification_evidence_by_id(
            row_id, session_id=record["session_id"], cwd=record["cwd"])
        row = lookup.get("evidence") or {}
        if (lookup.get("status") != "passed" or row.get("kind") != "build"
                or row.get("exit_code") != 0 or row.get("command") != cmd
                or str(row.get("cwd")) != str(record["cwd"])
                or str(row.get("session_id")) != str(record["session_id"])):
            return _err(f"native build row does not match this invocation "
                        f"(status={lookup.get('status')}, kind={row.get('kind')}, "
                        f"exit={row.get('exit_code')}); refused")
        post_err = _hashes_drift(record, "post-build snapshot mismatch")
        if post_err:
            return _err(post_err)
        artifact_absent_post = not os.path.exists(
            os.path.join(record["cwd"], record["entry_artifact"]))
        artifact_bytes, artifact_error = _artifact_read(record["cwd"], record["entry_artifact"])
        if artifact_error:
            return _err(artifact_error)
        record["build_artifact_digest"] = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
        record["status"] = "built"
        record["build_row_id"] = row_id
        record["newly_generated"] = bool(artifact_absent_pre and not artifact_absent_post)
        record["build_label"] = (
            "newly generated" if record["newly_generated"]
            else "artifact observed after build (existed pre-invocation or vanished after)")
        state.set(f"scenario:{sid}", record)
        return _ok(scenario_id=sid, build_row_id=row_id,
                   newly_generated=record["newly_generated"], status="built")

    def browser_scenario(args: dict, **kw) -> str:
        action = str(args.get("action", ""))
        sid = str(args.get("scenario_id", "")).strip()
        if action == "status":
            declared = state.get(f"scenario:{sid}")
            if not declared:
                return _err(f"unknown scenario {sid!r}")
            denied = _owner_ok(declared, kw)
            if denied:
                return _err(denied)
            return _ok(**declared)
        if action == "build":
            return _scenario_build(sid, kw)
        if action != "declare":
            return _err("action must be declare|build|status")
        if not sid:
            return _err("declare needs scenario_id")
        if state.get(f"scenario:{sid}") is not None:
            return _err(f"scenario {sid!r} already declared; refuse silent overwrite")
        sources = args.get("sources")
        entry_artifact = str(args.get("entry_artifact", "")).strip()
        build_command = str(args.get("build_command", "")).strip()
        if not isinstance(sources, list) or not sources:
            return _err("declare needs non-empty sources (workspace-relative list)")
        if not entry_artifact:
            return _err("declare needs entry_artifact (workspace-relative HTML artifact)")
        initial_text, err = _parse_predicate(args.get("initial_predicate"), "initial_predicate")
        if err:
            return _err(err)
        final_text, err = _parse_predicate(args.get("final_predicate"), "final_predicate")
        if err:
            return _err(err)
        interaction, err = _validate_interaction(args.get("interaction"))
        if err:
            return _err(err)
        cwd = _task_cwd(kw.get("task_id"))
        if not cwd:
            return _err("no task-cwd recorded for this task_id; refusing to guess a working directory")
        backend_err = _local_backend_error(kw.get("task_id"))
        if backend_err:
            return _err(backend_err)
        direct_html = entry_artifact in sources
        if build_command and direct_html:
            return _err("direct-HTML artifacts are declared sources; build_command belongs to compiled mode")
        if direct_html:
            build_command = ""
        hashes = _source_hashes(sources, cwd)
        snap_err = _snapshot_error(hashes, "declare")
        if snap_err:
            return _err(snap_err)
        source_revision = _canonical_source_revision(hashes)
        record = {
            "scenario_id": sid,
            "mode": "direct_html" if direct_html else "compiled",
            "entry_artifact": entry_artifact,
            "build_command": build_command,
            "initial_text": initial_text,
            "final_text": final_text,
            "interaction": interaction,
            "sources": {src: hashes[src] for src in sources},
            "source_revision": source_revision,
            "build_row_id": None,
            "status": "declared",
            "session_id": kw.get("session_id"),
            "task_id": kw.get("task_id"),
            "cwd": cwd,
        }
        state.set(f"scenario:{sid}", record)
        index = state.get("scenarios", {})
        if not isinstance(index, dict):
            index = {}
        index[sid] = kw.get("session_id")
        state.set("scenarios", index)
        return _ok(scenario_id=sid, mode=record["mode"], source_hashes=hashes,
                   source_revision=source_revision, status="declared")

    # ---------------------------------------------------------------- capture
    def _artifact_read(cwd: str, artifact: str):
        """Trusted read: confined regular file, bounded size, strict UTF-8.
        Returns (bytes, None) or (None, error)."""
        resolved, sentinel = _workspace_resolve(artifact, cwd)
        if sentinel:
            return None, f"artifact {artifact} resolves outside the workspace"
        blocked = _native_read_block(resolved)
        if blocked == GUARD_UNAVAILABLE:
            return None, ("native read guard unavailable; refusing transport "
                          "rather than drifting to a weaker copied guard")
        if blocked:
            return None, f"sensitive file refused: {artifact!r}: {blocked}"
        try:
            st = os.stat(resolved)
        except OSError:
            return None, f"artifact {artifact} unreadable"
        if not stat.S_ISREG(st.st_mode):
            return None, f"artifact {artifact} is not a regular file"
        if st.st_size > MAX_ARTIFACT_BYTES:
            return None, f"artifact exceeds {MAX_ARTIFACT_BYTES} byte bound"
        try:
            with open(resolved, "rb") as fh:
                raw = fh.read(MAX_ARTIFACT_BYTES + 1)
        except OSError as exc:
            return None, f"artifact read failed: {exc}"
        if len(raw) > MAX_ARTIFACT_BYTES:
            return None, f"artifact exceeds {MAX_ARTIFACT_BYTES} byte bound"
        try:
            raw.decode("utf-8")  # strict validation; bytes kept for transport
        except UnicodeDecodeError as exc:
            return None, f"artifact is not UTF-8: {exc}"
        return raw, None

    def browser_capture(args: dict, **kw) -> str:
        sid = str(args.get("scenario_id", ""))
        record = state.get(f"scenario:{sid}")
        if not record:
            return _err(f"unknown scenario {sid!r}")
        denied = _owner_ok(record, kw)
        if denied:
            return _err(f"scenario {sid!r}: {denied}")
        backend_err = _local_backend_error(record.get("task_id"))
        if backend_err:
            return _err(backend_err)
        required_status = "declared" if record["mode"] == "direct_html" else "built"
        if record["status"] != required_status:
            return _err(f"scenario {sid!r} not ready (status={record['status']}, "
                        f"expected {required_status})")
        drift = _hashes_drift(record, "pre-observation snapshot mismatch")
        if drift:
            record["status"] = "stale"
            state.set(f"scenario:{sid}", record)
            return _err(f"stale source evidence refused: {drift}")
        raw_bytes, err = _artifact_read(record["cwd"], record["entry_artifact"])
        if err:
            return _err(err)
        raw_digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
        if (record["mode"] == "compiled"
                and record.get("build_artifact_digest") != raw_digest):
            return _err("artifact differs from the native build snapshot; evidence refused")
        # Qualified wire shape (browser-batch-qualification.py): DOCTYPE first,
        # CSP meta immediately after it; a duplicate doctype must never be
        # produced. (Actionability is governed by CSP_PREFIX quoting, not by
        # doctype ordering.)
        text = raw_bytes.decode("utf-8")
        head = DOCTYPE_RE.match(text)
        if head:
            transported_bytes = (head.group(0) + CSP_PREFIX
                                 + text[head.end():]).encode("utf-8")
        else:
            transported_bytes = (CSP_PREFIX + text).encode("utf-8")
        transported_digest = "sha256:" + hashlib.sha256(transported_bytes).hexdigest()
        payload_url = "data:text/html;base64," + base64.b64encode(
            transported_bytes).decode("ascii")

        def _record_and_resolve(observations_ok: bool, note: str) -> dict:
            """Mint exactly one native row and resolve it for the consumer.
            Negative observations record ok=False (failed stays failed); a
            disabled ledger yields unbound, never accepted."""
            payload = json.dumps({
                "raw_artifact_digest": raw_digest,
                "transported_digest": transported_digest,
                "build_row_id": record.get("build_row_id"),
                "newly_generated": record.get("newly_generated"),
                "interaction": record["interaction"],
                "final_observed": observations_ok,
                "note": note,
            }, sort_keys=True)
            err_note = None
            ev_id = None
            try:
                from agent.verification_evidence import (
                    record_external_verification, verification_evidence_by_id)
            except ImportError as exc:
                return {"id": None, "status": "unbound", "reason": str(exc)}
            try:
                rec = record_external_verification(
                    session_id=record["session_id"], cwd=record["cwd"],
                    source="browser-bladebro", check_id=f"browser:{sid}",
                    ok=observations_ok, output=payload,
                    source_revision=record["source_revision"],
                    artifact_digest=transported_digest,
                )
                ev_id = rec.get("id") if isinstance(rec, dict) else None
            except Exception as exc:
                err_note = str(exc)
            consumer = "unbound"
            if ev_id is not None:
                try:
                    from agent.verification_evidence import verification_evidence_by_id
                    lookup = verification_evidence_by_id(
                        ev_id, session_id=record["session_id"], cwd=record["cwd"])
                    consumer = str(lookup.get("status"))
                except Exception as exc:
                    err_note = str(exc)
            return {"id": ev_id, "status": consumer, "reason": err_note}

        # Base64 inflates the artifact ~4/3, and the qualified flow echoes
        # payload_url twice. Bound the exact RPC that will be sent instead of
        # inferring transport size from artifact size; native MCP envelopes
        # cap result text near 64KiB.
        run_steps = [
            {"action": "state", "op": "open-tab", "url": payload_url},
            {"action": "if", "condition": "js", "text": _guard_js(payload_url),
             "then": [{"action": "if", "condition": "text",
                       "text": record["initial_text"],
                       "then": [dict(record["interaction"])],
                       "else": _false_guard("INITIAL")}],
             "else": _false_guard("TARGET")},
            {"action": "if", "condition": "text", "text": record["final_text"],
             "then": [{"action": "if", "condition": "js", "text": _guard_js(payload_url),
                       "then": [{"action": "see", "mode": "model", "budget": 1200}],
                       "else": _false_guard("FINAL_TARGET")}],
             "else": _false_guard("FINAL")},
        ]
        if len(json.dumps({"steps": run_steps})) > MAX_RUN_JSON_CHARS:
            return _err(f"certified run payload exceeds measured bound "
                        f"({MAX_RUN_JSON_CHARS} JSON characters); refusing before dispatch")
        canonical = json.dumps(
            {"session": record["session_id"], "task": record["task_id"],
             "cwd": record["cwd"], "server": server, "steps": run_steps},
            sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        try:
            from tools.approval import request_tool_approval
        except ImportError:
            return _err("native approval gate unavailable; capture refused")
        verdict = request_tool_approval(
            "browser_capture", f"Execute declared browser interaction [{digest[:16]}]",
            rule_key=f"browser-bladebro:capture:{digest}")
        if not (isinstance(verdict, dict) and verdict.get("approved")):
            return _err("approval gate denied mutating capture before dispatch")
        env = _mcp("run", {"steps": run_steps})
        if not env.get("ok"):
            err_text = str(env.get("error", ""))
            if GUARD_THROW_PREFIX in err_text:
                branch = err_text.split(GUARD_THROW_PREFIX, 1)[1].split("\n", 1)[0].strip()
                record["status"] = "failed"  # determinate negative observation
                record["failed_guard"] = branch
                mint = _record_and_resolve(False, f"guard_{branch.lower()}_false")
                record["evidence_id"] = mint["id"]
                state.set(f"scenario:{sid}", record)
                return _err(f"guard {branch} false; scenario FAILED honestly; "
                            f"negative evidence row={mint['id']} "
                            f"(binding={mint['status']})")
            # Ambiguous failure (timeout/auth/expiry): no row, unbound, no replay.
            record["status"] = "unbound"
            state.set(f"scenario:{sid}", record)
            return _err(f"native run failed (outcome unknown; unbound, no replay): {err_text}")
        out_text = _result_text(env)
        # Host-minted connection identity on the run envelope: an observation
        # from an unverifiable connection can never mint evidence (negative
        # guard-false rows above are determinate and tokenless by design).
        token = env.get("connection_token")
        if not isinstance(token, str) or not token:
            record["status"] = "unbound"
            state.set(f"scenario:{sid}", record)
            return _err("host connection token unavailable; capture evidence refused")
        if any(marker in out_text for marker in TRUNCATION_MARKERS):
            # Truncated framing is UNKNOWN framing, never partial success.
            record["status"] = "unbound"
            state.set(f"scenario:{sid}", record)
            return _err("run framing truncated by the MCP envelope cap; outcome unknown, "
                        "evidence refused")
        observations = _parse_run_outcomes(out_text)
        if observations is None:
            # Unparseable framing: unknown outcome, never success by omission.
            record["status"] = "unbound"
            state.set(f"scenario:{sid}", record)
            return _err("native run framing unrecognized; outcome unknown, evidence refused")
        observed_success = observations["initial"] and observations["final"]
        # Backend-produced accessibility snapshot (see mode=model) is already
        # in-hand: surface whether the final flow left a keyboard-operable
        # interactive element available, as an ACCESSIBILITY observation —
        # advisory, never evidence-bearing.
        accessibility_note = None
        if not any(k in out_text for k in ("button", "link", "textbox", "checkbox")):
            accessibility_note = ("backend accessibility snapshot shows no interactive "
                                  "element after the flow; review keyboard operability")
        drift = _hashes_drift(record, "post-observation snapshot mismatch")
        post_bytes, err = _artifact_read(record["cwd"], record["entry_artifact"])
        post_digest = ("sha256:" + hashlib.sha256(post_bytes).hexdigest()
                       if post_bytes is not None else err)
        if drift or post_digest != raw_digest:
            record["status"] = "stale"
            state.set(f"scenario:{sid}", record)
            return _err("source or artifact drifted during observation; evidence refused")
        # Backend role/label snapshot: the qualified run's final see already
        # carries the accessibility snapshot (interactive elements with
        # roles/labels), so the supplementary call is skipped when the run
        # output contains those observations. see(logs=console) is never in
        # the run framing and is always sampled. Supplementary observations
        # only — never evidence-bearing, never flip the verdict.
        supplementary = {}
        if observed_success:
            observations_present = any(
                k in out_text for k in ("button", "link", "textbox", "checkbox"))
            samples = () if observations_present else (
                ("role_label_snapshot", {"mode": "model", "budget": 1200}),)
            for name, obs in (*samples,
                              ("console_errors", {"logs": "console"})):
                seen = _mcp("see", obs)
                supplementary[name] = (str(seen.get("result", ""))
                                       if seen.get("ok")
                                       else f"unavailable: {seen.get('error')}")
        # Close the tab we created (no replay of ambiguous operations).
        created_tab = _parse_tab_id(out_text)
        if created_tab:
            _mcp("state", {"op": "close-tab", "target_id": created_tab})
            _drop_tab(created_tab)
        mint = _record_and_resolve(observed_success, "qualified_flow")
        evidence_id = mint["id"]
        consumer_status = mint["status"]
        record_error = mint["reason"]
        if not observed_success:
            record["status"] = "failed"  # negative stays failed
        elif consumer_status == "passed":
            record["status"] = "passed"
        else:
            record["status"] = "unbound"
        record["last_observation"] = {
            "initial_observed": observations["initial"],
            "final_observed": observations["final"],
            "raw_artifact_digest": raw_digest,
            "transported_digest": transported_digest,
            "supplementary": {k: v[:500] for k, v in supplementary.items()},
        }
        state.set(f"scenario:{sid}", record)
        out = {
            "scenario_id": sid,
            "passed": observed_success,
            "initial_observed": observations["initial"],
            "final_observed": observations["final"],
            "raw_artifact_digest": raw_digest,
            "artifact_digest": transported_digest,
            "build_row_id": record.get("build_row_id"),
            "newly_generated": record.get("newly_generated"),
            "source_revision": record["source_revision"],
            "native_recorded": evidence_id is not None,
            "evidence_id": evidence_id,
            "evidence_status": consumer_status,
            "evidence_binding": (
                "bound" if consumer_status in ("passed", "failed")
                else f"unbound: native ledger unavailable ({record_error or 'recorder absent or returned None'})"
            ),
        }
        if accessibility_note:
            out["accessibility_note"] = accessibility_note
        if supplementary:
            # Observations are supplementary: backend role/label snapshot and
            # console errors inform the reader; they cannot mint or void
            # certification (that stays with the qualified run framing).
            out["role_label_snapshot"] = supplementary.get("role_label_snapshot", "")[:500]
            out["console_errors"] = supplementary.get("console_errors", "")[:500]
        return _ok(**out)

    def _parse_run_outcomes(text: str):
        """Strictly parse the qualified native run framing:
        step 2.0 initial if ->then, step 2.0.0 interaction dom-changed
        outcome, step 3 final if ->then, step 3.0 backend see observation.
        Anything else is unknown framing -> unknown outcome (None)."""
        initial = final = interaction_ok = see_observed = False
        for line in text.splitlines():
            if line.startswith("step 2.0: if(text") and "→ then" in line:
                initial = True
            elif line.startswith(f"step {INTERACTION_OUTCOME_STEP}: outcome:") \
                    and "dom-changed" in line:
                interaction_ok = True
            elif line.startswith("step 3: if(text") and "→ then" in line:
                final = True
            elif line.startswith("step 3.0.0: see ") and "chars" in line:
                see_observed = True
        if not (initial and interaction_ok and final and see_observed):
            return None
        return {"initial": True, "final": True}

    # ---------------------------------------------------------------- vision
    def browser_screenshot(args: dict, **kw) -> str:
        tab_id = str(args.get("tab_id", ""))
        tab = _tabs().get(tab_id)
        denied = _owner_ok(tab, kw) if tab is not None else "tab is not owned by this plugin session"
        if denied:
            return _err(denied)
        ok, res = _switch(tab_id)
        if not ok:
            return _err(f"switch failed: {res}")
        env = _mcp("vision", {"marks": bool(args.get("marks"))})
        if not env.get("ok"):
            return _err(f"vision failed: {env.get('error')}")
        return _ok(
            tab_id=tab_id,
            supplementary_only=True,
            note="screenshots are supplementary; certified evidence requires the native run flow",
            result=_result_text(env),
        )

    def _allowlisted() -> bool:
        try:
            ctx.call_mcp(server, "state", {"op": "tabs"}, timeout=10)
            return True
        except PermissionError:
            return False
        except Exception:
            return False

    tools = {
        "browser_open": (
            "Open a browser tab owned by this plugin session; returns the native tab id. "
            "Ordinary browsing only — a tab opened here can NEVER mint certified source+build evidence.",
            {"url": {"type": "string", "description": "URL to open"}},
            browser_open, ["url"],
        ),
        "browser_close": (
            "Close a tab this plugin session owns.",
            {"tab_id": {"type": "string"}},
            browser_close, ["tab_id"],
        ),
        "browser_run": (
            "Run a bounded native batch on an owned tab (session/task owner verified). "
            "Dispatch is ONE qualified native run RPC: native switch-tab plus a "
            "plugin-generated exact-URL js guard whose then-branch carries the approved "
            "steps and whose false branch is a plugin throw (whole-response error, "
            "nothing mutated). While loops require max <= %d; aggregate tree <= %d "
            "actions; nesting <= %d; worst-case expanded loop executions <= %d "
            "(nested maxima multiply); unknown step kinds refused, validated before "
            "loop cost. Truncated run output is refused as unknown outcome. Side "
            "effects escalate to the native human-approval gate bound to "
            "session+task+workspace+server+tab+target+the full plugin-generated batch; "
            "unknown outcomes are never replayed (tab binding invalidated). Ordinary "
            "browsing only — this cannot mint certifiable evidence."
            % (MAX_LOOP, MAX_STEPS, MAX_DEPTH, MAX_EXPANDED),
            {
                "tab_id": {"type": "string"},
                "steps": {"type": "array", "description": "native run/act step tree"},
            },
            browser_run, ["tab_id", "steps"],
        ),
        "browser_scenario": (
            "declare: pin an immutable artifact scenario — workspace source snapshot, "
            "confined entry_artifact HTML, typed initial/final text predicates "
            "(native text conditions; selector/role refused), the single declared DOM "
            "interaction (click/type/press/select/scroll/hover/fill — press carries "
            "key; keyboard-capable flows via fill(fields+submit); "
            "no js/navigate/state/storage), optional "
            "build_command for compiled mode (the artifact is then generated by the "
            "build and must be absent before it; qualified for single-file compiler "
            "outputs only — multi-file bundles/dependency graphs remain unverified). "
            "build: run the declared command in the foreground through the native tool "
            "gateway and require the actual native evidence row (kind=build, exit 0, "
            "exact command/cwd/session); refuse pre-existing output, background/yielded "
            "results, and source drift before/after. "
            "status: inspect the declaration (owner verified).",
            {
                "action": {"type": "string", "enum": ["declare", "build", "status"]},
                "scenario_id": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"}},
                "entry_artifact": {"type": "string"},
                "initial_predicate": {"type": "object"},
                "final_predicate": {"type": "object"},
                "interaction": {"type": "object"},
                "build_command": {"type": "string"},
            },
            browser_scenario, ["action", "scenario_id"],
        ),
        "browser_capture": (
            "Capture certified evidence for a declared scenario. The plugin constructs "
            "the navigation payload in trusted Python (qualified CSP prefix + declared "
            "artifact bytes, both digests recorded), and runs open-tab -> exact-URL "
            "guard -> initial text predicate -> declared interaction -> final text "
            "predicate + backend see in ONE native run; the native backend observes "
            "each step (qualified step framing parsed strictly; unrecognized or "
            "truncated framing is unknown outcome; false guards are plugin throws, so a "
            "false branch fails the whole run rather than succeeding silently — and "
            "fails honestly with a negative native evidence row). Pre/post source and "
            "artifact rehashing bounds drift; a failed predicate is recorded failed; a "
            "disabled ledger is unbound, never accepted. Arbitrary caller tabs, HTML, "
            "URLs, or digests cannot establish provenance. Backend accessibility "
            "snapshots are advisory only, never evidence-bearing.",
            {"scenario_id": {"type": "string"}},
            browser_capture, ["scenario_id"],
        ),
        "browser_screenshot": (
            "Supplementary screenshot of an owned tab (never evidence on its own).",
            {"tab_id": {"type": "string"}, "marks": {"type": "boolean"}},
            browser_screenshot, ["tab_id"],
        ),
    }
    for name, (desc, props, handler, required) in tools.items():
        ctx.register_tool(
            name=name,
            toolset="browser-bladebro",
            schema={
                "name": name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
            handler=lambda args, _h=handler, **kw: _h(args or {}, **kw),
            check_fn=_allowlisted,
            description=desc,
            emoji="🌐",
        )
    logger.info(
        "browser-bladebro: registered %d native MCP-backed tools + 2 session "
        "lifecycle cleanup hooks (server=%s); certified evidence = "
        "direct-artifact protocol on the qualified run shape",
        len(tools), server,
    )
