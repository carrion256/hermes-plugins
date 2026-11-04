"""bladebro browser backend over the native Hermes MCP client.

The ``browser_*`` tools (navigate, snapshot, click, type, scroll, back,
press, get_images, vision, console) — the same names, schemas and result
shapes the built-ins had before upstream Hermes removed them — are owned
and provided by this plugin on the bladebro backend. Core ``browser_exec``
(``tools/browser_use_cli.py``) is the separate CDP/browser-harness lane;
these are the bladebro real-browser lane tools, with no behavioral coupling.
Each Hermes task owns one backend tab.

Calls go through the host's native MCP path (``ctx.call_mcp``), gated by the
operator-granted ``plugins.entries.browser-bladebro.mcp_allowlist`` (default
deny).

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
from collections import Counter
from pathlib import Path

# Live URL/SSRF policy comes from the current Hermes tree — the same module
# core browser_exec enforces with (upstream deleted the built-in browser_*
# tools and their policy helpers on 2026-11-03). Bound at module level so
# tests can rebind these names; every call site below goes through them.
from tools.browser_url_policy import (  # noqa: E402  (_url_policy_error: landing-check parity)
    _url_policy_error,
    evaluate_url_safety as _evaluate_url_safety,
    eval_ssrf_guard_active as _eval_ssrf_guard_active,
    expression_targets_private_url as _expression_targets_private_url,
    url_blocked as _url_blocked,
)
from tools.url_safety import normalize_url_for_request as _normalize_url_for_request

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


STEP_RE = re.compile(r"(?m)^step (\d+): ")
STEP_ERROR_RE = re.compile(r"^✗ error: (?:step \d+ failed: )+")
ACTIONABLE_RE = re.compile(r"\| (\d+) actionable")
TITLE_RE = re.compile(r"(?m)^title: (.*)$")
CONSOLE_LINE_RE = re.compile(r"^(\w+): (.*)$")
MEDIA_RE = re.compile(r"MEDIA:(\S+)")
NO_BROWSER_SESSION = "No browser session for this task. Call browser_navigate first."
HREF_STEP = {"action": "eval", "js": "location.href"}
SCROLL_PIXELS = 500  # built-in parity: about half a viewport per call
FULL_SNAPSHOT_BUDGET = 50_000  # stays under the host's 64 KiB MCP envelope cap
GET_IMAGES_JS = (
    "[...document.images].map(img => ({src: img.src, alt: img.alt || '', "
    "width: img.naturalWidth, height: img.naturalHeight}))"
    ".filter(img => img.src && !img.src.startsWith('data:'))")


def _split_steps(text: str) -> dict[int, str]:
    """Native run framing -> {top-level step number: body}. Nested ``step 2.0:``
    lines stay inside their parent's body."""
    parts = STEP_RE.split(text)
    return {int(parts[i]): parts[i + 1].rstrip() for i in range(1, len(parts) - 1, 2)}


def _eval_value(body: str):
    """``js → result: <json>`` step body -> parsed value (raw text when not JSON)."""
    raw = body.split("result: ", 1)[1] if "result: " in body else body
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _native_ref(ref) -> str:
    """Built-in ``@e5`` / annotate ``[5]`` -> native ``e5`` (bladebro treats
    ``@e5`` as a stale ref)."""
    ref = str(ref or "").strip().lstrip("@").strip("[]")
    return f"e{ref}" if ref.isdigit() else ref


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


# --------------------------------------------------------------------------
# Plugin-owned model contract and the pure helpers ported from the built-in
# browser tools (Hermes 8f68cf7; upstream removed them 2026-11-03, leaving
# only an 11-line compat stub). URL/SSRF, secret-URL and website policy are
# NOT ported — they route through tools.browser_url_policy above, the current
# tree's own policy module (what browser_exec enforces with).
# --------------------------------------------------------------------------

DEFAULT_SNAPSHOT_THRESHOLD = 15000
MIN_SNAPSHOT_THRESHOLD = 1000
# Ceiling on the stored full-snapshot file (mirrors web_tools.MAX_STORED_TEXT_CHARS):
# the stored copy exists for read_file paging and must not be unbounded.
MAX_STORED_SNAPSHOT_CHARS = 2_000_000

_EVAL_NAVIGATED_WHY = "This may have been caused by a JavaScript navigation via browser_console."

_BROWSER_TOOL_EMOJI = {
    "browser_navigate": "🌐", "browser_snapshot": "📸", "browser_click": "👆",
    "browser_type": "⌨️", "browser_scroll": "📜", "browser_back": "◀️",
    "browser_press": "⌨️", "browser_get_images": "🖼️", "browser_vision": "👁️",
    "browser_console": "🖥️",
}

