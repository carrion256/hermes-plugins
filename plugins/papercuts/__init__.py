"""Papercuts bridge — typed, redacted forwarding to the native papercuts CLI.

The native binary owns dedup (report fingerprint), recurrence counts, and
status; this plugin owns typed field mapping, redaction, and conservative
argv building (no shell-style string splitting). Auto-capture is opt-in,
rate-bounded (quota check + filing + increment in one host state transaction
lock, atomic across profile processes), and reads only authoritative
delivered blocks and post_tool_call failures — never raw unredacted text.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Literal, Protocol, TypeAlias

logger = logging.getLogger(__name__)

CATEGORIES = ("tooling", "infra", "docs", "workflow", "model", "other")
SEVERITIES = ("low", "medium", "high", "blocker")
_MODES = ("report", "list", "ack", "resolve", "stats", "friction", "drafts")
AUTO_DAILY_CAP = 20
PASSTHROUGH_TIMEOUT_SECONDS = 30
DRAFT_RULE_DIR_DEFAULT = "~/.hermes/rules/drafts"
DRAFT_RULE_AFTER_DEFAULT = 3
# Refusals the auto-capture path can give, a subset of the friction module's
# receipt reasons so a sink can forward them without translation.
_FileOutcome: TypeAlias = Literal["filed", "quota", "disabled", "error"]


class _FrictionEntry(Protocol):
    """Structural view of ``friction.GateFriction`` (that module is path-loaded)."""

    source: str
    kind: str
    identity: str
    summary: str
    detail: str
    session_id: str


class _FrictionReceiptLike(Protocol):
    """Structural view of ``friction.FrictionReceipt``."""

    filed: bool
    reason: str


def _load_sibling(module_name: str, filename: str) -> ModuleType | None:
    """Load a module that lives beside this plugin, once per process.

    ``sys.modules`` is consulted FIRST: the shared modules register themselves under
    these names, so a second plugin path-loading the same file joins the SAME sink
    slot and counters rather than getting a private copy (two sinks = one silently
    unfilled). An absent or broken sibling is a documented degraded mode — the
    feature is unavailable — and never a crash at register.
    """
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / filename
    if not path.is_file():
        logger.warning("papercuts: %s is not installed; feature unavailable", filename)
        return None
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {filename}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module  # visible to nested imports before exec
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - foreign module import: any error is the
        # documented degraded mode, never a crash at register
        sys.modules.pop(module_name, None)
        logger.warning("papercuts: %s unusable (%s)", filename, exc)
        return None
    return module



def _passthrough(argv):
    try:
        return subprocess.run(argv, timeout=PASSTHROUGH_TIMEOUT_SECONDS,
                              check=False).returncode  # exit code is the result here
    except subprocess.TimeoutExpired:
        return 124
    except OSError as exc:
        print(f"papercuts: {exc}", file=sys.stderr)
        return 127


_HELP_CACHE: dict[tuple[str, str], str] = {}


def _supports(binary: str, mode: str, flag: str) -> bool:
    """True when this native build advertises ``flag`` for ``mode`` (cached per binary).

    Optional argv must survive native builds that do not define a flag: argparse rejects the
    whole invocation with exit 2 on an unknown argument, so an unsupported annotation cost
    the entire papercut instead of itself. Probing the build's own help keeps the record.
    """
    key = (binary, mode)
    text = _HELP_CACHE.get(key)
    if text is None:
        try:
            proc = subprocess.run([binary, mode, "--help"], capture_output=True,
                                  text=True, timeout=5,
                                  check=False)  # exit 2 on unknown flags is an answer, not an error
            text = f"{proc.stdout or ''}{proc.stderr or ''}"
        except (OSError, subprocess.SubprocessError):
            text = ""  # unprobeable: treat as unsupported rather than risk the record
        _HELP_CACHE[key] = text
    return flag in text


def _parse_time_lost(value) -> float:
    """Native trust boundary #2: typed numeric check before argv building.

    bool is a numeric subclass — rejected; NaN/inf/negative rejected.
    The native binary validates again; the plugin refuses before spawning.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("time_lost_seconds must be a finite nonnegative number")
    import math
    try:
        number = float(value)
    except OverflowError:
        raise ValueError("time_lost_seconds exceeds the finite numeric range") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError("time_lost_seconds must be a finite nonnegative number")
    return number


def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg})


def _redact(text: str) -> str | None:
    """Native egress redaction; None when the helper fails (fail closed)."""
    if text is None or text == "":
        return text or ""
    try:
        from agent.redact import redact_for_egress
        return redact_for_egress(str(text))
    except Exception:
        return None


