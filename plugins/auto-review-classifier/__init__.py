"""Advisory effects screening; native permissions always remain authoritative.

Only native-redacted JSON reaches the classifier. Missing context is explicit;
'safe' on incomplete evidence is unknown. Approval keys bind exact arguments,
backend, task workspace and real target identities, rechecked after screening.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import re
import shlex
import stat
import os
import time
import urllib.request
import uuid

_CLASSIFY_TIMEOUT = 2.5
_COOLDOWN_SECONDS = 45
logger = logging.getLogger(__name__)
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


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


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
    """Mask typed values, force native scalar redaction, then bound and serialize."""
    from agent.redact import REDACTION_UNAVAILABLE, redact_for_egress
    if len(_canonical(summary)) > _MAX_ARGS:
        raise ValueError("input limit")
    changed = [False]

    def redact_scalar(text):
        masked = _mask_flag_values(text)
        safe = redact_for_egress(masked)
        if not isinstance(safe, str) or REDACTION_UNAVAILABLE in safe:
            raise ValueError("redaction unavailable")
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
    text = _canonical(safe)
    if len(text) > _MAX_SUMMARY:
        raise ValueError("summary limit")
    return text, bool(safe["unknown"])



def register(ctx):
    # Cooldown is a fixed blackout: recovery is re-attempted after it expires.
    down = {}  # endpoint -> monotonic deadline; a dead endpoint must not tax every tool call
    def classify(text):
        key = os.environ.get(ctx.get_config("api_key_env", "CLASSIFIER_API_KEY"), "")
        endpoint = str(ctx.get_config("endpoint", "http://192.168.50.71:8787") or "").rstrip("/")
        if down.get(endpoint, 0.0) > time.monotonic():
            return None
        if not key or not endpoint:
            return None
        request = urllib.request.Request(endpoint + "/classify",
                                         data=json.dumps({"text": text, "schema": {"risk": ["safe", "risky"]}}).encode(),
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(request, timeout=_CLASSIFY_TIMEOUT) as reply:
                data = json.loads(reply.read(16385))
            verdict = data.get("risk") if isinstance(data, dict) else None
            if verdict in ("safe", "risky"):
                return verdict
            logger.warning("auto-review classifier returned unusable verdict: %r", verdict)
        except Exception as exc:
            logger.warning("auto-review classifier unavailable: %s: %s",
                           type(exc).__name__, exc, exc_info=True)
        down[endpoint] = time.monotonic() + _COOLDOWN_SECONDS
        return None

    def gate(tool_name="", args=None, task_id="", tool_call_id="", session_id="", turn_id="", **_kw):
        if tool_name in _SKIP or (tool_name == "process_manage" and isinstance(args, dict) and args.get("action") in _POLLING):
            return None
        def status(reason):
            # Metadata only (the dispatcher ignores returns without an
            # action=block/approve): screening was attempted but NOT fulfilled.
            return {"risk_review": {"status": "unknown", "reason": reason,
                                     "screened": False}}
        if not isinstance(args, dict):
            return status("invalid_arguments")
        try:
            if len(_canonical(args)) > _MAX_ARGS:
                return status("input_limit")
            summary = _snapshot(tool_name, args, task_id)
            outbound, incomplete = _outbound(summary)
        except Exception:
            return status("context_or_redaction_unavailable")
        verdict = classify(outbound)
        if verdict is None:
            return status("classifier_unavailable_or_invalid")
        try:
            if _snapshot(tool_name, args, task_id) != summary:
                return status("target_or_context_changed_during_review")
        except Exception:
            return status("context_recheck_unavailable")
        if verdict == "safe":
            if incomplete:
                return status("incomplete_context")
            return None
        material = [tool_name, task_id, session_id, turn_id, summary["workspace"],
                    summary["backend"], summary["targets"], args]
        if incomplete:
            # Unresolved command/remote targets cannot mint a reusable approval.
            material.append(tool_call_id or uuid.uuid4().hex)
        digest = hashlib.sha256(_canonical(material).encode()).hexdigest()
        policy = str(ctx.get_config("policy", "ask") or "ask").lower()
        identity = _canonical(summary["targets"]) + "\0" + _canonical(summary["workspace"])

        def revalidate(final_args):
            # Read-only, post-approval identity check with FINAL args; the
            # host calls it synchronously in the approval context.
            try:
                fresh = _snapshot(tool_name, final_args, task_id)
                fresh_identity = _canonical(fresh["targets"]) + "\0" + _canonical(fresh["workspace"])
                return fresh_identity == identity
            except Exception:
                return False

        directive = {"action": "block" if policy == "deny" else "approve",
                     "message": "auto-review-classifier: operation classified risky; human review required.",
                     "rule_key": f"auto-review:{tool_name}:{digest}"}
        if policy != "deny":
            directive["revalidate"] = revalidate
        return directive

    ctx.register_hook("pre_tool_call", gate)
