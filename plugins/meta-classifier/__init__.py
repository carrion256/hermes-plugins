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
- Shadow decisions (`shadow_decisions`, on by default): the risk tier of every command this
  plugin escalates to a human, and every Droid Shield screen, are appended as verdict rows to
  the shadow-gate ledger, keyed so the human's later approval answer pairs with them. Rows
  are evidence for a promotion decision made elsewhere; no decision here reads them.
- `hermes meta-classifier-replay [--run]`: replays droid's captured payloads, then reports
  the transport and the shadow row counters.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import importlib.util
import json
import logging
import re
import shlex
import stat
import subprocess
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Final, Literal, Protocol, cast, runtime_checkable

from . import addons, replay, schemas
from . import browser_decision as browser_decision_mod
from .client import Classifier, has_service_key
from .text import canonical as _canonical, egress as _egress, render_transcript, window as _window

# Stdlib logging, not structlog: the Hermes host ships no structlog (it is not
# an installed dependency) and owns plugin logging; see plugins-doctor/checks.py.
logger = logging.getLogger(__name__)
_CLASSIFY_TIMEOUT: Final[float] = 2.5
# The replay CLI's own wait budget: captured payloads are far larger than a tool-call screen.
_REPLAY_TIMEOUT: Final[float] = 30.0
_TURNSTATE_KEY: Final = "hermes_plugins_keyword_steering_turnstate"
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


_MUTATING_CLASSES = frozenset({"file_mutation", "delete", "move", "command_execution",
                               "process_mutation", "unknown"})


def _effect_classes(tool: str, args: dict) -> tuple[list[str], list[str]]:
    """(effect classes, unknown aspects) for one tool call — the one notion of
    what a call writes.

    Both the execute-risk snapshot and the ``/dry`` veto read this, so the two
    can never disagree about whether an operation mutates state. Unproven effects
    (MCP tools, browser actions, anything unrecognized) classify as ``unknown``:
    unknown is never read-only here.
    """
    classes: list[str] = []
    unknown: list[str] = []
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
    return classes, unknown


def _read_only(tool: str, args: object) -> bool:
    """A call this plugin never screens: the read-only tool set, and the
    poll-only ``process_manage`` actions."""
    if tool in _SKIP:
        return True
    return (tool == "process_manage" and isinstance(args, dict)
            and args.get("action") in _POLLING)


def _mutating(tool: str, args: dict) -> bool:
    """True unless the call is provably read-only by :func:`_effect_classes`."""
    if _read_only(tool, args):
        return False
    classes, _unknown = _effect_classes(tool, args)
    return bool(_MUTATING_CLASSES.intersection(classes))


@runtime_checkable
class _ActivationLike(Protocol):
    """The current turn's activations as ``keyword-steering``'s turnstate holds them."""

    turn_id: str
    names: frozenset[str]


@runtime_checkable
class _TurnStateLike(Protocol):
    """The part of the turn-state store the dry veto uses."""

    def active(self, session_id: str) -> _ActivationLike | None: ...

    def record_withheld(self, session_id: str, tool_name: str, reason: str) -> None: ...


def _turn_state() -> _TurnStateLike | None:
    """``keyword-steering``'s turn-state module, or ``None`` when it is absent.

    Loaded by path through the shared ``sys.modules`` key (house sibling
    convention): both plugins must read and write ONE process-wide store, and a
    plugin installed without keyword-steering simply has no /dry to honour —
    ``register`` warns about that instead of pretending the veto exists.
    """
    module = sys.modules.get(_TURNSTATE_KEY)
    if not isinstance(module, ModuleType):
        path = Path(__file__).resolve().parent.parent / "keyword-steering" / "turnstate.py"
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(_TURNSTATE_KEY, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_TURNSTATE_KEY] = module  # dataclasses resolve annotations here
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(_TURNSTATE_KEY, None)
            raise
    return module if isinstance(module, _TurnStateLike) else None