# Verbatim from tools/browser_tool.py @ 8f68cf7: the model-facing contract for
# the browser_* lane (names, schemas, result shapes — prompts, skills, toolset
# membership and display keep working unchanged).
BROWSER_TOOL_SCHEMAS = [
    {
        "name": "browser_navigate",
        "description": "Navigate to a URL in the browser. Initializes the session and loads the page. Must be called before other browser tools. For simple information retrieval, prefer a lightweight retrieval tool when one is available (faster, cheaper). For plain-text endpoints — URLs ending in .md, .txt, .json, .yaml, .yml, .csv, .xml, raw.githubusercontent.com, or any documented API endpoint — prefer an available text-extraction or terminal-fetch tool; the browser stack is overkill and much slower for these. Use browser tools when you need to interact with a page (click, fill forms, dynamic content). Returns a compact page snapshot with interactive elements and ref IDs — no need to call browser_snapshot separately after navigating.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to (e.g., 'https://example.com')"
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "browser_snapshot",
        "description": "Get a text-based snapshot of the current page's accessibility tree. Returns interactive elements with ref IDs (like @e1, @e2) for browser_click and browser_type. full=false (default): compact view with interactive elements. full=true: complete page content. Snapshots over 15000 chars are truncated or LLM-summarized; when that happens the complete snapshot is saved to a file and the output includes its path so you can page through the rest with read_file. Requires browser_navigate first. Note: browser_navigate already returns a compact snapshot — use this to refresh after interactions that change the page, or with full=true for complete content.",
        "parameters": {
            "type": "object",
            "properties": {
                "full": {
                    "type": "boolean",
                    "description": "If true, returns complete page content. If false (default), returns compact view with interactive elements only.",
                    "default": False
                }
            },
            "required": []
        }
    },
    {
        "name": "browser_click",
        "description": "Click on an element identified by its ref ID from the snapshot (e.g., '@e5'). The ref IDs are shown in square brackets in the snapshot output. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The element reference from the snapshot (e.g., '@e5', '@e12')"
                }
            },
            "required": ["ref"]
        }
    },
    {
        "name": "browser_type",
        "description": "Type text into an input field identified by its ref ID. Clears the field first, then types the new text. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The element reference from the snapshot (e.g., '@e3')"
                },
                "text": {
                    "type": "string", "description": "The text to type into the field"
                }
            },
            "required": ["ref", "text"]
        }
    },
    {
        "name": "browser_scroll",
        "description": "Scroll the page in a direction. Use this to reveal more content that may be below or above the current viewport. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string", "enum": ["up", "down"], "description": "Direction to scroll"
                }
            },
            "required": ["direction"]
        }
    },
    {
        "name": "browser_back",
        "description": "Navigate back to the previous page in browser history. Requires browser_navigate to be called first.",
        "parameters": { "type": "object", "properties": {}, "required": [] }
    },
    {
        "name": "browser_press",
        "description": "Press a keyboard key. Useful for submitting forms (Enter), navigating (Tab), or keyboard shortcuts. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key to press (e.g., 'Enter', 'Tab', 'Escape', 'ArrowDown')"
                }
            },
            "required": ["key"]
        }
    },
    {
        "name": "browser_get_images",
        "description": "Get a list of all images on the current page with their URLs and alt text. Useful for finding images to analyze with the vision tool. Requires browser_navigate to be called first.",
        "parameters": { "type": "object", "properties": {}, "required": [] }
    },
    {
        "name": "browser_vision",
        "description": "Take a screenshot of the current page so you can inspect it visually. Use this when you need to understand what the page looks like - especially for CAPTCHAs, visual verification challenges, complex layouts, or cases where the text snapshot misses important visual information. When your active model has native vision, the screenshot is attached to your context directly and you inspect it on the next turn; otherwise Hermes falls back to an auxiliary vision model and returns a text analysis. Includes a screenshot_path that you can share with the user by including MEDIA:<screenshot_path> in your response. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What you want to know about the page visually. Be specific about what you're looking for."
                },
                "annotate": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, overlay numbered [N] labels on interactive elements. Each [N] maps to ref @eN for subsequent browser commands. Useful for QA and spatial reasoning about page layout."
                }
            },
            "required": ["question"]
        }
    },
    {
        "name": "browser_console",
        "description": "Get browser console output and JavaScript errors from the current page. Returns console.log/warn/error/info messages and uncaught JS exceptions. Use this to detect silent JavaScript errors, failed API calls, and application warnings. Requires browser_navigate to be called first. When 'expression' is provided, evaluates JavaScript in the page context and returns the result — use this for DOM inspection, reading page state, or extracting data programmatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "clear": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, clear the message buffers after reading"
                },
                "expression": {
                    "type": "string",
                    "description": "JavaScript expression to evaluate in the page context. Runs in the browser like DevTools console — full access to DOM, window, document. Return values are serialized to JSON. Example: 'document.title' or 'document.querySelectorAll(\"a\").length'"
                }
            },
            "required": []
        }
    },
]


