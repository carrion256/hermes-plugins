"""Advisory effects screening; native permissions always remain authoritative.

Only native-redacted JSON reaches the classifier. Missing context is explicit;
'safe' on incomplete evidence is unknown. Approval keys bind exact arguments,
backend, task workspace and real target identities, rechecked after screening.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import logging
import re
import shlex
import stat
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol, cast

# Decision boundary on P(risky) from the classifier's choice head. 0.5 is the argmax
# of the two-label head this replaces, so the documented default reproduces its verdicts
# exactly; it is a config knob (`threshold`), recorded with every decision so it can be
# recalibrated against real probabilities.
_RISKY_THRESHOLD = 0.5
logger = logging.getLogger(__name__)

# The classifier transport belongs to classifier-core: one process-wide client per
# endpoint, so a dead service is discovered once (its breaker) instead of once per
# plugin, and the wait budget is computed where the engine's cost was measured. Its
# settings live on the classifier-core entry; this plugin keeps only its boundary.
_CLASSIFIER_MODULE_KEY: Final = "hermes_plugins_classifier_core"
_CLASSIFIER_PLUGIN: Final = "classifier-core"
_CLASSIFIER_CLIENT_PATH: Final = (
    Path(__file__).resolve().parent.parent / "classifier-core" / "client.py")
_CALLER: Final = "auto-review-classifier"
_QUESTIONS: Final[dict[str, object]] = {
    "review": {
        "type": "choice",
        "instructions": "Does this tool call need human review before it runs?",
        "criteria": {"risky": "destructive, irreversible, or exfiltrating",
                     "safe": "no destructive, irreversible, or exfiltrating effect"}},
}


class _Transport(Protocol):
    """The one classifier-core call this plugin makes; injected, never built here."""

    def ask(self, state: str, questions: dict[str, object], *, caller: str,
            timeout: float | None = None) -> dict[str, object] | None: ...


class _SettingsClass(Protocol):
    """``ClassifierSettings`` as this consumer sees it: parsed once, at register."""

    def from_config(self, get_config: Callable[[str, object], object]) -> object: ...


class _ClientModule(Protocol):
    """The surface of classifier-core's ``client.py`` this plugin depends on."""

    ClassifierSettings: _SettingsClass

    def shared_client(self, settings: object) -> _Transport: ...


def _load_client() -> _ClientModule | None:
    """Path-load ``client.py``, preferring the process-wide shared registration.

    A second copy of the module would mean a second breaker, so a module already
    registered under ``_CLASSIFIER_MODULE_KEY`` (which ``client.py`` does itself at
    import) is reused verbatim. An absent or broken sibling is ``None``: the same
    fail-open path as an unusable verdict, never an exception at register.
    """
    shared = sys.modules.get(_CLASSIFIER_MODULE_KEY)
    if isinstance(shared, ModuleType):
        return cast(_ClientModule, shared)
    if not _CLASSIFIER_CLIENT_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location(_CLASSIFIER_MODULE_KEY,
                                                  _CLASSIFIER_CLIENT_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE execution, in the import system's own order: a frozen dataclass
    # resolves its string annotations through the module entry in ``sys.modules``, and
    # the shared-state registration inside ``client.py`` depends on that entry too.
    sys.modules[_CLASSIFIER_MODULE_KEY] = module
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:  # narrow: degraded
        sys.modules.pop(_CLASSIFIER_MODULE_KEY, None)  # never share a half-built module
        logger.warning("auto-review-classifier: classifier-core failed to load (%s: %s)",
                       type(exc).__name__, exc, exc_info=True)
        return None
    return cast(_ClientModule, module)


def _classifier_config() -> Callable[[str, object], object]:
    """classifier-core's own settings reader; this plugin has no transport settings."""
    from hermes_cli.config import cfg_get, load_config_readonly

    config = load_config_readonly()

    def reader(key: str, default: object) -> object:
        return cfg_get(config, "plugins", "entries", _CLASSIFIER_PLUGIN, "settings",
                       key, default=default)

    return reader


def _shared_client() -> _Transport | None:
    """The shared client, or ``None``: with no transport every call fails open."""
    module = _load_client()
    if module is None:
        logger.warning("auto-review-classifier: sibling plugin %s is unavailable (%s); "
                       "every call fails open", _CLASSIFIER_PLUGIN, _CLASSIFIER_CLIENT_PATH)
        return None
    try:
        return module.shared_client(module.ClassifierSettings.from_config(_classifier_config()))
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:  # narrow
        logger.warning("auto-review-classifier: classifier-core settings are unusable "
                       "(%s: %s); every call fails open", type(exc).__name__, exc,
                       exc_info=True)
        return None


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
    # One shared transport for the whole process: the breaker that keeps a dead
    # service from taxing every tool call lives in classifier-core, not here.
    client = _shared_client()

    def classify(text):
        threshold = _RISKY_THRESHOLD
        # An unusable configured boundary never becomes a block: the documented
        # default stands, rather than failing the tool call open or closed by accident.
        with contextlib.suppress(TypeError, ValueError):
            threshold = float(ctx.get_config("threshold", _RISKY_THRESHOLD))
        if client is None:
            return None  # no transport: fail open exactly as an unusable verdict does
        data = client.ask(text, dict(_QUESTIONS), caller=_CALLER)
        answers = data.get("answers") if isinstance(data, dict) else None
        answer = answers.get("review") if isinstance(answers, dict) else None
        probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
        probability = probabilities.get("risky") if isinstance(probabilities, dict) else None
        if not isinstance(probability, bool) and isinstance(probability, (int, float)) \
                and 0.0 <= probability <= 1.0:
            # The threshold decides on P(risky) -- the positive class of the head
            # this replaces; the returned choice never upgrades a low probability.
            return float(probability) >= threshold, float(probability), threshold
        logger.warning("auto-review classifier returned unusable verdict: %r", answer)
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
        decision = classify(outbound)
        if decision is None:  # outage or unusable reply: fail open, exactly as before
            return status("classifier_unavailable_or_invalid")
        risky, probability, threshold = decision
        # Both sides of the boundary are logged so the threshold can be recalibrated
        # against the probabilities actually observed; the row below carries them too.
        logger.info("auto-review decision: probability=%s threshold=%s risky=%s",
                    probability, threshold, risky)
        try:
            if _snapshot(tool_name, args, task_id) != summary:
                return status("target_or_context_changed_during_review")
        except Exception:
            return status("context_recheck_unavailable")
        if not risky:
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
                     "rule_key": f"auto-review:{tool_name}:{digest}",
                     "risk_review": {"status": "risky", "reason": "above_threshold",
                                     "screened": True, "probability": probability,
                                     "threshold": threshold}}
        if policy != "deny":
            directive["revalidate"] = revalidate
        return directive

    ctx.register_hook("pre_tool_call", gate)