# ------------------------------------------------------------------ Shadow decisions
# The ledger belongs to shadow-gate; this plugin is one of its producers and never its
# judge. A verdict row is written when a verdict is produced and paired later, when the
# human answers the approval that verdict minted. A ledger that is absent -- the sibling is
# not installed, or its rows cannot be written -- costs nothing: the row is counted as
# ``ledger_unavailable`` and reported, exactly as an unusable classifier is.
_SHADOW_PLUGIN: Final[str] = "shadow-gate"
_SHADOW_LEDGER_KEY: Final[str] = "hermes_plugins_shadow_gate"
_SHADOW_LEDGER_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / _SHADOW_PLUGIN / "ledger.py")
RISK_DECISION: Final[str] = "meta_classifier.risk"
SHIELD_DECISION: Final[str] = "meta_classifier.shield"
LEDGER_AVAILABLE: Final[str] = "available"
LEDGER_UNAVAILABLE: Final[str] = "ledger_unavailable"
# The host namespaces a plugin rule's approval key and scopes it to the executed arguments
# when a hook modified them (tools/approval.py); neither is part of the identity this plugin
# minted, so both are stripped to recover the key a verdict row was written under.
_APPROVAL_NAMESPACE: Final[str] = "plugin_rule:"
_APPROVAL_ARG_SCOPE: Final[str] = "#exec:"
# Only an answer a person (or the guardian) actually gave is an outcome. A prompt that timed
# out, was withdrawn or was never delivered carries no decision, so it maps to nothing.
_APPROVAL_OUTCOMES: Final[Mapping[str, str]] = MappingProxyType({
    "once": "approved", "session": "approved", "always": "approved",
    "smart_approve": "approved", "deny": "denied", "smart_deny": "denied"})
_TRACKED_APPROVALS: Final[int] = 512  # newest kept; one turn mints a handful of these
ShieldVerdict = Literal["block", "warn", "clean"]


class _Ledger(Protocol):
    """The two shadow-gate appends this plugin performs; rows only, never policy."""

    def record_verdict(self, decision: str, key: str, input_digest: str, verdict: str, *,
                       meta: Mapping[str, str] | None = None) -> object: ...

    def record_outcome(self, decision: str, key: str, outcome: str, *,
                       meta: Mapping[str, str] | None = None) -> object: ...


class _LedgerModule(Protocol):
    """The surface of shadow-gate's ``ledger.py`` this producer depends on."""

    def shared_ledger(self, root: Path) -> _Ledger: ...