def _redact_browser_output(value):
    """Force-redact secrets in browser-originated data (snapshots, console, eval
    results can carry page-rendered keys/cookies/tokens). Tool output is a model
    boundary, so this applies even when global log redaction is disabled.
    Ported verbatim from tools/browser_tool_snapshot.py @ 8f68cf7."""
    from agent.redact import redact_sensitive_text

    if isinstance(value, str):
        return redact_sensitive_text(value, force=True)
    if isinstance(value, list):
        return [_redact_browser_output(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_browser_output(item) for item in value)
    if isinstance(value, dict):
        return {_redact_browser_output(key): _redact_browser_output(item) for key, item in value.items()}
    return value


def _store_full_snapshot(snapshot_text: str):
    """Write a full snapshot to cache/web and return its path (None on failure — best-effort).

    Ported from tools/browser_tool_snapshot.py @ 8f68cf7: cache/web is mounted
    read-only into remote backends, so read_file can page through the complete
    tree on any backend. The stored copy is force-redacted (page-rendered
    secrets must not hit disk unmasked) and named by content hash so identical
    snapshots dedupe.
    """
    try:
        from hermes_constants import get_hermes_dir
        from agent.redact import redact_sensitive_text

        content = redact_sensitive_text(snapshot_text, force=True)
        if len(content) > MAX_STORED_SNAPSHOT_CHARS:
            content = (
                content[:MAX_STORED_SNAPSHOT_CHARS]
                + f"\n\n[... stored copy truncated at {MAX_STORED_SNAPSHOT_CHARS:,} chars "
                f"of {len(content):,} ...]"
            )
        from tools.spill_safety import ensure_spill_dir, write_text_exclusive

        cache_dir = get_hermes_dir("cache/web", "web_cache")
        ensure_spill_dir(cache_dir, private=False)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:10]
        path = cache_dir / f"browser-snapshot-{digest}.txt"
        # Deterministic filename in a well-known dir: refuse symlinks (lstat-unlink +
        # exclusive create); same-content re-snapshots legitimately overwrite. Not
        # private: cache/web is bind-mounted into remote backends' container UID.
        write_text_exclusive(path, content, private=False, overwrite=True)
        return str(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to store full browser snapshot: %s", exc)
        return None


def _truncate_snapshot(snapshot_text: str, max_chars: int) -> str:
    """Truncate a snapshot at line boundaries (never mid-element) to ``max_chars``.

    Ported from tools/browser_tool_snapshot.py @ 8f68cf7. The full snapshot is
    stored to cache/web and the appended note tells the agent how to page
    through it with read_file — element refs beyond the cut are in the file,
    not lost.
    """
    if len(snapshot_text) <= max_chars:
        return snapshot_text

    stored_path = _store_full_snapshot(snapshot_text)
    if stored_path:
        # Agent-visible path: read_file runs inside the active backend (#72389).
        from tools.credential_files import to_agent_visible_cache_path
        stored_path = to_agent_visible_cache_path(stored_path)

    lines = snapshot_text.split('\n')
    result: list[str] = []
    chars = 0
    # Reserve space for the truncation note (the stored-path variant is the
    # longer of the two). Clamp so tiny max_chars values still keep content.
    reserve = min(110 + len(stored_path or ""), max_chars // 2)
    for line in lines:
        if chars + len(line) + 1 > max_chars - reserve:
            break
        result.append(line)
        chars += len(line) + 1
    remaining = len(lines) - len(result)
    if remaining > 0:
        if stored_path:
            next_line = len(result) + 1
            result.append(
                f'\n[... {remaining} more lines truncated — full snapshot: '
                f'read_file path="{stored_path}" offset={next_line} limit=200]'
            )
        else:
            result.append(f'\n[... {remaining} more lines truncated, use browser_snapshot for full content]')
    return '\n'.join(result)


def _redact_browser_typed_text_for_display(value, typed_text):
    """Replace every occurrence of a secret-looking browser_type value with its redacted form.

    Ported verbatim from agent/display.py @ 8f68cf7 (the display helper left
    with the built-in tools). Backends echo the attempted input in error
    strings/metadata, so it is swapped before reaching logs, callbacks, the
    model, or chat history. Forced regardless of ``security.redact_secrets``:
    a leaked typed credential is a security boundary.
    """
    from agent.redact import redact_sensitive_text

    needle = "" if typed_text is None else str(typed_text)
    redacted = redact_sensitive_text(needle, force=True) if needle else needle
    if redacted == needle:
        return value
    if isinstance(value, str):
        return value.replace(needle, redacted)
    if isinstance(value, dict):
        return {key: _redact_browser_typed_text_for_display(item, typed_text) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_browser_typed_text_for_display(item, typed_text) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_browser_typed_text_for_display(item, typed_text) for item in value)
    return value


# browser_console(expression=...) policy, ported from
# tools/browser_tool_eval_policy.py @ 8f68cf7. ``browser.restrict_evaluate``
# left core with the built-in tools; the check itself is kept exactly as it
# was, opt-in on the same config keys (it gates on primitive *names*;
# private-address egress is enforced separately by the SSRF checks).

_RISKY_BROWSER_EVAL_PATTERNS = (
    (re.compile(r"\bdocument\s*\.\s*cookie\b", re.I), "document.cookie"),
    (re.compile(r"\b(?:localStorage|sessionStorage)\b", re.I), "web storage"),
    (re.compile(r"\bindexedDB\b", re.I), "IndexedDB"),
    (re.compile(r"\bcaches\s*\.\s*(?:open|match|keys)\b", re.I), "Cache Storage"),
    (re.compile(r"\bnavigator\s*\.\s*(?:clipboard|credentials|serviceWorker)\b", re.I), "navigator sensitive API"),
    (re.compile(r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(", re.I), "network request"),
    (re.compile(r"\bnavigator\s*\.\s*sendBeacon\s*\(", re.I), "network beacon"),
    (re.compile(r"\bdocument\s*\.\s*forms\b.*\bvalue\b", re.I | re.S), "form value extraction"),
    (re.compile(r"\bquerySelector(?:All)?\s*\([^)]*(?:input|textarea|password)[^)]*\).*\bvalue\b", re.I | re.S), "form value extraction"),
)

_JS_STRING_LITERAL_RE = re.compile(r"""'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`""", re.S)

_SENSITIVE_BROWSER_EVAL_TOKENS = (
    ("cookie", "document.cookie"),
    ("localStorage", "web storage"), ("sessionStorage", "web storage"),
    ("indexedDB", "IndexedDB"), ("caches", "Cache Storage"),
    ("clipboard", "navigator sensitive API"), ("credentials", "navigator sensitive API"),
    ("serviceWorker", "navigator sensitive API"),
    ("fetch", "network request"), ("XMLHttpRequest", "network request"),
    ("WebSocket", "network request"), ("EventSource", "network request"),
    ("sendBeacon", "network beacon"),
)


def _browser_eval_flag(key: str) -> bool:
    """Boolean ``browser.<key>`` (default False) from the effective config."""
    try:
        from hermes_cli.config_effective import load_user_config_effective
        browser_cfg = load_user_config_effective().get("browser", {})
        if isinstance(browser_cfg, dict) and key in browser_cfg:
            return str(browser_cfg[key]).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        pass
    return False


def _decode_js_string_literal(literal: str) -> str:
    """Best-effort decode of a JS string literal (not a parser): normalizes escapes like ``"co\\x6fkie"``."""
    if len(literal) < 2:
        return literal
    try:
        return bytes(literal[1:-1], "utf-8").decode("unicode_escape")
    except Exception:
        return literal[1:-1]


def _decoded_js_string_literals(expression: str) -> list:
    return [_decode_js_string_literal(match.group(0)) for match in _JS_STRING_LITERAL_RE.finditer(expression)]


def _sensitive_browser_eval_token_reason(expression: str):
    """Risk reason for direct or quoted sensitive primitives: direct spellings alone miss
    ``document["cookie"]`` / ``globalThis["fetch"]``, so tokens are also matched inside the decoded,
    concatenated string literals (catches ``document["coo" + "kie"]``)."""
    literals = "".join(_decoded_js_string_literals(expression)).lower()
    return next((reason for token, reason in _SENSITIVE_BROWSER_EVAL_TOKENS
                 if re.search(rf"\b{re.escape(token)}\b", expression, re.I) or token.lower() in literals), None)


def _risky_browser_eval_reason(expression: str):
    """Return a human-readable reason if a JS expression uses risky primitives."""
    if not expression:
        return None
    hit = next((reason for pattern, reason in _RISKY_BROWSER_EVAL_PATTERNS if pattern.search(expression)), None)
    return hit or _sensitive_browser_eval_token_reason(expression)


def _enforce_browser_eval_policy(expression: str):
    """Block sensitive browser JS evaluation when the opt-in denylist is on (opt-in because it gates on
    primitive *names*; private-address egress is enforced separately by the SSRF checks)."""
    if not _browser_eval_flag("restrict_evaluate") or _browser_eval_flag("allow_unsafe_evaluate"):
        return None
    reason = _risky_browser_eval_reason(expression)
    if not reason:
        return None
    return ("Blocked: browser_console(expression=...) tried to use sensitive browser "
            f"JavaScript primitive ({reason}) while browser.restrict_evaluate is "
            "enabled. Use browser_snapshot/browser_get_images/browser_console "
            "without expression for normal inspection, or set "
            "browser.restrict_evaluate: false in config.yaml to allow programmatic evaluation.")


def _blocked_private_page_json(blocked_url: str, why: str) -> str:
    """Refusal payload for a page whose URL targets a private/internal address.
    Ported verbatim from tools/browser_tool.py @ 8f68cf7."""
    return json.dumps({"success": False,
                       "error": f"Blocked: page URL targets a private or internal address ({blocked_url}). {why}"})


# browser_vision helpers, ported from tools/browser_tool_vision.py @ 8f68cf7
# (the Lightpanda pre-route and cloud-provider branches were built-in-engine
# plumbing with no analogue on this backend and are not carried over).

def _get_vision_model():
    """Model for browser_vision (screenshot analysis — multimodal)."""
    return os.getenv("AUXILIARY_VISION_MODEL", "").strip() or None


def _native_vision_result(screenshot_path, question: str, annotate: bool):
    """Multimodal tool-result envelope: the main model inspects the pixels itself.

    The embed is baked into history and re-sent every later turn, so apply the
    same proactive resize as vision_analyze's native path (skipped when already
    under both caps; without Pillow it fails open to the raw bytes).
    """
    from tools.vision_tools import (
        _EMBED_MAX_DIMENSION,
        _build_native_vision_tool_result,
        _resize_image_for_vision,
    )
    from tools.vision_tools_history_budget import resolve_embed_target_bytes

    data_url = _resize_image_for_vision(screenshot_path, mime_type="image/png",
                                        max_base64_bytes=resolve_embed_target_bytes(),
                                        max_dimension=_EMBED_MAX_DIMENSION, force_jpeg=True)
    native_result = _build_native_vision_tool_result(image_url=str(screenshot_path), question=question,
                                                     image_data_url=data_url,
                                                     image_size_bytes=screenshot_path.stat().st_size)
    meta = native_result.setdefault("meta", {})
    meta["screenshot_path"] = str(screenshot_path)
    native_result["text_summary"] = f"{native_result.get('text_summary', '')} Screenshot path: {screenshot_path}".strip()
    return native_result


def _analyze_screenshot_with_aux_llm(screenshot_path, question: str) -> str:
    """One-shot aux vision-LLM analysis (not baked into history), secret-redacted.

    Full resolution first; on a size-related provider rejection the image is
    downscaled once and retried. ``auxiliary.vision.timeout/temperature`` — local
    vision models can take well over 30s, so the default timeout is generous.
    """
    vision_prompt = (
        f"You are analyzing a screenshot of a web browser.\n\n"
        f"User's question: {question}\n\n"
        f"Provide a detailed and helpful answer based on what you see in the screenshot. "
        f"If there are interactive elements, describe them. If there are verification challenges "
        f"or CAPTCHAs, describe what type they are and what action might be needed. "
        f"Focus on answering the user's specific question."
    )
    _screenshot_bytes = screenshot_path.read_bytes()
    _screenshot_b64 = base64.b64encode(_screenshot_bytes).decode("ascii")
    data_url = f"data:image/png;base64,{_screenshot_b64}"
    vision_model = _get_vision_model()
    logger.debug("browser_vision: analysing screenshot (%d bytes)", len(_screenshot_bytes))

    vision_timeout = 120.0
    vision_temperature = 0.1
    try:
        from hermes_cli.config import cfg_get, load_config
        _vision_cfg = cfg_get(load_config(), "auxiliary", "vision", default={})
        if _vision_cfg.get("timeout") is not None:
            vision_timeout = float(_vision_cfg["timeout"])
        if _vision_cfg.get("temperature") is not None:
            vision_temperature = float(_vision_cfg["temperature"])
    except Exception:
        pass

    from agent.auxiliary_client import call_llm  # lazy: heavy client, only needed on the vision path

    call_kwargs = {
        "task": "vision", "temperature": vision_temperature, "timeout": vision_timeout,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": vision_prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
    }
    if vision_model:
        call_kwargs["model"] = vision_model
    try:
        response = call_llm(**call_kwargs)
    except Exception as _api_err:
        from tools.vision_tools import _is_image_size_error, _resize_image_for_vision, _RESIZE_TARGET_BYTES
        if not (_is_image_size_error(_api_err) and len(data_url) > _RESIZE_TARGET_BYTES):
            raise
        logger.info("Vision API rejected screenshot (%.1f MB); auto-resizing to ~%.0f MB and retrying...",
                    len(data_url) / (1024 * 1024), _RESIZE_TARGET_BYTES / (1024 * 1024))
        data_url = _resize_image_for_vision(screenshot_path, mime_type="image/png")
        call_kwargs["messages"][0]["content"][1]["image_url"]["url"] = data_url
        response = call_llm(**call_kwargs)

    from agent.redact import redact_sensitive_text  # the LLM may have read secrets off the screenshot
    return redact_sensitive_text((response.choices[0].message.content or "").strip())


def register(ctx) -> None:
    server = str(ctx.get_config("server", "bladebro") or "bladebro")
    timeout = float(ctx.get_config("timeout", 60) or 60)
    state = ctx.state
    # Reentrant: browser_vision holds switch-tab + capture as one critical section.
    _browser_lock = threading.RLock()

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

    # ------------------------------------------------------------ task tabs
    # The backend has ONE current tab shared by every caller. Each Hermes task
    # (keyed like the built-in browser: session + task_id) owns one tab, and
    # every call switches to it inside the same native run RPC as the action.
    def _task_key(kw) -> tuple[str, str]:
        return str(kw.get("session_id") or ""), str(kw.get("task_id") or "default")

    def _task_tab(kw) -> str | None:
        sid, tid = _task_key(kw)
        for tab_id, tab in _tabs().items():
            if isinstance(tab, dict) and (tab.get("session_id"), tab.get("task_id")) == (sid, tid):
                return tab_id
        return None

    def _open_task_tab(kw) -> tuple[str | None, str]:
        env = _mcp("state", {"op": "open-tab", "url": "about:blank"})
        if not env.get("ok"):
            return None, f"open-tab failed: {env.get('error')}"
        tab_id = _parse_tab_id(_result_text(env))
        if not tab_id:
            return None, f"native open-tab result did not carry a tab id (expected: {TAB_ID_RE_HINT})"
        sid, tid = _task_key(kw)
        tabs = _tabs()
        tabs[tab_id] = {"opened": time.time(), "session_id": sid, "task_id": tid}
        state.set("tabs", tabs)
        return tab_id, ""

    def _in_tab(tab_id: str, *steps) -> tuple[dict | None, str]:
        """ONE native run RPC: switch-tab, then ``steps`` (step 2 onward), so no
        other caller can move the backend's current tab in between. Returns
        ({step: body}, "") or (None, error); a vanished tab drops the binding
        and reports NO_BROWSER_SESSION (nothing ran)."""
        env = _mcp("run", {"steps": [
            {"action": "state", "op": "switch-tab", "target_id": tab_id}, *steps]})
        if env.get("ok"):
            return _split_steps(_result_text(env)), ""
        err = str(env.get("error", ""))
        if err.startswith("✗ error: step 1 failed") and "tab not found" in err:
            _drop_tab(tab_id)
            return None, NO_BROWSER_SESSION
        return None, STEP_ERROR_RE.sub("", err)

    def _task_steps(kw, *steps) -> tuple[dict | None, str]:
        tab_id = _task_tab(kw)
        return _in_tab(tab_id, *steps) if tab_id else (None, NO_BROWSER_SESSION)

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

    def _subagent_stop(child_session_id=None, **_):
        """Close the tabs a delegated child opened when it stops, instead of
        leaving them open until the parent session ends."""
        try:
            _cleanup_session("subagent_stop", child_session_id)
        except Exception as exc:
            logger.warning("browser-bladebro subagent_stop cleanup failed: %s", exc)

    ctx.register_hook("subagent_stop", _subagent_stop)

    def _task_cwd(task_id) -> str | None:
        """Recorded cwd for THIS task only; no ambient fallback."""
        if not task_id:
            return None
        try:
            from tools.terminal_tool import get_session_cwd
            return get_session_cwd(str(task_id))
        except Exception:
            return None

    # ------------------------------------------------ browser lane tool impls
    # Same names, schemas and result shapes the built-ins had (their modules
    # are gone upstream; the contract is plugin-owned above), backed by the
    # bladebro backend.

    def _json(payload) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def _snapshot_threshold() -> int:
        """Compact snapshot character budget: ``settings.snapshot_threshold``
        (the built-in ``browser.snapshot_threshold`` left core with the
        built-in tools), floored so paging stays meaningful."""
        try:
            configured = int(ctx.get_config("snapshot_threshold",
                                            DEFAULT_SNAPSHOT_THRESHOLD))
        except (TypeError, ValueError):
            configured = DEFAULT_SNAPSHOT_THRESHOLD
        return max(configured, MIN_SNAPSHOT_THRESHOLD)

    def _page_text(body: str) -> str:
        """Snapshot handling: oversized text is truncated at line boundaries
        with the full text stored for read_file paging, then redacted."""
        threshold = _snapshot_threshold()
        if len(body) > threshold:
            body = _truncate_snapshot(body, max_chars=threshold)
        return _redact_browser_output(body)

    def _snapshot_fields(body: str) -> dict:
        count = ACTIONABLE_RE.search(body)
        return {"snapshot": _page_text(body), "element_count": int(count.group(1)) if count else 0}

    def _landing_blocked(tab_id: str, landed) -> str | None:
        """Post-redirect check on where the page actually landed (metadata
        floor, private ranges, website policy — the current tree's
        ``browser_url_policy``, same verdict browser_exec applies); a blocked
        page is moved to about:blank so later reads cannot expose it."""
        if not isinstance(landed, str) or not landed.startswith(("http://", "https://")):
            return None
        blocked = _url_policy_error(landed)
        if blocked is None:
            return None
        _in_tab(tab_id, {"action": "navigate", "url": "about:blank"})
        return _err(f"Blocked: page landed on {landed}: {blocked['error']}")

    def _private_page_blocked(kw, why: str) -> str | None:
        """Host SSRF-guard parity: when the guard is active in this browser
        mode (the current tree's ``browser_url_policy.eval_ssrf_guard_active``,
        the verdict browser_exec applies), a page that moved itself to a
        private/internal address (JS ``location=``, meta refresh, an eval)
        yields no content and takes no input. The URL probe is this plugin's
        own tab run — the host probe reads the host browser session, which
        this plugin never drives; the verdict itself is the host's
        ``url_blocked``."""
        if not _eval_ssrf_guard_active():
            return None
        out, _probe_err = _task_steps(kw, HREF_STEP)
        if out is None:
            return None  # probe failed: fail open, like the host probe
        landed = _eval_value(out.get(2, ""))
        if isinstance(landed, str) and _url_blocked(landed):
            return _blocked_private_page_json(landed, why)
        return None

    def _input_blocked(kw, action: str) -> str | None:
        """Host ``_blocked_private_page_action`` parity for the input tools."""
        return _private_page_blocked(
            kw, f"Refusing to {action} on this page in this browser mode.")

    def _act(kw, step: dict, fields: dict, failure: str) -> dict:
        """One page action; the backend's verdict + page delta rides along as
        ``result`` so the model rarely needs a separate browser_snapshot."""
        out, err = _task_steps(kw, step)
        if out is None:
            return {"success": False, "error": err or failure}
        return {"success": True, **fields, "result": _page_text(out.get(2, ""))}

    def browser_navigate(args: dict, **kw) -> str:
        # Secret-in-URL refusal + normalize + full URL policy (metadata floor,
        # private ranges, website policy) — the current tree's
        # ``browser_url_policy.evaluate_url_safety``, exactly what a core
        # browser_open would enforce.
        url = _normalize_url_for_request(str(args.get("url", "")))
        blocked = _evaluate_url_safety(url)
        if blocked:
            return _json(blocked)
        steps = ({"action": "navigate", "url": url}, HREF_STEP)
        tab_id = _task_tab(kw)
        out, err = _in_tab(tab_id, *steps) if tab_id else (None, NO_BROWSER_SESSION)
        if out is None and err == NO_BROWSER_SESSION:
            # First navigation, or the backend lost the tab: nothing ran yet.
            tab_id, err = _open_task_tab(kw)
            if tab_id:
                out, err = _in_tab(tab_id, *steps)
        if out is None:
            return _err(err)
        landed = _eval_value(out.get(3, ""))
        refused = _landing_blocked(tab_id, landed)
        if refused:
            return refused
        tab = _tabs().get(tab_id)
        if tab is not None and tab.pop("console_seen", None) is not None:
            state.set("tabs", {**_tabs(), tab_id: tab})
        body = out.get(2, "")
        title = TITLE_RE.search(body)
        return _json({"success": True, "url": landed if isinstance(landed, str) else url,
                      "title": title.group(1) if title else "", **_snapshot_fields(body)})

    def browser_snapshot(args: dict, **kw) -> str:
        full = bool(args.get("full"))
        out, err = _task_steps(kw, {
            "action": "see", "mode": "model", "content": full,
            "budget": FULL_SNAPSHOT_BUDGET if full else _snapshot_threshold()})
        if out is None:
            return _err(err)
        body = out.get(2, "")
        if body.startswith("see ("):  # drop the "see (N chars):" framing line
            body = body.split("\n", 1)[1] if "\n" in body else ""
        blocked = _private_page_blocked(kw, _EVAL_NAVIGATED_WHY)
        if blocked is not None:
            return blocked
        return _json({"success": True, **_snapshot_fields(body)})

    def browser_click(args: dict, **kw) -> str:
        ref = _native_ref(args.get("ref"))
        if not ref:
            return _err("ref is required")
        blocked = _input_blocked(kw, "click")
        if blocked is not None:
            return blocked
        return _json(_act(kw, {"action": "click", "ref": ref}, {"clicked": f"@{ref}"},
                          f"Failed to click @{ref}"))

    def browser_type(args: dict, **kw) -> str:
        from agent.display import redact_tool_args_for_display
        ref, text = _native_ref(args.get("ref")), str(args.get("text", ""))
        if not ref:
            return _err("ref is required")
        # The typed value already went to the page; keep secrets out of history.
        shown = (redact_tool_args_for_display("browser_type", {"text": text}) or {})["text"]
        blocked = _input_blocked(kw, "type")
        if blocked is not None:
            return blocked
        response = _act(kw, {"action": "type", "ref": ref, "text": text},
                        {"typed": shown, "element": f"@{ref}"}, f"Failed to type into @{ref}")
        return _json(_redact_browser_typed_text_for_display(response, text))

    def browser_scroll(args: dict, **kw) -> str:
        direction = args.get("direction")
        if direction not in ("up", "down"):
            return _err(f"Invalid direction '{direction}'. Use 'up' or 'down'.")
        dy = SCROLL_PIXELS if direction == "down" else -SCROLL_PIXELS
        return _json(_act(kw, {"action": "scroll", "dy": dy}, {"scrolled": direction},
                          f"Failed to scroll {direction}"))

    def browser_back(args: dict, **kw) -> str:
        tab_id = _task_tab(kw)
        if not tab_id:
            return _err(NO_BROWSER_SESSION)
        out, err = _in_tab(tab_id, {"action": "back"}, HREF_STEP)
        if out is None:
            return _err(err or "Failed to go back")
        landed = _eval_value(out.get(3, ""))
        return _landing_blocked(tab_id, landed) or _json({
            "success": True, "url": landed if isinstance(landed, str) else "",
            "result": _page_text(out.get(2, ""))})

    def browser_press(args: dict, **kw) -> str:
        key = str(args.get("key", ""))
        if not key:
            return _err("key is required")
        blocked = _input_blocked(kw, "press")
        if blocked is not None:
            return blocked
        return _json(_act(kw, {"action": "press", "key": key}, {"pressed": key},
                          f"Failed to press {key}"))

    def browser_get_images(args: dict, **kw) -> str:
        out, err = _task_steps(kw, {"action": "eval", "js": GET_IMAGES_JS})
        if out is None:
            return _err(err or "Failed to get images")
        blocked = _private_page_blocked(kw, _EVAL_NAVIGATED_WHY)
        if blocked is not None:
            return blocked
        images = _eval_value(out.get(2, ""))
        if not isinstance(images, list):
            return _json({"success": True, "images": [], "count": 0,
                          "warning": "Could not parse image data"})
        return _json({"success": True, "images": _redact_browser_output(images),
                      "count": len(images)})

    def browser_console(args: dict, **kw) -> str:
        expression = args.get("expression")
        if expression is not None:
            policy_error = _enforce_browser_eval_policy(expression)
            if policy_error:
                return _err(policy_error)
            # Pre-scan: a fetch/XHR to an internal address never updates
            # location.href, so the post-eval recheck cannot see it.
            if _eval_ssrf_guard_active():
                literal = _expression_targets_private_url(str(expression))
                if literal:
                    return _err("Blocked: JavaScript expression targets a private or "
                                f"internal address ({literal}). Reading internal "
                                "endpoints via browser_console is not permitted in "
                                "this browser mode.")
            out, err = _task_steps(kw, {"action": "eval", "js": str(expression)})
            if out is None:
                return _err(err)
            blocked = _private_page_blocked(kw, _EVAL_NAVIGATED_WHY)
            if blocked is not None:
                return blocked
            value = _redact_browser_output(_eval_value(out.get(2, "")))
            return _json({"success": True, "result": value, "result_type": type(value).__name__})
        tab_id = _task_tab(kw)
        if not tab_id:
            return _err(NO_BROWSER_SESSION)
        out, err = _in_tab(tab_id, {"action": "see", "logs": "console"})
        if out is None:
            return _err(err)
        blocked = _private_page_blocked(kw, _EVAL_NAVIGATED_WHY)
        if blocked is not None:
            return blocked
        entries: list[list[str]] = []
        for line in out.get(2, "").splitlines()[1:]:
            if line.startswith("console"):  # "console (N entries):" / "console: (empty)"
                continue
            match = CONSOLE_LINE_RE.match(line)
            if match:
                entries.append([match.group(1), match.group(2)])
            elif entries:  # continuation of a multi-line message / stack
                entries[-1][1] += "\n" + line
        # The backend has no buffer clear, so `clear` records a baseline that
        # later reads subtract. ponytail: the baseline resets on browser_navigate
        # only; a reload by click/press can hide an identical repeat message.
        tab = _tabs().get(tab_id) or {}
        baseline = Counter(tab.get("console_seen") or [])
        fresh = []
        for level, text in entries:
            key = f"{level}: {text}"
            if baseline[key]:
                baseline[key] -= 1
            else:
                fresh.append((level, _redact_browser_output(text)))
        if args.get("clear"):
            tab["console_seen"] = [f"{level}: {text}" for level, text in entries]
            state.set("tabs", {**_tabs(), tab_id: tab})
        messages = [{"type": level, "text": text, "source": "console"}
                    for level, text in fresh if level != "exception"]
        errors = [{"message": text, "source": "exception"}
                  for level, text in fresh if level == "exception"]
        return _json({"success": True, "console_messages": messages, "js_errors": errors,
                      "total_messages": len(messages), "total_errors": len(errors)})

    def browser_vision(args: dict, **kw):
        question, annotate = str(args.get("question", "")), bool(args.get("annotate"))
        tab_id = _task_tab(kw)
        if not tab_id:
            return _err(NO_BROWSER_SESSION)
        # Vision is not a run step, so switch + capture are two RPCs held under
        # the plugin lock. ponytail: serializes this process only; another
        # process driving the same backend can switch tabs in between. Upgrade
        # path: a backend vision step inside `run` (or a target_id on vision).
        blocked = _private_page_blocked(kw, _EVAL_NAVIGATED_WHY)
        if blocked is not None:
            return blocked
        with _browser_lock:
            switched = _mcp("state", {"op": "switch-tab", "target_id": tab_id})
            if not switched.get("ok"):
                if "tab not found" in str(switched.get("error", "")):
                    _drop_tab(tab_id)
                    return _err(NO_BROWSER_SESSION)
                return _err(f"Failed to take screenshot: {switched.get('error')}")
            shot = _mcp("vision", {"marks": annotate})
        if not shot.get("ok"):
            return _err(f"Failed to take screenshot: {shot.get('error')}")
        media = MEDIA_RE.search(_result_text(shot))
        if not media or not os.path.isfile(media.group(1)):
            return _err("Failed to take screenshot: the backend returned no image")
        path = Path(media.group(1))
        from tools.vision_tools import _should_use_native_vision_fast_path
        if _should_use_native_vision_fast_path():
            return _native_vision_result(path, question, annotate)
        try:
            analysis = _analyze_screenshot_with_aux_llm(path, question)
        except Exception as exc:
            logger.warning("browser_vision analysis failed: %s", exc, exc_info=True)
            return _json({"success": False, "error": f"Error during vision analysis: {exc}",
                          "screenshot_path": str(path),
                          "note": "Screenshot was captured but vision analysis failed. "
                                  "You can still share it via MEDIA:<path>."})
        return _json({"success": True, "screenshot_path": str(path),
                      "analysis": analysis or "Vision analysis returned no content."})

    # ---------------------------------------------------------------- run
    def browser_run(args: dict, **kw) -> str:
        steps = args.get("steps")
        if not isinstance(steps, list) or not steps:
            return _err("steps must be a non-empty list of native run/act action objects")
        errors, effects = _validate_run_tree(steps)
        if errors:
            return _err("; ".join(errors))
        tab_id = _task_tab(kw)
        if not tab_id:
            return _err(NO_BROWSER_SESSION)
        here, err = _in_tab(tab_id, HREF_STEP)
        if here is None:
            return _err(err)
        expected_url = _eval_value(here.get(2, ""))
        if not isinstance(expected_url, str) or not expected_url:
            return _err("could not read the task tab's current URL; batch refused")
        # Input-action guard parity: a batch can click/type/press, so a page
        # sitting on an internal address must not receive it. The URL is the
        # one already read for the approval binding — no extra probe.
        if _eval_ssrf_guard_active() and _url_blocked(expected_url):
            return _blocked_private_page_json(
                expected_url, "Refusing to run this batch on this page in this "
                              "browser mode.")
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
                      f"performs side effects at {effects[:5]}"
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
        # ONE native run RPC: switch-tab + exact-URL guard (the URL read just
        # above, i.e. the approved target) wrapping the steps; a false guard
        # aborts with the plugin marker and mutates nothing. Never replayed.
        guarded = [
            {"action": "state", "op": "switch-tab", "target_id": tab_id},
            {"action": "if", "condition": "js", "text": _guard_js(expected_url),
             "then": list(steps), "else": _false_guard("TARGET")},
        ]
        env = _mcp("run", {"steps": guarded})
        text = _result_text(env)
        if not env.get("ok"):
            err = str(env.get("error", ""))
            if GUARD_THROW_PREFIX in err:
                return _err("exact-URL guard false: the page moved after approval; "
                            "user steps did not run and nothing was mutated")
            return _err(f"native run failed (outcome unknown, not replayed): "
                        f"{STEP_ERROR_RE.sub('', err)}")
        if any(marker in text for marker in TRUNCATION_MARKERS):
            return _err("run output truncated by the MCP envelope cap; outcome unknown, "
                        "refused (no success from partial framing)")
        out = {"run_output": text, "target": expected_url}
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

    def _allowlisted() -> bool:
        try:
            ctx.call_mcp(server, "state", {"op": "tabs"}, timeout=10)
            return True
        except PermissionError:
            return False
        except Exception:
            return False

    tools = {
        "browser_run": (
            "Run a bounded bladebro step batch (act/see/wait/if/while steps) on the "
            "current browser page — the one browser_navigate opened for this task — in "
            "ONE call, e.g. fill a form, wait, and branch on what appears. The batch is "
            "wrapped in an exact-URL guard bound to the page's current URL, so it never "
            "runs against a page that moved. While loops require max <= %d; aggregate "
            "tree <= %d actions; nesting <= %d; worst-case expanded loop executions <= %d "
            "(nested maxima multiply); unknown step kinds refused. Side effects escalate "
            "to the native human-approval gate bound to the full batch and target; "
            "unknown outcomes are never replayed. Ordinary browsing only — this cannot "
            "mint certifiable evidence."
            % (MAX_LOOP, MAX_STEPS, MAX_DEPTH, MAX_EXPANDED),
            {"steps": {"type": "array", "description": "native run/act step tree"}},
            browser_run, ["steps"],
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
    lane = {
        "browser_navigate": browser_navigate, "browser_snapshot": browser_snapshot,
        "browser_click": browser_click, "browser_type": browser_type,
        "browser_scroll": browser_scroll, "browser_back": browser_back,
        "browser_press": browser_press, "browser_get_images": browser_get_images,
        "browser_vision": browser_vision, "browser_console": browser_console,
    }
    registered = 0
    for schema in BROWSER_TOOL_SCHEMAS:
        handler = lane[schema["name"]]  # KeyError = schema grew a tool: fail loudly
        # Ordinary registration, not override: upstream removed the built-in
        # browser_* tools, so these names are unclaimed and no
        # allow_tool_override grant applies (or is needed).
        ctx.register_tool(
            name=schema["name"], toolset="browser", schema=schema,
            handler=lambda args, _h=handler, **kw: _h(args or {}, **kw),
            check_fn=_allowlisted, description=schema["description"],
            emoji=_BROWSER_TOOL_EMOJI.get(schema["name"], "🌐"),
        )
        registered += 1
    logger.info(
        "browser-bladebro: registered %d browser_* real-browser lane tools + %d "
        "bladebro extras + 3 cleanup hooks (server=%s)", registered, len(tools), server)
