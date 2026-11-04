"""intake — registration only; the pipeline lives in core.py (host-free).

Settings — binary, db_path, endpoint, model, the key's env name, caps and
timeout — are resolved on EVERY tool/CLI call through ``ctx.get_config``,
never captured in ``register()``: the native manager is shared across
multiplexed profiles and config reads are context-local (the covey-tasks
discipline). The Covey transport is the host bridge
(``plugins.covey.lib.covey_tool``); binary and db_path are passed explicitly
on every call, never ambient. The bearer key is read from the environment at
call time and never stored, logged or echoed; the session token likewise
never appears in a report.

Every failure path is a structured ``{"status", "submitted", "failed",
"reason"}`` result, never an exception; the CLI maps store_unavailable /
not_configured / error to exit 1 so an operator piping transcriptions can
tell a misconfigured run from a quiet one.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from . import core

# Host-bundled Covey bridge; on an unsupported host both stay None and every
# call surfaces a clear store_unavailable result instead of importing hard.
try:
    from plugins.covey.lib.covey_tool import run_covey_command, resolve_covey_binary
except ImportError:  # surfaced per call as store_unavailable
    run_covey_command = None  # type: ignore[assignment]
    resolve_covey_binary = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

COVEY_CALL_TIMEOUT = 10.0  # per covey command; store calls are local and quick

TOOL_SCHEMA = {
    "name": "intake",
    "description": (
        "Triage arbitrary text (or a text file) into durable Covey follow-up "
        "tasks in the profile's global worklist. Extracts ONLY actions the "
        "owner of the material must do or explicitly committed to — never "
        "fabricated work. dry_run returns the extracted items without "
        "touching the store."),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string",
                     "description": "Arbitrary material to triage (transcript, message, notes). Provide text OR path."},
            "path": {"type": "string",
                     "description": "Regular file to read instead of text; the same max_chars clip is applied."},
            "source": {"type": "string",
                       "description": "Provenance footer stamped onto every submitted task (default: tool)."},
            "dry_run": {"type": "boolean",
                        "description": "Extract and return the items only; the Covey store is never touched."},
        },
    },
}


def _as_int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _error(reason):
    return {"status": "error", "submitted": [], "failed": [], "reason": reason}


def register(ctx):
    def run_intake(args):
        # Fresh on every call: profiles multiplex through one manager.
        binary_cfg = str(ctx.get_config("binary", "covey") or "covey")
        db_path = str(ctx.get_config("db_path", "") or "")
        endpoint = str(ctx.get_config("endpoint", "http://127.0.0.1:8102") or "")
        model = str(ctx.get_config("served_model", "qwen3.8-flash-next") or "")
        api_key_env = str(ctx.get_config("api_key_env", "CLASSIFIER_API_KEY") or "CLASSIFIER_API_KEY")
        max_chars = _as_int(ctx.get_config("max_chars", 60000), 60000)
        max_items = _as_int(ctx.get_config("max_items", core.DEFAULT_MAX_ITEMS), core.DEFAULT_MAX_ITEMS)
        timeout = _as_float(ctx.get_config("timeout", 30.0), 30.0)
        classifier_endpoint = str(ctx.get_config("classifier_endpoint", "") or "")
        classifier_timeout = _as_float(ctx.get_config("classifier_timeout", 1.5), 1.5)
        grounding_max = max(0, _as_int(ctx.get_config("grounding_max", 100), 100))
        decision_log = str(ctx.get_config("decision_log", "") or "")
        api_key = os.environ.get(api_key_env, "").strip()  # env at call time, never stored

        text = args.get("text")
        path = args.get("path")
        text = text if isinstance(text, str) else None
        path = path if isinstance(path, str) else None
        if (text is None) == (path is None):
            return _error("exactly one of text or path is required")
        if path is not None:
            if not os.path.isfile(path):  # regular files only
                return _error("not_a_regular_file")
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError:
                return _error("unreadable_input_file")
        if not text.strip():
            # Empty or whitespace-only material is quiet input: an ok that
            # files nothing. An empty stdin pipe says exactly what it means
            # instead of being misreported as "no input given", and neither
            # the model nor the store is bothered.
            return {"status": "ok", "submitted": [], "existing": [],
                    "failed": [], "reason": None}
        source = str(args.get("source") or "tool")

        def call_covey(command_args):
            # Explicit binary+db on every call; the bridge never sees ambient config.
            return run_covey_command(command_args, db_path=db_path,
                                     binary=resolve_covey_binary(binary_cfg),
                                     timeout=COVEY_CALL_TIMEOUT)

        if args.get("dry_run"):  # extraction preview only; the store is never touched,
            # so a preview needs NO store config — only the model does. The preview
            # shows what extraction yields (urgency annotation included); unlike a
            # real run it is NOT deduped against the store, so already-filed items
            # appear here too.
            items, reason = core.extract(core.clip(text, max_chars), endpoint=endpoint,
                                         model=model, api_key=api_key, timeout=timeout,
                                         max_items=max_items)
            if reason is not None:
                status = "not_configured" if reason == "not_configured" else "model_unavailable"
                return {"status": status, "items": [], "submitted": [], "existing": [],
                        "failed": [], "reason": reason}
            core.annotate_urgency(items, classifier_endpoint=classifier_endpoint or endpoint,
                                  model=model, api_key=api_key, timeout=classifier_timeout)
            return {"status": "ok", "items": items, "submitted": [], "existing": [],
                    "failed": [], "reason": None}

        if run_covey_command is None or resolve_covey_binary is None:
            return {"status": "store_unavailable", "submitted": [], "existing": [],
                    "failed": [],
                    "reason": "covey bridge (plugins.covey.lib.covey_tool) is unavailable on this host"}
        if not os.path.isabs(db_path):
            return {"status": "store_unavailable", "submitted": [], "existing": [],
                    "failed": [],
                    "reason": "db_path must be the ABSOLUTE path of the profile's global covey DB "
                              "— set it to the same path the covey-tasks plugin uses"}

        report = core.ingest(text, source=source, endpoint=endpoint, model=model,
                             api_key=api_key, timeout=timeout, max_chars=max_chars,
                             max_items=max_items, call_covey=call_covey,
                             classifier_endpoint=classifier_endpoint,
                             classifier_timeout=classifier_timeout,
                             grounding_max=grounding_max)
        if report.get("status") in ("ok", "partial") and (
                report["submitted"] or report["existing"] or report["failed"]):
            # Append-only decision log: one JSON line per run that filed,
            # matched or lost something, so an operator can audit what a run
            # decided. Quiet runs (nothing extracted) log nothing — the log
            # records decisions, not material volume. Logging never blocks
            # intake: OSError (unwritable) AND ValueError (an unencodable
            # surrogate in source bytes from argv) are debug notes and the
            # report is returned unaltered; dry_run never reaches here.
            log_path = decision_log or db_path + ".intake.jsonl"
            try:
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(
                        {"ts": datetime.now(timezone.utc).isoformat(),
                         "source": source, "status": report["status"],
                         "submitted": report["submitted"],
                         "existing": report["existing"],
                         "failed": report["failed"]},
                        ensure_ascii=False) + "\n")
            except (OSError, ValueError):
                logger.debug("intake decision log unwritable at %s", log_path)
        return report

    def intake(args, **_kw):
        if not isinstance(args, dict):
            return json.dumps(_error("arguments must be an object"))
        return json.dumps(run_intake(args), ensure_ascii=False)

    ctx.register_tool(
        name="intake", toolset="intake", schema=TOOL_SCHEMA,
        handler=intake,
        description="Triage arbitrary text into Covey follow-up tasks (dry_run previews without submitting).")

    # ---------------------------------------------------------------- intake CLI
    def intake_setup(sub):
        sub.add_argument("file", help="text file to triage, or - for stdin")
        sub.add_argument("--source", default="cli",
                         help="provenance footer stamped onto every submitted task (default: cli)")
        sub.add_argument("--dry-run", action="store_true",
                         help="print the extracted items only; never touch the covey store")

    def intake_handler(args):
        if args.file == "-":
            report = run_intake({"text": sys.stdin.read(), "source": args.source,
                                 "dry_run": bool(args.dry_run)})
        else:
            report = run_intake({"path": args.file, "source": args.source,
                                 "dry_run": bool(args.dry_run)})
        print(json.dumps(report, ensure_ascii=False))
        # A misconfigured run must be non-zero for anyone piping transcriptions.
        return 1 if report.get("status") in ("store_unavailable", "not_configured",
                                             "error") else 0

    ctx.register_cli_command(
        name="intake",
        help="Triage a text file (or stdin) into Covey follow-up tasks in the profile's global worklist.",
        setup_fn=intake_setup, handler_fn=intake_handler)