def _load_sibling(module_key: str, path: Path, consumer: str) -> ModuleType | None:
    """Load one sibling module by path, preferring the process-wide registration.

    Producers and readers must agree on one ledger (one row cache, one append lock), so a
    module already registered under ``module_key`` -- which each sibling does itself at
    import -- is reused verbatim. ``sys.modules`` is populated BEFORE execution because a
    frozen dataclass resolves its string annotations through that entry. An absent or broken
    sibling is ``None`` and a counted degradation, never an exception at register.
    """
    shared = sys.modules.get(module_key)
    if isinstance(shared, ModuleType):
        return shared
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(module_key, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:  # narrow: degraded
        sys.modules.pop(module_key, None)  # never share a half-built module
        logger.warning("meta-classifier: %s failed to load (%s: %s)", consumer,
                       type(exc).__name__, exc, exc_info=True)
        return None
    return module


def _ledger_root() -> Path:
    """shadow-gate's configured root, or the default that plugin itself documents."""
    from hermes_cli.config import cfg_get, load_config_readonly
    from hermes_constants import get_hermes_home

    configured = ""
    try:
        raw = cfg_get(load_config_readonly(), "plugins", "entries", _SHADOW_PLUGIN,
                      "settings", "ledger_dir", default="")
    except (ImportError, OSError, RuntimeError, ValueError) as exc:  # narrow: use the default
        logger.warning("meta-classifier: %s ledger_dir is unreadable (%s: %s); using the "
                       "default root", _SHADOW_PLUGIN, type(exc).__name__, exc, exc_info=True)
    else:
        configured = raw.strip() if isinstance(raw, str) else ""
    if configured:
        return Path(configured).expanduser()
    return Path(get_hermes_home()) / "plugin-data" / _SHADOW_PLUGIN


def _shared_ledger() -> _Ledger | None:
    """The shared ledger, or ``None``: writes then count as ``ledger_unavailable``."""
    module = _load_sibling(_SHADOW_LEDGER_KEY, _SHADOW_LEDGER_PATH, _SHADOW_PLUGIN)
    if module is None:
        return None
    try:
        return cast(_LedgerModule, module).shared_ledger(_ledger_root())
    except (OSError, RuntimeError, TypeError, ValueError) as exc:  # narrow: degraded
        logger.warning("meta-classifier: %s ledger is unusable (%s: %s); rows will be "
                       "counted as %s", _SHADOW_PLUGIN, type(exc).__name__, exc,
                       LEDGER_UNAVAILABLE, exc_info=True)
        return None


def _digest(text: str) -> str:
    """The first 16 hex digits of text's SHA-256: a row key and input digest, never an
    integrity claim (the ledger stores both, and neither is verified against anything)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _approval_key(pattern_key: object) -> str:
    """The key this plugin minted that an approval's ``pattern_key`` carries, or ``""``."""
    if not isinstance(pattern_key, str):
        return ""
    return pattern_key.removeprefix(_APPROVAL_NAMESPACE).split(_APPROVAL_ARG_SCOPE, 1)[0]


def _row_meta(values: Mapping[str, object]) -> dict[str, str]:
    """The string fields of a meta mapping: the ledger's row schema is ``Mapping[str, str]``.

    A non-string is dropped rather than stringified -- an absent field is honest, a coerced
    one invents evidence that was never there.
    """
    return {name: value for name, value in values.items() if isinstance(value, str) and value}


@dataclass(frozen=True, slots=True)
class _PendingApproval:
    """Which recorded verdict a future approval answers, and the row its outcome joins."""

    decision: str
    key: str


class ShadowGate:
    """This plugin's ledger appends, with every failure counted instead of raised.

    Rows are the point and never the decision: nothing in the screening paths reads the
    ledger, so an absent ledger or a refused append only moves a counter -- which the status
    surface and ``meta-classifier-replay`` print -- and logs once.

    ``awaiting`` is the join. A verdict is recorded when a directive is minted, and the row
    key IS that directive's ``rule_key``, because the approval's ``pattern_key`` is
    ``plugin_rule:<rule_key>`` (host: tools/approval.py); :func:`_approval_key` strips that
    namespace and the host's ``#exec:`` argument scope to recover it. A shield row is keyed
    by the digest of the git command instead, so an approval that reaches the gate through the
    ordinary dangerous-command path -- where ``pattern_key`` is a detector key and only
    ``command`` is the command itself -- can still be paired; both keys are tracked.
    """

    def __init__(self, ledger: _Ledger | None, enabled: bool) -> None:
        self._ledger = ledger
        self._enabled = enabled
        self.counters: dict[str, int] = {"recorded": 0, LEDGER_UNAVAILABLE: 0, "disabled": 0}
        self.awaiting: dict[str, _PendingApproval] = {}

    @property
    def available(self) -> bool:
        """Whether a row could be written right now."""
        return self._enabled and self._ledger is not None

    def verdict(self, decision: str, key: str, input_digest: str, verdict: str,
                meta: Mapping[str, object] | None = None) -> None:
        """Append one verdict row. Never raises: a shadow that cannot be written is counted."""
        if not self._enabled:
            self.counters["disabled"] += 1
            return
        if self._ledger is None:
            self.counters[LEDGER_UNAVAILABLE] += 1
            return
        try:
            self._ledger.record_verdict(decision, key, input_digest, verdict,
                                        meta=_row_meta(meta or {}))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:  # narrow: counted
            self.counters[LEDGER_UNAVAILABLE] += 1
            logger.warning("meta-classifier: could not record a %s verdict (%s: %s)",
                           decision, type(exc).__name__, exc, exc_info=True)
            return
        self.counters["recorded"] += 1

    def track(self, join_key: str, decision: str, key: str) -> None:
        """Remember that an approval carrying ``join_key`` answers row ``key`` of ``decision``."""
        if not self.available or not join_key:
            return
        self.awaiting[join_key] = _PendingApproval(decision=decision, key=key)
        while len(self.awaiting) > _TRACKED_APPROVALS:
            self.awaiting.pop(next(iter(self.awaiting)))  # oldest first: the join is turn-brief

    def answer(self, *, pattern_key: object, command: object, choice: object,
               decided_by: object, surface: object) -> None:
        """Pair one approval with the verdict it answers; an unrelated approval writes nothing."""
        if not self.available:
            return
        pending = self.awaiting.pop(_approval_key(pattern_key), None)
        if pending is None and isinstance(command, str) and command:
            pending = self.awaiting.pop(_digest(command), None)
        if pending is None:
            return
        outcome = _APPROVAL_OUTCOMES.get(choice) if isinstance(choice, str) else None
        if outcome is None:
            return  # nobody decided: neither an approval nor a refusal is evidence
        self._ledger.record_outcome(  # never guarded: answer() runs inside the observer's boundary
            pending.decision, pending.key, outcome,
            meta=_row_meta({"decided_by": decided_by, "surface": surface}))
        self.counters["recorded"] += 1


def shadow_status() -> dict[str, object]:
    """Whether shadow rows can be written, for the plugin's status and replay output.

    Resolved on demand rather than from a register-time closure, so an operator or a test can
    ask without a screening run having happened. An absent ledger is reported as
    ``ledger_unavailable`` -- the same word the row counters use -- so one reading of the
    report is enough, and a missing shadow-gate is never mistaken for a plugin that shadowed
    nothing.
    """
    return {"ledger": LEDGER_AVAILABLE if _shared_ledger() is not None else LEDGER_UNAVAILABLE,
            "decisions": [RISK_DECISION, SHIELD_DECISION]}


def _snapshot(tool, args, task_id):
    context = _context(task_id)
    targets, unknown = _targets(tool, args, task_id, context)
    if context["backend"] != "local":
        unknown.append("backend_environment_unknown")
    if context["workspace"] == "unknown":
        unknown.append("workspace_unknown")
    action = args.get("action") if isinstance(args.get("action"), str) else tool
    classes, effect_unknown = _effect_classes(tool, args)
    unknown.extend(effect_unknown)
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
    # One shared transport for the whole process: the endpoint, the served model, the key and
    # the breaker that keeps a dead service from taxing every tool call all live in
    # classifier-core. Absent or unusable, every call answers None -- the same fail path a
    # dead endpoint gave, and each caller keeps its own posture.
    classifier = Classifier(timeout=_CLASSIFY_TIMEOUT)
    scale = str(ctx.get_config("risk_scale", "jev") or "jev").lower()
    tiers = schemas.RISK_SCALES.get(scale) or schemas.RISK_SCALES["jev"]
    scale = scale if scale in schemas.RISK_SCALES else "jev"
    approve_from = str(ctx.get_config("approve_from", "high") or "high").lower()
    approve_index = tiers.index(approve_from) if approve_from in tiers else len(tiers) - 1
    risk_schema = schemas.risk_schema(scale)
    transcript_chars = int(ctx.get_config("transcript_chars", 4000) or 0)
    # /dry is a keyword-steering turn mode; the veto is ours. Both are read once,
    # here: an unusable setting fails registration rather than half-enforcing.
    dry_turn = ctx.get_config("dry_turn", True)
    if not isinstance(dry_turn, bool):
        raise TypeError("meta-classifier: 'dry_turn' must be a boolean")
    turn_state = _turn_state() if dry_turn else None
    if dry_turn and turn_state is None:
        logger.warning("meta-classifier: cannot enforce /dry — keyword-steering's "
                       "turnstate module is unavailable, so the dry-turn veto is inert")
    # Shadow rows are evidence for shadow-gate's promotion gate, never an input to a decision
    # here: the ledger is resolved once, and every later failure is a counted row.
    shadow_decisions = ctx.get_config("shadow_decisions", True)
    if not isinstance(shadow_decisions, bool):
        raise TypeError("meta-classifier: 'shadow_decisions' must be a boolean")
    shadow = ShadowGate(_shared_ledger() if shadow_decisions else None, shadow_decisions)
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
    def record_shield(command: str, diff: str, verdict: ShieldVerdict, rule_key: str = "") -> None:
        """One shield verdict row, keyed by the git command it screened.

        The row key is the command's digest because a git write can reach the approval gate as
        an ordinary dangerous-command prompt, whose ``pattern_key`` is a detector key: the only
        field that identifies the same operation there is the ``command`` the host reports. When
        this plugin mints the approval itself (the warn path) the directive's rule key is
        tracked as well, because that is what the approval's ``pattern_key`` carries.
        """
        key = _digest(command)
        shadow.verdict(SHIELD_DECISION, key, _digest(diff), verdict)
        shadow.track(key, SHIELD_DECISION, key)
        if rule_key:
            shadow.track(rule_key, SHIELD_DECISION, key)

    def shield(command, task_id, tool_call_id):
        """None (nothing to say), a directive, or a status dict (unscreened).

        The shadow row says what the screen concluded -- ``block``, ``warn`` or ``clean`` -- and
        only when a screen actually happened. An unknown workspace, an unavailable redaction or
        an unknowable push range is not a verdict, and recording one as clean would invent
        evidence for exactly the case this plugin refuses to guess about.
        """
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
            # Screened, and nothing secret-shaped was published: that is a clean verdict, not
            # the absence of one.
            record_shield(command, diff, "clean")
            return None
        downgrade = [c for c in contexts if c["kind"] == "downgrade"]
        risk = [c for c in contexts if c["kind"] == "risk"][:_SHIELD_CAP]
        if len(downgrade) > _SHIELD_CAP:
            record_shield(command, diff, "block")
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
            record_shield(command, diff, "block")
            return {"action": "block", "message": "meta-classifier shield: likely real secrets in the "
                    f"diff ({', '.join(blocking)}). STOP: do not retry this command; remove the "
                    "secret material and rotate anything already exposed."}
        if warnings:
            digest = hashlib.sha256(_canonical([command, task_id, tool_call_id or uuid.uuid4().hex,
                                                sorted(warnings)]).encode()).hexdigest()
            rule_key = f"meta-classifier:shield:{digest}"
            record_shield(command, diff, "warn", rule_key)
            return {"action": "block" if policy() == "deny" else "approve",
                    "message": "meta-classifier shield: possible hardcoded credential values "
                    f"({', '.join(warnings)}); human review required.",
                    "rule_key": rule_key}
        record_shield(command, diff, "clean")
        return None

    # ------------------------------------------------------------ Dry turn
    def dry_veto(tool_name: str, args: dict, session_id: str, turn_id: str) -> dict | None:
        """Refuse a mutating call while ``/dry`` is active, without asking the model.

        What counts as mutating is this plugin's own ``_effect_classes`` — the
        same table the risk snapshot reads — so /dry and risk screening can never
        disagree about what a call writes. No classifier request is made on this
        path, and the refusal is recorded in keyword-steering's turn state so
        ``steering_status`` shows it: a silent fail-open would look like a veto
        that simply never had anything to do.
        """
        if not dry_turn or turn_state is None:
            return None
        activation = turn_state.active(session_id)
        if not isinstance(activation, _ActivationLike) or "dry" not in activation.names:
            return None
        if not _mutating(tool_name, args):
            return None
        turn_state.record_withheld(session_id, tool_name, "dry_turn")
        digest = hashlib.sha256(_canonical([session_id, activation.turn_id, turn_id,
                                            tool_name, sorted(args)]).encode("utf-8")).hexdigest()
        return {"action": "block",
                "message": f"DRY TURN: {tool_name} withheld — mutating operation; "
                           "re-issue without /dry to apply.",
                "rule_key": "dry_turn",
                "directive_id": f"dry_turn:{digest}"}

    # ------------------------------------------------------------ Execute risk
    def gate(tool_name="", args=None, task_id="", tool_call_id="", session_id="", turn_id="", **_kw):
        if _read_only(tool_name, args):
            return None
        veto = dry_veto(tool_name, args if isinstance(args, dict) else {},
                        str(session_id or ""), str(turn_id or ""))
        if veto is not None:
            return veto

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
            state_text = _canonical(state)
        except Exception:
            return status("context_or_redaction_unavailable")
        got = classifier.classify(state_text, risk_schema)
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
        rule_key = f"meta-classifier:{tool_name}:{digest}"
        # The verdict row is written for the approval this directive mints, and keyed by the
        # rule_key that directive carries: the host reports that key back as the approval's
        # ``pattern_key`` (namespaced ``plugin_rule:``, tools/approval.py), which is how
        # :meth:`ShadowGate.answer` pairs the human's decision with the tier that asked for it.
        # A tier below ``approve_from`` never reaches a human, so it has no outcome to pair and
        # records nothing -- an unpairable row would only dilute the evidence. When the shield
        # owns this approval (its warning rode along in the message) the key is still ours.
        shadow.verdict(RISK_DECISION, rule_key, _digest(state_text), tier, {"scale": scale})
        shadow.track(rule_key, RISK_DECISION, rule_key)
        directive = {"action": "block" if policy() == "deny" else "approve", "message": message,
                     "rule_key": rule_key}
        if policy() != "deny":
            directive["revalidate"] = revalidate
        return directive

    ctx.register_hook("pre_tool_call", gate)

    def on_approval(pattern_key="", command="", choice="", decided_by="", surface="", **_kw):
        """Pair one recorded verdict with the human's answer to the approval it asked for.

        Observer-only (the host ignores returns and cannot be vetoed from here), so it must
        never raise: a shadow row that cannot be written is counted, and the approval the user
        is waiting on is never delayed by a ledger that is having a bad day.
        """
        try:
            shadow.answer(pattern_key=pattern_key, command=command, choice=choice,
                          decided_by=decided_by, surface=surface)
        except Exception:  # boundary: the observer's contract is to never raise,
                          # and the failure shape is the ledger's, not ours; the counter says
                          # the row is missing, the traceback says why.
            logger.warning("meta-classifier: could not record an approval outcome",
                           exc_info=True)

    ctx.register_hook("post_approval_response", on_approval)

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
                # that transcript, with no error row and no retry after the shared breaker
                # let the service be tried again.
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
        check_fn=has_service_key,
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
        check_fn=has_service_key,
        description="Next browser action (operation + element index) from an observed table.",
    )

    # ------------------------------------------------------------ replay CLI
    def replay_setup(sub):
        sub.add_argument("--run", action="store_true",
                        help="POST every payload through the shared classifier transport "
                             "(classifier-core's endpoint; 30 s per payload)")
        sub.add_argument("--captures", default=str(ctx.get_config("captures_dir", replay.DEFAULT_CAPTURES)),
                         help="droid notes/captured/classifiers directory")
        sub.add_argument("--cases", default=None,
                         help="JSONL of human-labelled cases {id, text, addon|schema, expect} instead of droid captures; "
                              "--run prints per-head agreement")

    def replay_handler(args):
        payloads = addons.load_cases(args.cases) if args.cases else replay.build_payloads(args.captures)
        if args.run:
            # The replay's own deadline, stated explicitly: a captured payload runs to tens of
            # thousands of characters behind the service's serialised inference lane, so the
            # plugin's ordinary floor is not a budget for it. There is no cooldown argument to
            # pass any more -- the outage breaker is the shared client's.
            replay.run(payloads, Classifier(timeout=_REPLAY_TIMEOUT), print)
            # A replay is also the operator's diagnostic: which transport answered, and whether
            # the shadow rows this plugin is supposed to be writing were actually written.
            print(json.dumps({"classifier": classifier.status(), "shadow": shadow_status(),
                              "rows": dict(shadow.counters)}, ensure_ascii=False))
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