def _normalize_signature(tool_name: str, rule_key: str | None, origin: str | None) -> str:
    """Stable failure signature encoded into the blame dedup dimension so
    recurrence increments even when human summaries drift."""
    normalized = f"{tool_name or 'unknown'}|{rule_key or origin or 'error'}".lower()
    return "sig:" + "".join(c if c.isalnum() or c in "|.-_" else "_" for c in normalized)[:120]


def register(ctx) -> None:
    binary = str(ctx.get_config("binary", "/home/common/projects/papercuts/papercuts"))
    auto_capture = bool(ctx.get_config("auto_capture", False))
    gate_friction = bool(ctx.get_config("gate_friction", False))
    draft_rules = bool(ctx.get_config("draft_rules", False))
    raw_threshold = ctx.get_config("draft_rule_after", DRAFT_RULE_AFTER_DEFAULT)
    raw_draft_dir = str(ctx.get_config("draft_rule_dir", DRAFT_RULE_DIR_DEFAULT) or "").strip()
    if not raw_draft_dir:
        raise ValueError("papercuts: draft_rule_dir must be a non-empty path")
    draft_rule_dir = Path(raw_draft_dir).expanduser()
    state = ctx.state
    friction = _load_sibling("hermes_plugins_papercuts_friction", "friction.py")
    drafts = _load_sibling("hermes_plugins_papercuts_drafts", "drafts.py")
    # One parser lives in drafts.py so every entry point refuses a bad threshold the
    # same way: a non-integer is a TypeError, an int below 1 a ValueError, both at
    # register. With the sibling unusable the feature cannot run, so nothing reads it.
    draft_rule_after = (drafts.parse_threshold(raw_threshold) if drafts is not None
                        else DRAFT_RULE_AFTER_DEFAULT)
    # Native helper does not recognize bare credential labels ("token <hex>",
    # "secret <hex>"); register additive over-redaction patterns through the
    # sanctioned native API (can only widen masking, never weaken built-ins).
    try:
        ctx.register_redaction_patterns([
            r"token\s[0-9a-fA-F]{16,}",
            r"secret\s[0-9a-fA-F]{16,}",
        ])
    except (AttributeError, TypeError, ValueError):
        logger.warning("papercuts: redaction pattern registration unavailable")

    def available() -> bool:
        import os
        return bool(binary) and (shutil.which(binary) is not None or os.path.exists(binary))

    def _run(argv: list[str], timeout: int = 30) -> dict:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                  check=False)  # we read returncode; a nonzero exit is data
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "papercuts timed out"}
        except OSError as exc:
            return {"success": False, "error": f"papercuts invocation failed: {exc}"}
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return {"success": False, "error": err or f"exit {proc.returncode}", "stdout": out}
        return {"success": True, "output": out}

    def _report(args: dict, **_kw) -> str:
        mode = str(args.get("mode", "report"))
        if mode not in _MODES:
            return _err(f"unknown mode {mode!r}")

        if mode == "report":
            category = str(args.get("category", ""))
            severity = str(args.get("severity", ""))
            summary = str(args.get("summary", "")).strip()
            if category not in CATEGORIES:
                return _err(f"category must be one of {CATEGORIES}")
            if severity not in SEVERITIES:
                return _err(f"severity must be one of {SEVERITIES}")
            if not summary:
                return _err("summary (human title) is required")
            # Fail closed: EVERY free-text/string field goes through native
            # egress redaction; the binary stores these columns verbatim.
            red_summary = _redact(summary)
            if red_summary is None:
                return _err("redaction unavailable; refusing to file raw summary")
            argv = [binary, "report", "--category", category,
                    "--severity", severity, "--summary", red_summary]
            time_note = None
            if args.get("time_lost_seconds") is not None:
                try:
                    seconds = _parse_time_lost(args["time_lost_seconds"])
                except ValueError as exc:
                    return _err(str(exc))
                # observed loss only — never inferred from operation success/duration
                if _supports(binary, "report", "--time-lost-seconds"):
                    argv += ["--time-lost-seconds", repr(seconds)]
                else:
                    # This build has no timing field: keep the observation in the context
                    # rather than letting argparse reject the whole report.
                    time_note = f"observed time lost: {seconds}s"
            context_lines = [time_note] if time_note else []
            for field in ("expected", "observed", "tool_versions", "repro", "reference"):
                value = args.get(field)
                if value:
                    context_lines.append(f"{field}: {value}")
            if context_lines:
                red_ctx = _redact("\n".join(context_lines))
                if red_ctx is None:
                    return _err("redaction unavailable; refusing to file raw context")
                argv += ["--context", red_ctx]
            snippet = args.get("invocation_result")
            if snippet:
                red = _redact(str(snippet))
                if red is None:
                    return _err("redaction unavailable; refusing to file raw invocation result")
                argv += ["--snippet", red]  # native scrub runs again; --raw never passed
            if args.get("repo"):
                red_repo = _redact(str(args["repo"]))
                if red_repo is None:
                    return _err("redaction unavailable; refusing to file raw repo")
                argv += ["--repo", red_repo]
            if args.get("blame"):
                red_blame = _redact(str(args["blame"]))
                if red_blame is None:
                    return _err("redaction unavailable; refusing to file raw blame")
                argv += ["--blame", red_blame]
            return _forward(mode, argv)

        if mode == "list":
            argv = [binary, "list"]
            if args.get("status") in ("open", "ack", "resolved"):
                argv += ["--status", str(args["status"])]
            if args.get("json", True):
                argv += ["--json"]
            if args.get("limit"):
                argv += ["--limit", str(int(args["limit"]))]
            if args.get("sort") == "impact":
                if not _supports(binary, "list", "--sort"):
                    return _err("sort: this papercuts build has no --sort; impact ranking unavailable")
                argv += ["--sort", "impact"]
            elif args.get("sort") is not None:
                return _err("sort must be 'impact'")
            return _forward(mode, argv)

        if mode in ("ack", "resolve"):
            pid = args.get("id")
            if not pid:
                return _err(f"mode {mode} requires id")
            return _forward(mode, [binary, mode, str(pid)])

        if mode == "friction":
            if friction is None:
                return _err("gate-friction module unavailable (friction.py unusable)")
            return json.dumps({"success": True, "mode": mode, **friction.stats()})

        if mode == "drafts":
            return _drafts_json()

        # stats: native recurrence counts only; no invented cost estimates.
        return _forward(mode, [binary, "stats"])

    def _forward(mode: str, argv: list[str]) -> str:
        result = _run(argv)
        if result.get("success"):
            result = {**result, "mode": mode}
        return json.dumps(result)

    def _collect_drafts() -> dict[str, object]:
        """ONE native ``list --json`` call, then write inert rule drafts.

        Returns a JSON-ready result — counts included, so "nothing written" is
        always distinguishable from "could not run". Raises nothing: a bad payload
        or an unwritable directory is a reported failure.
        """
        if drafts is None:
            return {"success": False,
                    "error": "drafts module unavailable (drafts.py unusable)"}
        if not available():
            return {"success": False, "error": f"papercuts binary unavailable: {binary}"}
        if not _supports(binary, "list", "--json"):
            return {"success": False,
                    "error": "this papercuts build has no list --json; drafts unavailable"}
        result = _run([binary, "list", "--json"])
        if not result.get("success"):
            return {"success": False,
                    "error": str(result.get("error", "papercuts list failed"))}
        try:
            entries, skipped_rows = drafts.parse_list_rows(result.get("output", ""))
            report = drafts.emit_drafts(entries, draft_rule_dir, draft_rule_after)
        except (TypeError, ValueError, OSError) as exc:
            return {"success": False, "error": f"drafts: {exc}"}
        return {"success": True,
                "written": [str(path) for path in report.written],
                "skipped_existing": report.skipped_existing,
                "below_threshold": report.below_threshold,
                "skipped_rows": skipped_rows}

    def _drafts_json() -> str:
        return json.dumps({**_collect_drafts(), "mode": "drafts"})


    # ---------------------------------------------------------------- auto capture

    def _auto_allowed() -> bool:
        """Quota CHECK only — never consumes. Increment happens only when the
        native report filing actually succeeds (post-qualification)."""
        if not auto_capture:
            return False
        day = time.strftime("%Y-%m-%d")
        counts = state.get("auto_capture", {})
        if not isinstance(counts, dict) or counts.get("day") != day:
            return True
        return int(counts.get("n", 0)) < AUTO_DAILY_CAP

    def _quota_exhausted() -> bool:
        """Advisory only: is today's auto-capture quota already at the cap?

        It chooses a receipt REASON, never permission — the authoritative check
        stays inside the transaction in ``_consume_on_filing``."""
        day = time.strftime("%Y-%m-%d")
        counts = state.get("auto_capture", {})
        if not isinstance(counts, dict) or counts.get("day") != day:
            return False
        return int(counts.get("n", 0)) >= AUTO_DAILY_CAP

    def _consume_on_filing(argv) -> bool:
        """Quota check + native filing + increment run as ONE transaction across
        every process sharing this profile: the host's own state-file transaction
        lock (fcntl/msvcrt, thread-safe per path) spans the actual native
        submission, so concurrent plugin processes cannot share the final quota
        slot or lose the increment. The counter is read from disk INSIDE the lock
        (never a cached value) and persisted only when the native filing
        succeeds; benign events never touch quota."""
        try:
            from hermes_cli.plugins_state import _locked_plugin_state
        except Exception:
            logger.debug("papercuts auto-capture refused: native state transaction unavailable")
            return False  # fail closed: never RMW quota without the native transaction
        with _locked_plugin_state(state.path):
            day = time.strftime("%Y-%m-%d")
            data = state._read_unlocked()
            counts = data.get("auto_capture")
            if not isinstance(counts, dict) or counts.get("day") != day:
                counts = {"day": day, "n": 0}
            if int(counts.get("n", 0)) >= AUTO_DAILY_CAP:
                return False
            result = _run(argv)
            if result.get("success"):
                counts["n"] = int(counts.get("n", 0)) + 1
                data["auto_capture"] = counts
                from utils import atomic_json_write
                atomic_json_write(state.path, data, mode=0o600)
            return bool(result.get("success"))

    def _file_failure(*, category: str, summary: str, signature: str,
                      snippet: str, repo: str = "") -> _FileOutcome:
        """File one auto-captured failure, or say WHY it was refused.

        The two observers ignore the result; the gate-friction sink forwards it as
        the receipt reason, so a refusal is an reported outcome, not a silence.
        ``"quota"`` is advisory (see ``_quota_exhausted``); permission is always
        decided inside ``_consume_on_filing``'s transaction."""
        if not _auto_allowed():
            return "quota" if _quota_exhausted() else "disabled"
        red_summary = _redact(summary)
        red_snippet = _redact(snippet)
        if red_summary is None or red_snippet is None:
            logger.debug("papercuts auto-capture refused: redaction unavailable")
            return "error"  # fail closed; never file raw
        argv = [binary, "report", "--category", category, "--severity", "low",
                "--summary", red_summary, "--blame", signature,
                "--snippet", red_snippet]
        if repo:
            red_repo = _redact(repo)
            if red_repo is None:
                return "error"
            argv += ["--repo", red_repo]
        if _consume_on_filing(argv):
            return "filed"
        return "quota" if _quota_exhausted() else "error"

    def _make_friction_sink(
        receipt_cls: Callable[[bool, str], _FrictionReceiptLike],
    ) -> Callable[[_FrictionEntry], _FrictionReceiptLike]:
        """Build the gate-friction sink against the shared module's receipt type.

        Filing a gate refusal means filing a workflow papercut through the SAME
        redaction and cross-process quota transaction as auto-capture. The blame
        signature is the caller's own identity, so a gate that fires a hundred
        times stays one record with a recurrence count."""
        def _sink(entry: _FrictionEntry) -> _FrictionReceiptLike:
            outcome = _file_failure(
                category="workflow",
                summary=entry.summary,
                signature=_normalize_signature(entry.source, entry.kind,
                                               entry.identity[:64]),
                snippet=f"{entry.detail}\nsession: {entry.session_id}"[:2000],
            )
            return receipt_cls(outcome == "filed", outcome)

        return _sink

    if friction is not None:
        # Explicit config decides: disabled means no sink, so a reload cannot leave
        # a stale closure filing after the operator turned the feature off.
        if gate_friction:
            friction.install_sink(_make_friction_sink(friction.FrictionReceipt))
        else:
            friction.uninstall_sink()

    def on_post_tool_directive(*, tool_name=None, tool_input=None, action=None,
                               delivered=None, directives=None, **_kw) -> None:
        """Loop-guard blocks only: authoritative delivered block directives whose
        selected message carries the LOOP GUARD prefix. Deliberately does NOT
        trust directive dicts alone — no approval bypasses here; we only file."""
        if not auto_capture or action != "block" or not delivered:
            return
        for directive in directives or []:
            if directive.get("outcome") != "delivered" or directive.get("action") != "block":
                continue
            message = str(directive.get("message") or "")
            if not message.startswith("LOOP GUARD"):
                continue
            rule_key = directive.get("rule_key")
            origin = directive.get("origin")
            _file_failure(
                category="workflow",
                summary=f"tool {tool_name} blocked by loop guard: {rule_key or origin}",
                signature=_normalize_signature(str(tool_name), rule_key, origin),
                snippet=f"{message}\ninput: {json.dumps(tool_input, default=str)[:2000]}",
            )
            return  # one report per authoritative selected directive per call

    def on_post_tool_call(*, tool_name=None, args=None, result=None,
                          status=None, error_type=None, error_message=None,
                          **_kw) -> None:
        # Authoritative host event fields decide; result text is never parsed
        # for failure markers (a successful result containing "error" text
        # must not file, and exotic failure renderings are covered).
        if not auto_capture or status != "error":
            return
        if isinstance(result, dict):
            err_text = error_message or str(result.get("error", ""))
            text = err_text or json.dumps(result, default=str)
        else:
            text = error_message or (result if isinstance(result, str)
                                     else json.dumps(result, default=str))
        _file_failure(
            category="tooling",
            summary=f"tool {tool_name} returned an error ({error_type or 'error'})",
            signature=_normalize_signature(str(tool_name), error_type, "tool_error"),
            snippet=text[:2000],
        )

    def on_session_end(**_kw) -> None:
        """Bounded draft refresh at the session boundary: ONE native ``list --json``
        call, registered only when ``draft_rules`` is on. Never raises and never
        prints; the outcome is recorded in plugin state, so a run that wrote
        nothing is distinguishable from one that could not run."""
        try:
            outcome = _collect_drafts()
        except Exception as exc:  # noqa: BLE001 - observer at a session boundary:
            # nothing here may raise; the outcome is recorded instead of logged away
            outcome = {"success": False,
                       "error": f"drafts: {type(exc).__name__}: {exc}"}
        state.set("drafts_last", {**outcome,
                                  "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    ctx.register_tool(
        name="papercuts",
        toolset="papercuts",
        schema={
            "name": "papercuts",
            "description": (
                "File and triage tool grievances via the native papercuts binary "
                "(it owns dedup, recurrence counts and status). report: typed "
                "category/severity/summary + expected/observed/tool_versions/repro/reference "
                "(mapped into native context) and invocation_result (natively redacted, "
                "never raw). list --json shows recurrence in the count field — triage "
                "by actual recurrence, not invented cost estimates. report accepts observed "
                "time_lost_seconds; list --sort impact ranks by severity, recurrence and "
                "reported time lost (unknown timing sorts last, never as zero). ack/resolve/stats "
                "forward natively. friction: counters for the gate-friction sink "
                "(continuation/loop-guard/chain-compaction refusals) — per source, per "
                "reason, and whether the sink is installed. drafts: write inert rule "
                "drafts (enabled: false) for papercuts at or above the configured "
                "recurrence threshold; never overwrites a draft a human may have edited."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": list(_MODES)},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "summary": {"type": "string", "description": "Human-readable title"},
                    "expected": {"type": "string"},
                    "observed": {"type": "string"},
                    "tool_versions": {"type": "string"},
                    "invocation_result": {"type": "string",
                                          "description": "Redacted invocation/result excerpt"},
                    "repro": {"type": "string"},
                    "reference": {"type": "string"},
                    "repo": {"type": "string"},
                    "blame": {"type": "string", "description": "Optional sig:<signature> dedup tag"},
                    "id": {"type": "string", "description": "Record id for ack/resolve"},
                    "json": {"type": "boolean", "description": "list: native --json (default true)"},
                    "status": {"type": "string", "enum": ["open", "ack", "resolved"]},
                    "limit": {"type": "integer"},
                    "time_lost_seconds": {"type": "number",
                                          "description": "Observed seconds lost (report); finite >=0; unknown = omit"},
                    "sort": {"type": "string", "enum": ["impact"],
                             "description": "list: order by severity, recurrence, reported time lost"},
                },
                "required": ["mode"],
            },
        },
        handler=lambda args, **kw: _report(args or {}, **kw),
        check_fn=available,
        description="File tool grievances (papercuts), typed and redacted.",
    )
    ctx.register_hook("post_tool_directive", on_post_tool_directive)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    if draft_rules:
        # Nothing self-activates: the session-boundary draft run is opt-in config.
        ctx.register_hook("on_session_end", on_session_end)
    ctx.register_cli_command(
        name="papercuts",
        help="File or triage tool grievances (papercuts).",
        setup_fn=lambda sub: sub.add_argument("rest", nargs="*",
                                              help="native papercuts args, e.g. report --category tooling --severity low --summary x"),
        handler_fn=lambda args: _passthrough([binary, *(args.rest or ["list"])]),
    )
    logger.info("papercuts: bridge registered -> %s (auto_capture=%s, daily cap %d, "
                "gate_friction=%s, draft_rules=%s after=%d dir=%s)",
                binary, auto_capture, AUTO_DAILY_CAP, gate_friction, draft_rules,
                draft_rule_after, draft_rule_dir)
