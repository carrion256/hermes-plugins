"""Meta classification plugin: droid's classifier decisions on Hermes hooks.

Forked from auto-review-classifier (redacted egress, resolved targets,
operation-bound approvals) and merged with the decisions reverse-engineered
from droid 0.244.0 (droid repo notes/arch/classifiers.md):

- Execute risk (`pre_tool_call`): droid's Jev LOW/MEDIUM/HIGH criteria or the
  luna off/low/medium/high tier ladder replace the binary safe/risky head.
  Native permissions stay authoritative; a tier at/above `approve_from` asks
  (or blocks under policy deny).
- Droid Shield (`pre_tool_call` on `git commit`/`git push`): staged/unpushed
  added lines are scrubbed with native redaction; scrubbed-value lines are
  findings (downgrade question: block at P(B)>=0.3775 else warn), secret-named
  lines without a finding are risk contexts (risk question: warn at P(B)>=0.95).
  Unlike droid, the plugin applies the threshold itself, so each screened line
  records its P(B) beside the threshold used and either can be fitted to real
  data. Fail-closed exactly like droid: downgrade failure blocks, risk failure
  is silent.
- Local Signals (`pre_llm_call` snapshot + session triggers): Factory-Judge's
  intent/friction/expertise enum heads in one request, persisted as JSONL
  analytics. Unlike droid, unusable replies are recorded as errors, never as
  plausible defaults.
- `decision_model` tool: agent-facing Jev evaluate request -> labels.
- `hermes meta-classifier-replay [--run]`: replays droid's captured payloads.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shlex
import stat
import subprocess
import uuid
from pathlib import Path

from . import addons, replay, schemas
from . import browser_decision as browser_decision_mod
from .client import Classifier
from .text import canonical as _canonical, egress as _egress, render_transcript, window as _window

logger = logging.getLogger(__name__)
_CLASSIFY_TIMEOUT = 2.5
_SKIP = {"read_file", "search_files", "web_search", "web_extract", "skill_view",
         "skills_list", "ha_get_state", "ha_list_entities", "ha_list_services"}
_POLLING = {"list", "poll", "log", "wait"}
_PATHS = ("path", "file", "file_path", "target", "notebook_path", "workdir", "cwd")
_PRIORITY = {"command", "cmd", "script", "action", "env", "environment", "force",
             "recursive", "url", "endpoint", "patch", *_PATHS}
_MAX_ARGS = 131072
_MAX_SUMMARY = 16000
_CREDENTIAL_NAME = re.compile(
    r"(?:api[_-]?key|access[_-]?key|private[_-]?key|auth[_-]?token|token|secret|"
    r"password|passwd|passphrase|credential|authorization)s?", re.I)
# Droid Shield trigger: `git [global options] commit|push` anywhere in the command line.
_GIT_WRITE = re.compile(r"\bgit(?:\s+-\S+(?:\s+[^-\s][^\s|;&]*)?)*\s+(commit|push)\b")
_SHIELD_CAP = 16
_TRIGGERS = {"session_end", "turn_end"}


def _mask_flag_values(text):
    """Mask flat shell flags/assignments; abstain on nested credential syntax.

    Display-only tokenization, never execution. Do not pretend shlex parses
    nested shell/Python programs: unknown credential contexts refuse egress.
    """
    if not _CREDENTIAL_NAME.search(text) or _CREDENTIAL_NAME.fullmatch(text):
        return text
    words = shlex.split(text, comments=False, posix=True)
    changed = False
    for index, word in enumerate(words):
        flag, separator, _value = word.partition("=")
        if flag.startswith("--") and _CREDENTIAL_NAME.fullmatch(flag[2:]):
            if separator:
                words[index] = flag + "=[redacted]"
            else:
                if index + 1 == len(words):
                    raise ValueError("credential flag has no value")
                words[index + 1] = "[redacted]"
            changed = True
        elif separator and _CREDENTIAL_NAME.search(flag) and re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", flag):
            words[index] = flag + "=[redacted]"
            changed = True
        elif _CREDENTIAL_NAME.search(word):
            raise ValueError("unparsed credential context")
    return shlex.join(words) if changed else text


def _context(task_id):
    from tools.file_tools_paths import _authoritative_workspace_root, _terminal_env_type_for_task
    backend = _terminal_env_type_for_task(task_id)
    workspace = _authoritative_workspace_root(task_id)
    if backend == "local" and workspace:
        workspace = str(Path(workspace).resolve())
    return {"backend": backend or "unknown", "workspace": workspace or "unknown"}


def _targets(tool, args, task_id, context):
    from tools.file_tools_paths import _resolve_path_for_task
    references = [(key, args[key]) for key in _PATHS if key in args]
    unknown = []
    if tool == "patch" and args.get("mode") == "patch":
        from tools.file_tools import _collect_v4a_header_paths
        patch = args.get("patch")
        if isinstance(patch, str) and len(patch) <= _MAX_ARGS:
            parsed = _collect_v4a_header_paths(patch)
            if isinstance(parsed, tuple):
                references.extend(("patch_header", path) for path in parsed[0])
            else:
                unknown.append("invalid_patch_headers")
        else:
            unknown.append("missing_or_oversized_patch")
    if len(references) > 32:
        unknown.append("target_limit")
    if tool in {"patch", "write_file"} and not references:
        unknown.append("missing_target")
    targets = []
    for argument, requested in references[:32]:
        if not isinstance(requested, str) or not requested or len(requested) > 4096 or "\0" in requested:
            unknown.append("invalid_target")
            continue
        row = {"argument": argument, "requested": requested, "resolved": None, "identity": None}
        if context["backend"] != "local":
            # SSH paths, like container paths, must never dereference host files.
            row["status"] = "remote_unresolved"
            unknown.append("remote_targets")
        elif context["workspace"] == "unknown":
            row["status"] = "workspace_unknown"
            unknown.append("workspace_unknown")
        else:
            try:
                target = Path(_resolve_path_for_task(requested, task_id))
                row["resolved"] = str(target)
                metadata = target.stat()
                row["identity"] = {"device": metadata.st_dev, "inode": metadata.st_ino,
                                   "mode": metadata.st_mode, "ctime_ns": metadata.st_ctime_ns}
                row["status"] = "existing"
                row["outside_workspace"] = not target.is_relative_to(context["workspace"])
                if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                    unknown.append("special_target")
            except (OSError, ValueError, RuntimeError):
                row["status"] = "missing_or_unresolved"
                unknown.append("missing_or_unresolved_target")
        targets.append(row)
    return targets, unknown


def _snapshot(tool, args, task_id):
    context = _context(task_id)
    targets, unknown = _targets(tool, args, task_id, context)
    if context["backend"] != "local":
        unknown.append("backend_environment_unknown")
    if context["workspace"] == "unknown":
        unknown.append("workspace_unknown")
    action = args.get("action") if isinstance(args.get("action"), str) else tool
    classes = []
    if tool in {"write_file", "patch"}:
        classes.append("file_mutation")
        if tool == "patch" and isinstance(args.get("patch"), str):
            if re.search(r"^\*\*\* Delete File:", args["patch"], re.M):
                classes.append("delete")
            if re.search(r"^\*\*\* Move File:", args["patch"], re.M):
                classes.append("move")
    elif tool in {"terminal", "execute_code"}:
        classes.append("command_execution")
        # Shell/code semantics and inherited environment aren't inferred from keywords.
        unknown.extend(["command_effects_and_targets_unknown", "inherited_environment_unknown"])
    elif tool == "process_manage":
        classes.append("process_mutation")
        unknown.append("process_identity_unknown")
    else:
        classes.append("unknown")
        unknown.append("unrecognized_tool_effects")
    return {"tool": tool, "action": action, **context, "targets": targets,
            "effect_classes": classes, "reversibility": "unknown",
            "environment": {"inherited": "unknown", "supplied": any(key in args for key in ("env", "environment"))},
            "unknown": sorted(set(unknown)), "arguments": args}


def _outbound(summary):
    """Mask typed values, force native scalar redaction, then bound the summary.

    Returns (safe_summary_dict, incomplete). The dict is the `pendingCommand`
    half of the Jev state; the caller serializes it.
    """
    if len(_canonical(summary)) > _MAX_ARGS:
        raise ValueError("input limit")
    changed = [False]

    def redact_scalar(text):
        safe = _egress(_mask_flag_values(text))
        changed[0] |= safe != text
        return safe

    def redact_value(value, depth=0):
        if depth > 12:
            raise ValueError("redaction depth limit")
        if isinstance(value, str):
            return redact_scalar(value)
        if isinstance(value, list):
            return [redact_value(item, depth + 1) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                safe_key = redact_scalar(key)
                if safe_key in result:
                    raise ValueError("redacted key collision")
                if _CREDENTIAL_NAME.search(key):
                    changed[0] = True
                    result[safe_key] = redact_scalar("[redacted]")
                else:
                    result[safe_key] = redact_value(item, depth + 1)
            return result
        return value

    safe = redact_value(summary)
    omitted = [0]
    nodes = [0]

    def bounded(value, depth=0):
        nodes[0] += 1
        if nodes[0] > 160 or depth > 5:
            omitted[0] += 1
            return "[omitted]"
        if isinstance(value, str):
            if len(value) > 2048:
                omitted[0] += 1
                return value[:2048] + "[truncated]"
            return value
        if isinstance(value, dict):
            keys = sorted(value, key=lambda k: (k not in _PRIORITY, k))
            omitted[0] += max(0, len(keys) - 48)
            result = {}
            for key in keys[:48]:
                if len(key) > 128:
                    omitted[0] += 1
                    continue
                result[key] = bounded(value[key], depth + 1)
            return result
        if isinstance(value, list):
            omitted[0] += max(0, len(value) - 32)
            return [bounded(item, depth + 1) for item in value[:32]]
        return value

    safe["arguments"] = bounded(safe["arguments"])
    # Budget arbitrary argument fields after priority fields; never cut serialized JSON.
    while len(_canonical(safe)) > _MAX_SUMMARY - 512 and safe["arguments"]:
        key = next(reversed(safe["arguments"]))
        safe["arguments"].pop(key)
        omitted[0] += 1
    safe["omissions"] = omitted[0]
    if omitted[0]:
        safe["unknown"].append("argument_context_omitted")
    if changed[0]:
        safe["unknown"].append("secret_values_redacted")
    if len(_canonical(safe)) > _MAX_SUMMARY:
        raise ValueError("summary limit")
    return safe, bool(safe["unknown"])


# ---------------------------------------------------------------- Droid Shield
def _has_remote_refs(workspace):
    """True when the repo knows any remote-tracking ref (--not --remotes is meaningless
    without one: it succeeds with empty output, which reads as 'nothing to screen')."""
    try:
        run = subprocess.run(["git", "for-each-ref", "--count=1", "--format=%(refname)",
                              "refs/remotes"], cwd=workspace, capture_output=True,
                             text=True, errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return run.returncode == 0 and bool(run.stdout.strip())


def _is_repo(workspace):
    """True when workspace is inside a git work tree (.git is a file in a worktree)."""
    try:
        run = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=workspace,
                             capture_output=True, text=True, errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return run.returncode == 0


def _git_diff(workspace, mode):
    base = ["git", "-c", "core.quotepath=off", "diff", "--no-color", "--unified=1", "--no-ext-diff"]
    if not _is_repo(workspace):
        return ""  # nothing outside a repository can be published by a git write
    if mode == "push":
        # @{upstream} covers the tracked branch; @{push} covers push.default with no upstream;
        # --not --remotes covers a branch no remote has seen. There is deliberately no --cached
        # fallback: an empty index says nothing about what a push publishes, and treating it as
        # clean is how unscreened commits were published. An unknowable range raises, so the
        # operator sees shield=unknown instead of a silent pass.
        attempts = [["@{upstream}...HEAD"], ["@{push}...HEAD"]]
        if _has_remote_refs(workspace):
            attempts.append(["--not", "--remotes"])
    else:
        # The worktree, not the index: `git commit -a`, `-p` and a pathspec all stage at commit
        # time, after this hook has run, so --cached was empty for exactly the commit forms
        # that publish the most work. Over-screening costs a review prompt; under-screening
        # commits the secret. HEAD is unborn in a repository with no commits yet, where the
        # index is the only meaningful comparison.
        attempts = [["HEAD"], ["--cached"]]
    for extra in attempts:
        try:
            run = subprocess.run(base + extra, cwd=workspace, capture_output=True, text=True,
                                 errors="replace", timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"git diff failed: {type(exc).__name__}") from exc
        if run.returncode == 0:
            return run.stdout
    raise ValueError("git diff failed")


def shield_contexts(diff):
    """Scanner contexts from a unified diff: one per added line that redaction
    scrubs (finding -> downgrade) or that names a secret (risk). Lines are the
    scrubbed window around the focus line, as droid sends them."""
    contexts, path, lineno, hunk = [], None, 0, []

    def flush():
        for index, (number, text, added) in enumerate(hunk):
            if not added:
                continue
            scrubbed = _egress(text)
            if scrubbed != text:
                kind = "downgrade"
            elif _CREDENTIAL_NAME.search(text):
                kind = "risk"
            else:
                continue
            window = hunk[max(0, index - 1):index + 2]
            contexts.append({
                "kind": kind, "location": f"{path}:{number}",
                "payload": {"extension": Path(path or "").suffix.lstrip(".") or "txt",
                            "lines": [_egress(t) for _n, t, _a in window],
                            "focus_line": index - max(0, index - 1)},
            })
        hunk.clear()

    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            flush()
            path = raw[4:].removeprefix("b/") if raw[4:] != "/dev/null" else None
        elif raw.startswith("@@"):
            flush()
            match = re.match(r"@@ -\S+ \+(\d+)", raw)
            lineno = int(match.group(1)) if match else 1
        elif raw.startswith("+") and not raw.startswith("+++"):
            hunk.append((lineno, raw[1:], True))
            lineno += 1
        elif raw.startswith(" "):
            hunk.append((lineno, raw[1:], False))
            lineno += 1
    flush()
    return contexts


def register(ctx):
    classifier = Classifier(ctx, timeout=_CLASSIFY_TIMEOUT)
    scale = str(ctx.get_config("risk_scale", "jev") or "jev").lower()
    tiers = schemas.RISK_SCALES.get(scale) or schemas.RISK_SCALES["jev"]
    scale = scale if scale in schemas.RISK_SCALES else "jev"
    approve_from = str(ctx.get_config("approve_from", "high") or "high").lower()
    approve_index = tiers.index(approve_from) if approve_from in tiers else len(tiers) - 1
    risk_schema = schemas.risk_schema(scale)
    transcript_chars = int(ctx.get_config("transcript_chars", 4000) or 0)
    history = {}  # session_id -> latest conversation_history (pre_llm_call snapshot)
    signals_seen = {}  # session_id -> digest of the last classified rendering

    def policy():
        return str(ctx.get_config("policy", "ask") or "ask").lower()

    def transcript_tail(session_id):
        messages = history.get(session_id)
        if not messages or transcript_chars <= 0:
            return None
        return {"rendered": _window(_egress(render_transcript(messages)), transcript_chars)}

    # ------------------------------------------------------------ Droid Shield
    def shield(command, task_id, tool_call_id):
        """None (nothing to say), a directive, or a status dict (unscreened)."""
        match = _GIT_WRITE.search(command)
        if not match or not ctx.get_config("shield", True):
            return None
        context = _context(task_id)
        if context["backend"] != "local" or context["workspace"] == "unknown":
            return {"shield": {"status": "unknown", "reason": "workspace_unavailable"}}
        try:
            diff = _git_diff(context["workspace"], match.group(1))
            contexts = shield_contexts(diff)
        except ValueError as exc:
            if "redaction" in str(exc):
                return {"action": "block", "message": "meta-classifier shield: native redaction "
                        "unavailable; cannot scrub staged secrets before review."}
            if match.group(1) == "push":
                # Nothing measurable describes what this push publishes (no upstream, no
                # @{push}, no remote-tracking ref). An empty index is not evidence of a clean
                # push, so refuse rather than let unscreened commits leave the machine.
                return {"action": "block", "message": "meta-classifier shield: cannot determine "
                        "which commits this push publishes; push to a fetched remote (or an "
                        "explicit remote/branch) so the range can be screened."}
            return {"shield": {"status": "unknown", "reason": str(exc)}}
        if not contexts:
            return None
        downgrade = [c for c in contexts if c["kind"] == "downgrade"]
        risk = [c for c in contexts if c["kind"] == "risk"][:_SHIELD_CAP]
        if len(downgrade) > _SHIELD_CAP:
            return {"action": "block", "message": f"meta-classifier shield: {len(downgrade)} "
                    "secret-shaped lines staged; review and unstage them before committing."}
        blocking, warnings, rows = [], [], []

        def probability(kind, context_row):
            """(P(B), threshold) for one staged line; P(B) is None when the answer
            carries no usable number. The service applies no threshold on this path,
            so the number and the threshold it is compared against are both recorded
            below -- that pairing is what lets a real threshold be fitted to real data."""
            questions, threshold = schemas.shield_question(kind)
            envelope = classifier.ask(_canonical(context_row["payload"]), questions)
            answers = envelope.get("answers") if isinstance(envelope, dict) else None
            answer = answers.get("verdict") if isinstance(answers, dict) else None
            options = answer.get("probabilities") if isinstance(answer, dict) else None
            value = options.get("B") if isinstance(options, dict) else None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, threshold
            value = float(value)
            # NaN and out-of-range fail the comparison, so they are no answer at all.
            return (value if 0.0 <= value <= 1.0 else None), threshold

        for context_row in downgrade:
            score, threshold = probability("downgrade", context_row)
            blocked = score is None or score >= threshold  # fail-closed: unavailable -> block
            rows.append({"location": context_row["location"], "kind": "downgrade",
                         "probability": score, "threshold_used": threshold,
                         "decision": "block" if blocked else "warn"})
            (blocking if blocked else warnings).append(context_row["location"])
        for context_row in risk:
            score, threshold = probability("risk", context_row)
            warned = score is not None and score >= threshold  # risk failure -> no warning
            rows.append({"location": context_row["location"], "kind": "risk",
                         "probability": score, "threshold_used": threshold,
                         "decision": "warn" if warned else "none"})
            if warned:
                warnings.append(context_row["location"])
        if rows:
            append_record({"shieldedAt": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                           "command": match.group(1), "workspace": context["workspace"], "lines": rows})
        if blocking:
            return {"action": "block", "message": "meta-classifier shield: likely real secrets in the "
                    f"diff ({', '.join(blocking)}). STOP: do not retry this command; remove the "
                    "secret material and rotate anything already exposed."}
        if warnings:
            digest = hashlib.sha256(_canonical([command, task_id, tool_call_id or uuid.uuid4().hex,
                                                sorted(warnings)]).encode()).hexdigest()
            return {"action": "block" if policy() == "deny" else "approve",
                    "message": "meta-classifier shield: possible hardcoded credential values "
                    f"({', '.join(warnings)}); human review required.",
                    "rule_key": f"meta-classifier:shield:{digest}"}
        return None

    # ------------------------------------------------------------ Execute risk
    def gate(tool_name="", args=None, task_id="", tool_call_id="", session_id="", turn_id="", **_kw):
        if tool_name in _SKIP or (tool_name == "process_manage" and isinstance(args, dict)
                                  and args.get("action") in _POLLING):
            return None

        def status(reason, **extra):
            # Metadata only (the dispatcher ignores returns without an
            # action=block/approve): screening was attempted but NOT fulfilled.
            return {"risk_review": {"status": "unknown", "reason": reason, "screened": False, **extra}}

        if not isinstance(args, dict):
            return status("invalid_arguments")
        shield_result = None
        if tool_name == "terminal" and isinstance(args.get("command"), str):
            shield_result = shield(args["command"], task_id, tool_call_id)
            if shield_result and shield_result.get("action") == "block":
                return shield_result
        try:
            if len(_canonical(args)) > _MAX_ARGS:
                return status("input_limit")
            summary = _snapshot(tool_name, args, task_id)
            safe, incomplete = _outbound(summary)
            state = {"pendingCommand": safe}
            tail = transcript_tail(session_id)
            if tail:
                state["sessionTranscript"] = tail
        except Exception:
            return status("context_or_redaction_unavailable")
        got = classifier.classify(_canonical(state), risk_schema)
        tier = got.get("riskLevel") if isinstance(got, dict) else None
        if tier not in tiers:
            if got is not None:
                logger.warning("meta-classifier: unusable riskLevel %r", tier)
            return status("classifier_unavailable_or_invalid")
        try:
            if _snapshot(tool_name, args, task_id) != summary:
                return status("target_or_context_changed_during_review")
        except Exception:
            return status("context_recheck_unavailable")
        if tiers.index(tier) < approve_index:
            if shield_result and shield_result.get("action"):
                return shield_result
            if incomplete:
                return status("incomplete_context", riskLevel=tier, scale=scale)
            return None
        material = [tool_name, task_id, session_id, turn_id, summary["workspace"],
                    summary["backend"], summary["targets"], args]
        if incomplete:
            # Unresolved command/remote targets cannot mint a reusable approval.
            material.append(tool_call_id or uuid.uuid4().hex)
        digest = hashlib.sha256(_canonical(material).encode()).hexdigest()
        identity = _canonical(summary["targets"]) + "\0" + _canonical(summary["workspace"])

        def revalidate(final_args):
            # Read-only, post-approval identity check with FINAL args; the
            # host calls it synchronously in the approval context.
            try:
                fresh = _snapshot(tool_name, final_args, task_id)
                return _canonical(fresh["targets"]) + "\0" + _canonical(fresh["workspace"]) == identity
            except Exception:
                return False

        message = f"meta-classifier: riskLevel={tier} ({scale} scale); human review required."
        if shield_result and shield_result.get("action"):
            message = shield_result["message"] + " " + message
        directive = {"action": "block" if policy() == "deny" else "approve", "message": message,
                     "rule_key": f"meta-classifier:{tool_name}:{digest}"}
        if policy() != "deny":
            directive["revalidate"] = revalidate
        return directive

    ctx.register_hook("pre_tool_call", gate)

    # ------------------------------------------------------------ Local Signals
    def triggers():
        raw = ctx.get_config("signals_triggers", ["session_end"]) or []
        return {str(t) for t in raw if str(t) in _TRIGGERS} if ctx.get_config("signals", False) else set()

    def signals_path():
        configured = ctx.get_config("signals_path", "")
        if configured:
            return Path(str(configured)).expanduser()
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / "logs" / "meta-classifier-signals.jsonl"

    def classify_signals(session_id, trigger):
        messages = history.get(session_id)
        if not messages:
            return None
        record = {"classifiedAt": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                  "sessionId": session_id, "triggerReason": trigger,
                  "messageRange": {"count": len(messages)}}
        try:
            rendered = _window(_egress(render_transcript(messages)),
                               int(ctx.get_config("signals_chars", 60000) or 60000))
        except ValueError as exc:
            record["error"] = str(exc)
            rendered = None
        if rendered is not None:
            digest = hashlib.sha256(rendered.encode()).hexdigest()
            if signals_seen.get(session_id) == digest:
                return None  # unchanged since the last successful classification
            record["windowMetrics"] = {"chars": len(rendered)}
            got = classifier.classify(rendered, schemas.SIGNALS_SCHEMA, timeout=15)
            if isinstance(got, dict):
                # Only a completed classification may suppress the next trigger. Caching the
                # digest before the call turned one transient outage into a permanent skip of
                # that transcript, with no error row and no retry after the cooldown expired.
                signals_seen[session_id] = digest
                record["classification"] = got
            else:
                record["error"] = "classifier_unavailable_or_invalid"
        append_record(record)
        return record

    def append_record(record):
        try:
            path = signals_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("meta-classifier: cannot persist signals: %s", exc)

    enabled_addons = addons.register_addons(ctx, classifier, append_record, history)

    def pre_llm_call(session_id="", conversation_history=None, **_kw):
        if session_id and isinstance(conversation_history, list):
            history[str(session_id)] = conversation_history
        return None

    def on_turn_end(session_id="", **_kw):
        if "turn_end" in triggers():
            classify_signals(str(session_id), "turn_end")

    def on_boundary(session_id="", old_session_id="", new_session_id="", session_key="", **_kw):
        # The host rotates with on_session_reset(session_id=<new>, old_session_id=...,
        # new_session_id=...) and interrupts with agent_loop_stopped(session_key=...); it
        # never sent the previous_session_id this keyed on, so the outgoing session's
        # transcript and digest were never dropped and grew for the life of the process.
        for identity in (session_id, old_session_id, new_session_id, session_key):
            identity = str(identity or "")
            if not identity:
                continue
            if "session_end" in triggers():
                classify_signals(identity, "session_end")
            history.pop(identity, None)
            signals_seen.pop(identity, None)

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_end", on_turn_end)
    ctx.register_hook("on_session_finalize", on_boundary)
    ctx.register_hook("on_session_reset", on_boundary)

    # ------------------------------------------------------------ DecisionModel tool
    def decision_model(args, **_kw):
        try:
            request = {"state": (args or {}).get("state", ""), "questions": (args or {}).get("questions")}
            payload = schemas.decision_to_classify(request)
            text = _egress(payload["text"])
        except ValueError as exc:
            return json.dumps({"success": False, "error": str(exc)})
        got = classifier.classify(text, payload["schema"], timeout=30)
        if not isinstance(got, dict):
            return json.dumps({"success": False, "error": "classifier_unavailable"})
        return json.dumps({"success": True, "answers": schemas.decision_answers(request, got),
                           "note": "labels only: this service exposes no probabilities or confidence; "
                                   "treat every answer as uncertain evidence, not a decision."})

    key_env = str(ctx.get_config("api_key_env", "CLASSIFIER_API_KEY"))
    ctx.register_tool(
        name="decision_model", toolset="meta-classifier",
        schema={
            "name": "decision_model",
            "description": (
                "Cheap label classifier for classification, yes/no judgments and graded scoring, "
                "especially when the same judgment repeats over many items (call it per item). Input is a "
                "decision request: `state` (the evidence, string or object) and `questions` "
                "({id: {type: 'choice'|'noul'|'score', instructions?, criteria}}). choice criteria: "
                "{option: definition}; noul criteria: {true: ..., false: ...}; score criteria: ordered "
                "level descriptions (max 10). Returns one label per question and no probabilities; the "
                "state is redacted before egress and must not contain secrets."),
            "parameters": {
                "type": "object",
                "properties": {
                    "state": {"description": "Evidence to judge (string or JSON object)."},
                    "questions": {"type": "object", "description": "Question id -> {type, instructions, criteria}."},
                },
                "required": ["questions"],
            },
        },
        handler=decision_model,
        check_fn=lambda: bool(os.environ.get(key_env, "").strip()),
        description="Label classification, yes/no and graded scoring via the classifier service.",
    )

    # ------------------------------------------------------------ Browser decision (jev-ultrafast port)
    def browser_decision(args, **_kw):
        try:
            state, schema, meta = browser_decision_mod.build_request(
                (args or {}).get("goal", ""), (args or {}).get("elements"))
            text = _egress(_canonical(state))
        except (ValueError, TypeError) as exc:
            return json.dumps({"success": False, "error": str(exc)})
        got = classifier.classify(text, schema, timeout=30)
        if not isinstance(got, dict):
            return json.dumps({"success": False, "error": "classifier_unavailable"})
        decision, problems = browser_decision_mod.validate(got, meta)
        return json.dumps({"success": decision is not None, "decision": decision,
                           "offered": meta["operations"], "problems": problems,
                           "note": "labels only, no probabilities: this is a suggested next action, "
                                   "not a decision. Execute via bladebro and verify the resulting "
                                   "delta yourself; a wrong pick is recoverable, an unverified one is not."})

    ctx.register_tool(
        name="browser_decision", toolset="meta-classifier",
        schema={
            "name": "browser_decision",
            "description": ("Suggest the next browser action for a goal, jev-ultrafast style: ONE "
                            "classifier request returns an operation (click/type/select/scroll_*/"
                            "wait/done/blocked) plus the compatible target index (and option index "
                            "for dropdowns). Input: goal + the observed element table "
                            "([{index, role, name?, value?, state?, options?}...], from an accessibility "
                            "tree or bladebro 'see'); only elements you observed can be picked. "
                            "Suggestion only — the caller executes through the browser and verifies."),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "What the page should end up doing/showing."},
                    "elements": {"type": "array", "description":
                        "Observed controls: [{index:int, role, name?, value?, state?, options?:[str]}]."},
                },
                "required": ["goal", "elements"],
            },
        },
        handler=browser_decision,
        check_fn=lambda: bool(os.environ.get(key_env, "").strip()),
        description="Next browser action (operation + element index) from an observed table.",
    )

    # ------------------------------------------------------------ replay CLI
    def replay_setup(sub):
        sub.add_argument("--run", action="store_true", help="POST every payload to the configured endpoint")
        sub.add_argument("--captures", default=str(ctx.get_config("captures_dir", replay.DEFAULT_CAPTURES)),
                         help="droid notes/captured/classifiers directory")
        sub.add_argument("--cases", default=None,
                         help="JSONL of human-labelled cases {id, text, addon|schema, expect} instead of droid captures; "
                              "--run prints per-head agreement")

    def replay_handler(args):
        payloads = addons.load_cases(args.cases) if args.cases else replay.build_payloads(args.captures)
        if args.run:
            replay.run(payloads, Classifier(ctx, timeout=30, cooldown=0), print)
        else:
            replay.dry_run(payloads, print)
        return 0

    ctx.register_cli_command(
        name="meta-classifier-replay",
        help="Replay droid's captured classifier payloads or labelled cases (dry run, or --run against the service).",
        setup_fn=replay_setup, handler_fn=replay_handler)
    logger.info("meta-classifier: risk scale %s (approve from %s), shield %s, signals %s, addons %s",
                scale, tiers[approve_index], bool(ctx.get_config("shield", True)), sorted(triggers()),
                sorted(enabled_addons))
