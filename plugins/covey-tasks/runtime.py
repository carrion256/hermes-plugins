"""Covey task runtime: one durable-worklist adapter per live Hermes agent.

This module owns the plugin-private half of the ``covey-tasks`` contract:
typed action parsing, the native CLI transport, two lazily-registered role
principals, exactly one active claim per live agent, request idempotency
retention, lease maintenance and bounded teardown.  The host (``__init__.py``)
owns binding real agents and the tool schema; it must never see session
tokens.  Model-facing outputs are allowlisted and redacted for the backend
token grammar.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable

# Native transport reuse: the plugin depends on the installed Hermes covey
# bridge module for subprocess execution.  Tests intercept this module-level
# name; ordinary tests use the real CLI.  No copied fallback transport.
try:
    from plugins.covey.lib.covey_tool import run_covey_command, resolve_covey_binary
except ImportError:  # unsupported host; surfaced as a clear error at execute()
    run_covey_command = None  # type: ignore[assignment]
    resolve_covey_binary = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

I64_MAX = 2**63 - 1
SESSION_TOKEN_RE = re.compile(r"session_[0-9a-fA-F]+")
REDACTED = "session_[redacted]"
NORMAL_TIMEOUT = 30.0  # exceeds Covey's 5s SQLite busy timeout + migration margin
TASK_OPERATIONS = {"create", "add", "start", "finish", "retry", "fail", "release",
                   "abandon", "checkpoint", "resume", "candidates", "availability", "stuck"}
TERMINAL_STATES = {
    "finish": "completed",
    "retry": "available",
    "fail": "failed",
    "abandon": "abandoned",
    "release": "available",
}


# Structured resumable handoff carried in subtask_attempt_outcomes.summary.
HANDOFF_MARKER = "covey-handoff.v1"
HANDOFF_MAX_BYTES = 4096  # the native validate_attempt_summary byte limit
HANDOFF_LIST_FIELDS = ("changes", "verification_refs", "failed_approaches", "remaining")

GUARDED_FILE_TOOLS = frozenset({"write_file", "patch"})
GUARDED_TERMINAL_TOOLS = frozenset({"terminal", "process_manage"})
READ_ONLY_PROCESS_ACTIONS = frozenset({"poll", "log", "wait", "list"})
FILE_PATH_KEYS = ("path", "file", "file_path")
WORKSPACE_LOCK_NAME = "covey-tasks.lock"

UNCERTAIN_CODES = {"timeout", "empty_output", "invalid_json", "invalid_envelope"}
# Engine-rendered verdicts (CoveyError -> conflict/not_found/permission_denied
# with exit codes 1..5) are the only codes that prove authority is gone.
# Bridge-level faults (binary_not_found, OS permission_denied, exit_N,
# internal_error/DatabaseError such as SQLITE_BUSY) prove nothing.
DEFINITE_LOSS_CODES = {"conflict", "not_found", "session_not_active"}
# Flat SubtaskCandidate fields projected to plugin consumers.
CANDIDATE_KEYS = (
    "subtask_id", "meta_task_id", "title", "kind", "state",
    "completion_policy", "routing_key", "priority", "effective_priority",
    "is_repair_followup", "blocked_dependents_count", "created_at", "updated_at",
)

# SessionNotActive / IllegalTransition(Session) / unknown session are all
# rendered by the backend as conflict or not_found; only the message tells
# them apart from claim-level conflicts.
SESSION_INACTIVE_RE = re.compile(
    r"is not active; current state is|illegal transition on session\b|"
    r"invalid session token|session not found",
    re.IGNORECASE,
)
CREATE_GUIDANCE = (
    "Plan/add all currently known tasks before starting execution. When every "
    "task is terminal Covey closes the worklist, even if some failed; a closed "
    "worklist rejects new tasks, and there is no silent rollover."
)
START_GUIDANCE = (
    "The claim lease is coordination, not an OS fence. Finish, retry, fail, "
    "release or abandon this task before starting another one from this agent."
)


class ActionReject(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StatusAction:
    pass


@dataclass(frozen=True)
class ListsAction:
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True)
class ShowMetaAction:
    meta_task_id: str
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True)
class ShowTaskAction:
    subtask_id: str


@dataclass(frozen=True)
class CreateAction:
    prompt: str
    request_id: str


@dataclass(frozen=True)
class AddAction:
    meta_task_id: str
    title: str
    priority: int
    request_id: str


@dataclass(frozen=True)
class StartAction:
    subtask_id: str
    request_id: str
    workspace: str = ""  # canonical linked-worktree root; "" = unbound (legacy)


@dataclass(frozen=True)
class CheckpointAction:
    subtask_id: str
    handoff: dict
    evidence_digest: str | None = None
    request_id: str = ""


@dataclass(frozen=True)
class ResumeAction:
    subtask_id: str


@dataclass(frozen=True)
class CandidatesAction:
    role: str
    meta_task_id: str = ""
    routing_key: str = ""
    limit: int = 50


@dataclass(frozen=True)
class AvailabilityAction:
    meta_task_id: str = ""
    routing_key: str = ""


@dataclass(frozen=True)
class StuckAction:
    older_than_ms: int
    limit: int = 50


@dataclass(frozen=True)
class FinishAction:
    subtask_id: str
    summary: str
    evidence_digest: str | None
    request_id: str
    handoff: dict | None = None


@dataclass(frozen=True)
class RetryFailAction:
    subtask_id: str
    summary: str
    failure_code: str
    evidence_digest: str | None
    request_id: str
    op: str = ""
    handoff: dict | None = None


@dataclass(frozen=True)
class ReleaseAbandonAction:
    subtask_id: str
    request_id: str


# ---------------------------------------------------------------------------
# Strict boundary parsing
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_int(value: Any, name: str, *, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionReject("invalid_arguments", f"{name} must be an integer")
    if not lo <= value <= hi:
        raise ActionReject("invalid_arguments", f"{name} must be between {lo} and {hi}")
    return value


def _parse_str(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value.strip() and not allow_empty):
        raise ActionReject("invalid_arguments", f"{name} must be a non-empty string")
    if "\x00" in value:
        # argv cannot carry NUL; reject before transport, never per-character filter.
        raise ActionReject("invalid_arguments", f"{name} must not contain NUL")
    return value


def _parse_request_id(value: Any) -> str:
    if value is None or value == "":
        return str(uuid.uuid4())
    text = _parse_str(value, "request_id")
    try:
        return str(uuid.UUID(text))
    except ValueError as exc:
        raise ActionReject("invalid_arguments", f"request_id must be a UUID: {exc}") from exc


def _require(args: dict, name: str) -> str:
    if name not in args:
        raise ActionReject("invalid_arguments", f"missing required field {name!r}")
    return _parse_str(args[name], name)


def _check_unknown(args: dict, allowed: set[str]) -> None:
    extra = set(args) - allowed
    if extra:
        raise ActionReject(
            "invalid_arguments",
            f"unexpected fields: {', '.join(sorted(extra))}",
        )


def parse_action(arguments: dict) -> Any:
    """Parse raw tool arguments into one typed action variant, exactly once."""
    if not isinstance(arguments, dict):
        raise ActionReject("invalid_arguments", "tool arguments must be an object")
    action = _parse_str(arguments.get("action"), "action")
    args = {k: v for k, v in arguments.items() if k != "action"}

    if action == "status":
        _check_unknown(args, set())
        return StatusAction()
    if action == "lists":
        _check_unknown(args, {"limit", "offset"})
        limit = _parse_int(args.get("limit", 50), "limit", lo=1, hi=500)
        offset = _parse_int(args.get("offset", 0), "offset", lo=0, hi=I64_MAX)
        return ListsAction(limit=limit, offset=offset)
    if action == "show":
        _check_unknown(args, {"meta_task_id", "subtask_id", "limit", "offset"})
        has_meta = "meta_task_id" in args
        has_sub = "subtask_id" in args
        if has_meta == has_sub:
            raise ActionReject(
                "invalid_arguments",
                "show requires exactly one of meta_task_id or subtask_id",
            )
        if has_sub:
            if "limit" in args or "offset" in args:
                raise ActionReject(
                    "invalid_arguments",
                    "limit/offset are only valid with meta_task_id",
                )
            return ShowTaskAction(subtask_id=_require(args, "subtask_id"))
        limit = _parse_int(args.get("limit", 50), "limit", lo=1, hi=500)
        offset = _parse_int(args.get("offset", 0), "offset", lo=0, hi=I64_MAX)
        return ShowMetaAction(meta_task_id=_require(args, "meta_task_id"), limit=limit, offset=offset)
    if action == "create":
        _check_unknown(args, {"prompt", "request_id"})
        return CreateAction(
            prompt=_require(args, "prompt"),
            request_id=_parse_request_id(args.get("request_id")),
        )
    if action == "add":
        _check_unknown(args, {"meta_task_id", "title", "priority", "request_id"})
        return AddAction(
            meta_task_id=_require(args, "meta_task_id"),
            title=_require(args, "title"),
            priority=_parse_int(args.get("priority", 100), "priority", lo=0, hi=1000),
            request_id=_parse_request_id(args.get("request_id")),
        )
    if action == "start":
        _check_unknown(args, {"subtask_id", "request_id", "workspace"})
        workspace = args.get("workspace")
        return StartAction(
            subtask_id=_require(args, "subtask_id"),
            request_id=_parse_request_id(args.get("request_id")),
            workspace="" if workspace is None else _parse_str(workspace, "workspace"),
        )
    if action == "finish":
        _check_unknown(args, {"subtask_id", "summary", "evidence_digest", "request_id", "handoff"})
        digest = args.get("evidence_digest")
        handoff = args.get("handoff")
        return FinishAction(
            subtask_id=_require(args, "subtask_id"),
            summary=_require(args, "summary"),
            evidence_digest=None if digest is None else _parse_str(digest, "evidence_digest"),
            request_id=_parse_request_id(args.get("request_id")),
            handoff=None if handoff is None else _validate_handoff(handoff),
        )
    if action == "checkpoint":
        _check_unknown(args, {"subtask_id", "handoff", "evidence_digest", "request_id"})
        digest = args.get("evidence_digest")
        return CheckpointAction(
            subtask_id=_require(args, "subtask_id"),
            handoff=_validate_handoff(args.get("handoff")),
            evidence_digest=None if digest is None else _parse_str(digest, "evidence_digest"),
            request_id=_parse_request_id(args.get("request_id")),
        )
    if action in ("retry", "fail"):
        _check_unknown(args, {"subtask_id", "summary", "failure_code", "evidence_digest",
                              "request_id", "handoff"})
        digest = args.get("evidence_digest")
        handoff = args.get("handoff")
        return RetryFailAction(
            subtask_id=_require(args, "subtask_id"),
            summary=_require(args, "summary"),
            failure_code=_require(args, "failure_code"),
            evidence_digest=None if digest is None else _parse_str(digest, "evidence_digest"),
            request_id=_parse_request_id(args.get("request_id")),
            op=action,
            handoff=None if handoff is None else _validate_handoff(handoff),
        )
    if action in ("release", "abandon"):
        _check_unknown(args, {"subtask_id", "request_id"})
        return ReleaseAbandonAction(
            subtask_id=_require(args, "subtask_id"),
            request_id=_parse_request_id(args.get("request_id")),
        )
    if action == "resume":
        _check_unknown(args, {"subtask_id"})
        return ResumeAction(subtask_id=_require(args, "subtask_id"))
    if action == "candidates":
        _check_unknown(args, {"role", "meta_task_id", "routing_key", "limit"})
        role = _require(args, "role")
        if role not in ("executor", "reviewer"):
            raise ActionReject("invalid_arguments", "role must be executor or reviewer")
        return CandidatesAction(
            role=role,
            meta_task_id=_parse_str(args.get("meta_task_id"), "meta_task_id")
            if args.get("meta_task_id") is not None else "",
            routing_key=_parse_str(args.get("routing_key"), "routing_key")
            if args.get("routing_key") is not None else "",
            limit=_parse_int(args.get("limit", 50), "limit", lo=1, hi=50),
        )
    if action == "availability":
        _check_unknown(args, {"meta_task_id", "routing_key"})
        return AvailabilityAction(
            meta_task_id=_parse_str(args.get("meta_task_id"), "meta_task_id")
            if args.get("meta_task_id") is not None else "",
            routing_key=_parse_str(args.get("routing_key"), "routing_key")
            if args.get("routing_key") is not None else "",
        )
    if action == "stuck":
        _check_unknown(args, {"older_than_ms", "limit"})
        return StuckAction(
            older_than_ms=_parse_int(args.get("older_than_ms", 3600000), "older_than_ms",
                                      lo=1, hi=I64_MAX),
            limit=_parse_int(args.get("limit", 50), "limit", lo=1, hi=50),
        )
    raise ActionReject("invalid_arguments", f"unknown action {action!r}")


def _validate_handoff(value: Any) -> dict:
    """Strictly parse an inbound handoff payload; the marker is added by the
    caller so a model cannot forge it."""
    if not isinstance(value, dict):
        raise ActionReject("invalid_arguments", "handoff must be an object")
    _check_unknown(value, {"objective", *HANDOFF_LIST_FIELDS})
    handoff = {"objective": _parse_str(value.get("objective"), "handoff.objective")}
    for field_name in HANDOFF_LIST_FIELDS:
        items = value.get(field_name)
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ActionReject(
                "invalid_arguments",
                f"handoff.{field_name} must be a list of strings",
            )
        handoff[field_name] = items
    return handoff


def encode_handoff(handoff: dict, workspace: str) -> str:
    """Encode a validated handoff as the marked single-line summary the
    native receipt column carries (compact JSON, <=4096 bytes, token-redacted)."""
    record = {
        "marker": HANDOFF_MARKER,
        "workspace": workspace,
        **_validate_handoff(handoff),
    }
    summary = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    summary = _redact(summary)
    if len(summary.encode()) > HANDOFF_MAX_BYTES:
        raise ActionReject(
            "invalid_arguments",
            f"handoff exceeds the {HANDOFF_MAX_BYTES}-byte receipt limit; trim it before checkpointing",
        )
    return summary


def decode_handoff(summary: Any) -> dict | None:
    """Parse only marked, validated handoff records.  Plain text, malformed
    JSON and incomplete records return None: summary text is NEVER verified
    proof, and unmarked receipts stay visible but untrusted."""
    if not isinstance(summary, str) or HANDOFF_MARKER not in summary:
        return None
    try:
        record = json.loads(summary)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("marker") != HANDOFF_MARKER:
        return None
    if not isinstance(record.get("workspace"), str):
        return None
    if not isinstance(record.get("objective"), str):
        return None
    for field_name in HANDOFF_LIST_FIELDS:
        items = record.get(field_name)
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            return None
    return record


def _git_output(workspace: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", workspace, *args],
            capture_output=True, text=True, timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _validate_workspace(value: str) -> str:
    """Canonical REAL LINKED worktree root, or None.  The main checkout is
    refused by comparing its absolute GITDIR to the common dir (for the main
    checkout they coincide inside the checkout, e.g. <repo>/.git);
    superprojects, submodules and non-Git paths too.  Absolute Git metadata
    paths are resolved by git itself, so `git worktree add --relative-paths`
    entries anchor correctly.  This is cooperative isolation, not a security
    sandbox."""
    canonical = os.path.realpath(value)
    if canonical != value or not os.path.isdir(canonical):
        return None
    root = _git_output(canonical, "rev-parse", "--show-toplevel")
    gitdir = _git_output(canonical, "rev-parse", "--absolute-git-dir")
    common = _git_output(canonical, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if root is None or gitdir is None or common is None:
        return None
    if os.path.realpath(root) != canonical:
        return None
    if _git_output(canonical, "rev-parse", "--show-superproject-working-tree"):
        return None
    if os.path.realpath(gitdir) == os.path.realpath(common):
        return None  # gitdir IS the common dir: main checkout, not a linked worktree
    return canonical


def _file_targets(tool_name: str, args: dict) -> "list[str] | None":
    """Targets a file-mutation tool may write, or None when indeterminate.

    patch(mode='patch') carries its targets in V4A headers; reuse the native
    extractor so the guard inspects the same paths patch_tool() mutates
    (including move/delete targets).  Indeterminate extractions refuse."""
    if tool_name == "patch" and args.get("mode") == "patch" and isinstance(args.get("patch"), str):
        try:
            from tools.file_tools import _collect_v4a_header_paths
        except Exception:
            return None  # host seam unavailable: refuse, not inspect-and-guess
        headers = _collect_v4a_header_paths(args["patch"])
        if isinstance(headers, str):
            return None  # Native dispatch reports malformed paths
        return headers[0]
    targets = [args[k] for k in FILE_PATH_KEYS
               if isinstance(args.get(k), str) and args.get(k)]
    if tool_name == "patch" and not targets:
        return None
    return targets


def _workspace_guard(workspace: str, claim: Any, tool_name: str, args: dict) -> dict | None:
    """Cooperative workspace lease guard for a guarded (bound) workspace.

    live claim: terminal checks run normally (optionally in the bound
    workspace cwd); explicit file/patch targets outside the workspace —
    including V4A header targets — are refused.  claim gone (definite loss
    or expired lease): ALL guarded file AND terminal/process mutations are
    refused until a fresh valid start; native read-only process actions
    (poll/log/wait/list) stay exempt so a truthful handoff can inspect
    already-running checks."""
    live = (claim is not None and getattr(claim, "workspace", "") == workspace
            and not claim.uncertain
            and claim.lease_deadline_ms > int(time.time() * 1000))
    if tool_name in GUARDED_TERMINAL_TOOLS:
        if live:
            return None  # terminal checks MUST run normally while claimed
        if (tool_name == "process_manage"
                and isinstance(args, dict)
                and args.get("action") in READ_ONLY_PROCESS_ACTIONS):
            return None
        return _envelope_failure(
            "workspace_guard_closed",
            f"workspace {workspace} is under Covey workspace-guard: the owning claim was "
            f"released or lost, so terminal/process mutations are refused until a fresh "
            "valid start on this agent (read-only poll/log/wait/list are allowed)",
        )
    if tool_name not in GUARDED_FILE_TOOLS:
        return None
    if not live:
        return _envelope_failure(
            "workspace_guard_closed",
            f"workspace {workspace} is under Covey workspace-guard: the owning claim was "
            f"released or lost, so file mutations are refused until a fresh valid start",
        )
    if not isinstance(args, dict):
        return None
    targets = _file_targets(tool_name, args)
    if targets is None:
        return _envelope_failure(
            "workspace_mutation_unverified",
            f"{tool_name} carries no verifiable target paths inside a Covey-bound workspace; "
            "pass explicit paths or release the task first",
        )
    for raw in targets:
        candidate = raw if os.path.isabs(raw) else os.path.join(workspace, raw)
        target = os.path.realpath(candidate)
        if target != workspace and not target.startswith(workspace + os.sep):
            return _envelope_failure(
                "workspace_mutation_outside",
                f"{tool_name} targets {raw}, outside the Covey-bound workspace {workspace}; "
                "operate inside the bound worktree or release the task first",
            )
    return None


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def _opt(flag: str, value: Any) -> str:
    """One-token ``--flag=value`` CLI option.

    clap treats a separate token beginning with '-' as a flag, for example
    a title like '- x'; the attached form always binds it as the value.
    """
    return f"{flag}={value}"


def _redact(value: Any) -> Any:
    """Redact backend session tokens (own and foreign) from every output string."""
    if isinstance(value, str):
        value = SESSION_TOKEN_RE.sub(REDACTED, value)
        return value
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def _envelope_failure(code: str, message: str, *, exit_code: int | None = None,
                      suggestions: Any = None, data: Any = None) -> dict:
    payload: dict = {"ok": False, "code": code, "message": _redact(message)}
    if suggestions:
        payload["suggestions"] = _redact(list(suggestions))
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if data is not None:
        payload["data"] = _redact(data)
    return payload


def _map_transport_failure(payload: dict) -> dict:
    code = str(payload.get("code") or "transport_error")
    return _envelope_failure(
        code,
        str(payload.get("message") or "Covey command failed"),
        exit_code=payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else None,
        suggestions=payload.get("suggestions") if isinstance(payload.get("suggestions"), list) else None,
    )


def _is_session_inactive(payload: dict) -> bool:
    """Backend proves the session itself is reaped/exited/unknown."""
    code = str(payload.get("code") or "")
    if code == "session_not_active":
        return True
    return code in ("conflict", "not_found") and bool(
        SESSION_INACTIVE_RE.search(str(payload.get("message") or ""))
    )


def _is_definite_authority_loss(payload: dict) -> bool:
    """True only for an authoritative backend refusal; transient transport,
    OS and database faults stay uncertain until reconciliation."""
    code = str(payload.get("code") or "")
    if code in DEFINITE_LOSS_CODES:
        return True
    return code == "permission_denied" and payload.get("exit_code") == 3


# ---------------------------------------------------------------------------
# Allowlisted projections (raw payloads/warnings/tokens never pass through)
# ---------------------------------------------------------------------------


def _project_meta(meta: dict) -> dict:
    return {
        "meta_task_id": meta["meta_task_id"],
        "prompt_text": meta["prompt_text"],
        "state": meta["state"],
        "created_at": meta["created_at"],
        "updated_at": meta["updated_at"],
    }


def _project_list_item(row: dict) -> dict:
    return _project_meta(row)


def _project_task(task: dict) -> dict:
    return {
        "subtask_id": task["subtask_id"],
        "meta_task_id": task["meta_task_id"],
        "title": task["title"],
        "kind": task["kind"],
        "priority": task["priority"],
        "state": task["state"],
        "completion_policy": task["completion_policy"],
        "evidence_policy": task["evidence_policy"],
        "routing_key": task["routing_key"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
    }


def _project_outcome(row: dict) -> dict:
    return {
        "claim_id": row["claim_id"],
        "subtask_id": row["subtask_id"],
        "fence_seq": row["fence_seq"],
        "outcome_kind": row["outcome_kind"],
        "summary": row["summary"],
        "evidence_digest": row["evidence_digest"],
        "failure_code": row["failure_code"],
        "recorded_at": row["recorded_at"],
    }


# ---------------------------------------------------------------------------
# Internal state records
# ---------------------------------------------------------------------------


@dataclass
class _Claim:
    subtask_id: str
    claim_id: str
    fence_seq: int
    lease_deadline_ms: int
    uncertain: bool = False
    request_id: str = ""  # request that created this claim (partial-start replay)
    started: bool = False
    workspace: str = ""  # canonical linked-worktree root bound at claim time


@dataclass
class _RequestState:
    digest: str
    principals: dict[str, str] = field(default_factory=dict)  # role -> principal
    tokens: dict[str, str] = field(default_factory=dict)  # role -> token used
    uncertain: bool = False


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class CoveyRuntime:
    """Adapter runtime for exactly one live Hermes agent."""

    def __init__(
        self,
        agent: Any,
        *,
        binary: str = "covey",
        db_path: str,
        profile_home: str,
        lease_duration_ms: int = 300000,
        renew_interval_ms: int = 60000,
        heartbeat_interval_ms: int = 60000,
        teardown_timeout: float = 3.0,
    ) -> None:
        self.agent_ref: weakref.ref | None = weakref.ref(agent) if agent is not None else None
        self.profile_home = str(profile_home)
        self.db_path = str(db_path)
        self.last_session_id = ""
        self.lease_duration_ms = int(lease_duration_ms)
        self.renew_interval_ms = int(renew_interval_ms)
        self.heartbeat_interval_ms = int(heartbeat_interval_ms)
        self.teardown_timeout = float(teardown_timeout)
        self.instance_id = str(uuid.uuid4())
        self._closed = False
        self._config_error: str | None = None
        self._resolved_binary: str | None = None
        self._capabilities: dict | None = None
        self._capability_identity: tuple | None = None
        self._token: dict[str, str] = {}  # role -> live token
        self._retired: dict[str, str | None] = {}  # retired role -> pending-exit token
        self._request_states: dict[str, _RequestState] = {}
        self._claim: _Claim | None = None
        self._lost_reason: str | None = None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._epoch = 0
        # Every token/principal ever retired: never re-minted or replayed.
        self._dead_tokens: set[str] = set()
        self._dead_principals: set[str] = set()
        self._token_principal: dict[str, str] = {}
        self._close_guard = threading.Lock()
        self._close_started = False
        self._last_heartbeat_mono = 0.0
        self._last_renew_mono = 0.0
        self._replaced_guard = None  # previous workspace guard dropped only on successful start
        self._ws_lock = None  # machine-wide flock handle on the bound worktree
        self._cwd_backup: dict[str, object] = {}
        self._guarded_workspace: str | None = None  # guard stays open until next valid start
        self._validate_config(binary)

    # -- config / capabilities ---------------------------------------------

    def _validate_config(self, binary: str) -> None:
        if run_covey_command is None:
            self._config_error = (
                "native covey transport (plugins.covey.lib.covey_tool) is unavailable on this host"
            )
            return
        if not isinstance(binary, str) or not binary:
            self._config_error = "binary must be a non-empty executable path/name"
            return
        if (
            not isinstance(self.db_path, str)
            or not self.db_path
            or not os.path.isabs(self.db_path)
        ):
            self._config_error = "db_path must be a required absolute local path"
            return
        try:
            self._resolved_binary = resolve_covey_binary(binary)  # never consults ambient env
        except Exception as exc:  # pragma: no cover - resolver itself is defensive
            self._config_error = f"binary could not be resolved: {exc}"

    def _binary_identity(self) -> tuple | None:
        try:
            st = os.stat(self._resolved_binary)
            return (st.st_dev, st.st_ino, st.st_size, int(st.st_mtime))
        except OSError:
            return None

    def _probe_capabilities(self) -> dict:
        """DB-free ``--help`` probes; help exits before the DB is opened."""
        identity = self._binary_identity()
        if self._capabilities is not None and identity == self._capability_identity:
            return self._capabilities
        base = self._call(["--help"])
        meta_list = self._call(["meta", "list", "--help"])
        create = self._call(["subtask", "create", "--help"])
        finish = self._call(["subtask", "finish", "--help"])
        caps = {
            "base_cli": bool(base.get("ok")),
            "meta_list": bool(meta_list.get("ok"))
            and "--limit" in str(meta_list.get("data", {}).get("help_text", ""))
            and "--offset" in str(meta_list.get("data", {}).get("help_text", "")),
            "explicit_create_policy": bool(create.get("ok"))
            and "--completion-policy" in str(create.get("data", {}).get("help_text", ""))
            and "--evidence-policy" in str(create.get("data", {}).get("help_text", ""))
            and "--routing-key" in str(create.get("data", {}).get("help_text", "")),
            "finish_receipt": bool(finish.get("ok"))
            and "--summary" in str(finish.get("data", {}).get("help_text", ""))
            and "--evidence-digest" in str(finish.get("data", {}).get("help_text", "")),
        }
        self._capabilities = caps
        self._capability_identity = identity
        return caps

    def _task_operations_supported(self) -> bool:
        caps = self._probe_capabilities()
        return all(caps.values())

    # -- workspace binding (cooperative isolation, not security sandbox) ----

    def _acquire_workspace_lock(self, workspace: str) -> dict | None:
        """Machine-wide advisory lock on the worktree's shared git identity
        (.git dir for the main checkout; the linked gitdir for worktrees),
        so two plugin runtimes/profiles on this machine serialize claims on
        one worktree.  flock is cooperative, not enforcement against a
        deliberate escape."""
        # Resolve the gitdir via Git itself (absolute), so linked-worktree
        # metadata written with relative paths (`gitdir: ../.git/worktrees/x`)
        # is anchored to the WORKTREE, not to the Hermes process cwd.
        gitdir = _git_output(workspace, "rev-parse", "--absolute-git-dir")
        if gitdir is None:
            return _envelope_failure(
                "workspace_invalid", f"{workspace}: gitdir could not be resolved")
        lock_path = os.path.join(os.path.realpath(gitdir), WORKSPACE_LOCK_NAME)
        handle = None
        try:
            handle = open(lock_path, "a+", encoding="utf-8")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if handle is not None:
                handle.close()
            if isinstance(exc, BlockingIOError):
                return _envelope_failure(
                    "workspace_busy",
                    f"worktree {workspace} is held by another covey-tasks claim on this machine; "
                    "wait for it to finish or use a different worktree",
                )
            return _envelope_failure("workspace_invalid", f"cannot lock {workspace}: {exc}")
        self._ws_lock = handle
        return None

    def _release_workspace_lock(self) -> None:
        handle = self._ws_lock
        self._ws_lock = None
        if handle is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()
        self._restore_session_cwd()

    def _bind_session_cwd(self, workspace: str) -> None:
        """Route the agent's native file/terminal cwd to the workspace under
        BOTH identity keys file tools consume: session_id (terminal/session
        surface) and _current_task_id (turn-facade generates a fresh task
        UUID when run_conversation has none; file tools resolve relative
        paths through the task_id key)."""
        agent = self.agent_ref() if self.agent_ref is not None else None
        keys = [k for k in (getattr(agent, "session_id", None),
                            getattr(agent, "_current_task_id", None))
                if isinstance(k, str) and k]
        if not keys:
            return
        try:
            from tools.terminal_tool import get_session_cwd, record_session_cwd
            for key in keys:
                if key not in self._cwd_backup:
                    self._cwd_backup[key] = get_session_cwd(key)
                record_session_cwd(key, workspace)
        except Exception:
            pass  # host seam unavailable: file/terminal cwd routing degrades to unbound

    def _restore_session_cwd(self) -> None:
        try:
            from tools.terminal_tool import clear_session_cwd, record_session_cwd
            for key, prior in self._cwd_backup.items():
                if prior is None:
                    clear_session_cwd(key)
                else:
                    record_session_cwd(key, prior)
        except Exception:
            pass
        self._cwd_backup.clear()

    def workspace_guard(self, tool_name: str, args: Any) -> dict | None:
        """pre_tool_call directive for this runtime's guarded workspace."""
        with self._close_guard:
            # Check-then-publish (and the cwd restore below) is serialized
            # against close()'s authority revocation on the close guard: a
            # guard pass can never (re)bind cwd after close restored the
            # authoritative value.  The guard is held only for these host
            # calls — never across the runtime lock, a subprocess, or an
            # exit attempt, so close never waits on them.
            if self._guarded_workspace is None:
                return None
            claim = self._claim
            if claim is not None and claim.workspace == self._guarded_workspace:
                # Native task IDs can rotate between turns while the claim stays live.
                self._bind_session_cwd(self._guarded_workspace)
            elif self._cwd_backup:
                # An unbound replacement claim never owns this retained guard.
                # Restore the authoritative cwd instead of rebinding released cwd.
                self._restore_session_cwd()
            return _workspace_guard(self._guarded_workspace, self._claim,
                                    tool_name, args if isinstance(args, dict) else {})

    # -- transport ----------------------------------------------------------

    def _call(self, command_args: list, timeout: float = NORMAL_TIMEOUT, *,
              data_list: bool = False, teardown: bool = False) -> dict:
        # A closed runtime issues no backend calls except teardown exits, so a
        # late in-flight action cannot keep acting on revoked authority.
        if self._closed and not teardown:
            return {"ok": False, "code": "closed", "message": "runtime is closed"}
        # Explicit binary+db on every call (background calls included); the
        # transport never sees ambient config because we always pass both.
        payload = run_covey_command(
            command_args, db_path=self.db_path, binary=self._resolved_binary, timeout=timeout
        )
        if not isinstance(payload, dict):
            return {"ok": False, "code": "invalid_envelope", "message": "Covey returned a non-object envelope"}
        if payload.get("ok") is True:
            data = payload.get("data")
            expected = list if data_list else dict
            if data is not None and not isinstance(data, expected):
                return {"ok": False, "code": "invalid_envelope",
                        "message": "Covey data payload was not the expected type"}
        return payload

    # -- agent / session tracking --------------------------------------------

    def _agent_alive(self) -> Any:
        return self.agent_ref() if self.agent_ref is not None else None

    def _track_session(self) -> None:
        agent = self._agent_alive()
        if agent is not None:
            session_id = getattr(agent, "session_id", None)
            if isinstance(session_id, str) and session_id:
                self.last_session_id = session_id

    @property
    def closed(self) -> bool:
        return self._closed

    # -- envelope helpers ----------------------------------------------------

    @staticmethod
    def _ok(data: dict, request_id: str | None = None) -> dict:
        env = {"ok": True, "data": _redact(data)}
        if request_id is not None:
            env["request_id"] = request_id
        return env

    @staticmethod
    def _fail(code: str, message: str, request_id: str | None = None, *,
              exit_code: int | None = None, suggestions: Any = None, data: Any = None) -> dict:
        env = _envelope_failure(code, message, exit_code=exit_code, suggestions=suggestions, data=data)
        if request_id is not None:
            env["request_id"] = request_id
        return env

    # -- execute -------------------------------------------------------------

    def execute(self, arguments: dict) -> dict:
        """Parse/validate, then run exactly one operation; never raises domain errors."""
        if self._closed:
            return self._fail("closed", "runtime is closed; host will create a fresh runtime")
        self._track_session()
        try:
            action = parse_action(arguments)
        except ActionReject as reject:
            return self._fail(reject.code, reject.message)
        if self._config_error is not None:
            return self._fail("invalid_config", self._config_error)
        if self._closed:
            return self._fail("closed", "runtime is closed; host will create a fresh runtime")
        try:
            if isinstance(action, StatusAction):
                return self._status()
            if isinstance(action, (ListsAction, ShowMetaAction, ShowTaskAction,
                                   ResumeAction, CandidatesAction, AvailabilityAction,
                                   StuckAction)):
                # Reads against a nonexistent DB short-circuit before any
                # transport call: the engine would auto-create+migrate the
                # path, and reads must not create or register anything.
                if not os.path.exists(self.db_path):
                    return self._fail(
                        "not_initialized",
                        "the configured Covey database does not exist; create a worklist "
                        "first (reads never initialize the DB)",
                    )
            if isinstance(action, ListsAction):
                return self._lists(action)
            if isinstance(action, (ShowMetaAction, ShowTaskAction)):
                return self._show(action)
            if isinstance(action, ResumeAction):
                return self._resume(action)
            if isinstance(action, CandidatesAction):
                return self._candidates(action)
            if isinstance(action, AvailabilityAction):
                return self._availability(action)
            if isinstance(action, StuckAction):
                return self._stuck(action)
            requested = action.op if isinstance(action, RetryFailAction) else str(arguments.get("action") or "")
            return self._mutation(action, requested)
        except Exception as exc:  # unexpected defects stay visible, sanitized
            logger.warning("covey-tasks runtime internal error: %s", _redact(str(exc)))
            return self._fail("internal_error", f"unexpected runtime error: {exc}")

    # -- reads ----------------------------------------------------------------

    def _require_capabilities(self) -> dict | None:
        if not self._task_operations_supported():
            caps = self._capabilities or {}
            return self._fail(
                "unsupported_binary",
                "Covey binary lacks required capabilities: "
                + ", ".join(sorted(k for k, v in caps.items() if not v)),
                suggestions=[
                    "Install or rebuild the newer covey binary that supports meta list and "
                    "explicit completion/evidence policy flags on subtask create and finish.",
                ],
            )
        return None

    def _status(self) -> dict:
        caps = self._probe_capabilities()
        claim = self._claim
        return self._ok(
            {
                "binary": self._resolved_binary,
                "db_path": self.db_path,
                "profile_home": self.profile_home,
                "capabilities": caps,
                "agent_bound": self.agent_ref is not None and self.agent_ref() is not None,
                "orchestrator_registered": "orchestrator" in self._token,
                "executor_registered": "executor" in self._token,
                "active_claim": (
                    {
                        "subtask_id": claim.subtask_id,
                        "claim_id": claim.claim_id,
                        "fence_seq": claim.fence_seq,
                        "lease_deadline_ms": claim.lease_deadline_ms,
                        "uncertain": claim.uncertain,
                    }
                    if claim is not None
                    else None
                ),
                "ownership_healthy": claim is not None and not claim.uncertain,
                "lost_reason": _redact(self._lost_reason) if self._lost_reason else None,
                "maintenance_running": (
                    self._thread is not None and self._thread.is_alive()
                    and not self._stop_event.is_set()
                ),
                "closed": self._closed,
            }
        )

    def _lists(self, action: ListsAction) -> dict:
        # The meta list envelope carries its rows as a JSON list directly in
        # data (never a wrapped dict), so data_list=True for the parse gate.
        payload = self._call(
            ["meta", "list", _opt("--limit", action.limit), _opt("--offset", action.offset)],
            data_list=True,
        )
        if payload.get("ok") is not True:
            return _map_transport_failure(payload)
        rows = payload.get("data")
        if not isinstance(rows, list):
            return self._fail("invalid_envelope", "meta list returned a non-list data field")
        items = []
        for row in rows:
            if not isinstance(row, dict) or not {"meta_task_id", "prompt_text", "state", "created_at", "updated_at"} <= set(row):
                return self._fail("invalid_envelope", "meta list row had unexpected shape")
            items.append(_project_list_item(row))
        # The worklist label describes the state machine, not success: a
        # completed worklist is closed, and never claims all work succeeded.
        for item in items:
            if item["state"] == "completed":
                item["label"] = "closed"
        return self._ok({"items": items, "limit": action.limit, "offset": action.offset})

    def _show(self, action: Any) -> dict:
        if isinstance(action, ShowMetaAction):
            return self._show_meta(action)
        return self._show_task(action)

    def _show_meta(self, action: ShowMetaAction) -> dict:
        payload = self._call(["meta", "status", _opt("--meta-task-id", action.meta_task_id)])
        if payload.get("ok") is not True or not isinstance(payload.get("data"), dict):
            return _map_transport_failure(payload)
        data = payload["data"]
        meta = data.get("meta_task")
        subtasks = data.get("subtasks")
        if not isinstance(meta, dict) or not isinstance(subtasks, list):
            return self._fail("invalid_envelope", "meta status response had unexpected shape")
        # ponytail: counts are O(tasks in worklist); the CLI returns the full
        # list internally. Move pagination into Covey if measured need appears.
        counts: dict[str, int] = {}
        records = []
        for task in subtasks:
            if not isinstance(task, dict) or "state" not in task:
                return self._fail("invalid_envelope", "meta status subtask row had unexpected shape")
            counts[task["state"]] = counts.get(task["state"], 0) + 1
            records.append(_project_task(task))
        page = records[action.offset : action.offset + action.limit]
        return self._ok(
            {
                "meta_task": _project_meta(meta),
                "subtasks": page,
                "counts": counts,
                "limit": action.limit,
                "offset": action.offset,
                "total": len(records),
                "paged": len(records) > len(page),
            }
        )

    def _observed_now_ms(self) -> int:
        return int(time.time() * 1000)

    def _show_task(self, action: ShowTaskAction) -> dict:
        payload = self._call(["subtask", "status", _opt("--subtask-id", action.subtask_id)])
        if payload.get("ok") is not True or not isinstance(payload.get("data"), dict):
            return _map_transport_failure(payload)
        data = payload["data"]
        task = data.get("subtask")
        outcomes = data.get("attempt_outcomes")
        if not isinstance(task, dict) or not isinstance(outcomes, list):
            return self._fail("invalid_envelope", "subtask status response had unexpected shape")
        claim = data.get("claim")
        observed_at = self._observed_now_ms()
        # Observational health only: no held claim -> unclaimed; deadline
        # <= observation -> lease_expired (persisted state may lag expiry);
        # otherwise held is a snapshot, not proof the owner session is active.
        if claim is None:
            health = "unclaimed"
        else:
            if not isinstance(claim, dict) or "lease_deadline" not in claim:
                return self._fail("invalid_envelope", "subtask status claim had unexpected shape")
            health = "lease_expired" if int(claim["lease_deadline"]) <= observed_at else "held"
        return self._ok(
            {
                "subtask": _project_task(task),
                "attempt_outcomes": [_project_outcome(row) for row in outcomes if isinstance(row, dict)],
                "ownership_health": health,
                "observed_at": observed_at,
            }
        )

    def _resume(self, action: ResumeAction) -> dict:
        """Token-free recovery view: original task/outcome/fence provenance
        plus VALIDATED marked handoff records.  Summary text is never proof;
        unmarked or malformed summaries stay visible but unverified."""
        shown = self._show_task(ShowTaskAction(subtask_id=action.subtask_id))
        if shown.get("ok") is not True:
            return shown
        data = shown["data"]
        handoffs = []
        for outcome in data["attempt_outcomes"]:
            record = decode_handoff(outcome.get("summary"))
            handoffs.append({
                "claim_id": outcome["claim_id"],
                "fence_seq": outcome["fence_seq"],
                "outcome_kind": outcome["outcome_kind"],
                "failure_code": outcome["failure_code"],
                "evidence_digest": outcome["evidence_digest"],
                "recorded_at": outcome["recorded_at"],
                "validated_handoff": record,         # structurally validated marked record
                "unverified_summary": record is None and outcome.get("summary"),
            })
        data["resume_handoffs"] = handoffs
        data["note"] = ("validated_handoff is a structurally validated record only: "
                        "its claims (objective/changes/verification refs) are the prior "
                        "attempt's self-report and must be re-verified, not trusted")
        return self._ok(data)

    def _candidates(self, action: CandidatesAction) -> dict:
        argv = ["subtask", "candidates", _opt("--role", action.role),
                _opt("--limit", action.limit)]
        if action.meta_task_id:
            argv.append(_opt("--meta-task-id", action.meta_task_id))
        if action.routing_key:
            argv.append(_opt("--routing-key", action.routing_key))
        payload = self._call(argv, data_list=True)
        if payload.get("ok") is not True:
            return _map_transport_failure(payload)
        rows = payload.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return self._fail("invalid_envelope", "subtask candidates returned malformed rows")
        # Native SubtaskCandidate rows are FLAT (subtask_id/title/state at the
        # top level, no wrapper, no evidence_policy): project them as-is —
        # wrapping or filtering on a nested "subtask" silently empties the
        # result even when claimable work exists.
        items = []
        for row in rows:
            if "subtask_id" not in row or "state" not in row:
                return self._fail("invalid_envelope",
                                  "subtask candidate row had unexpected shape")
            items.append({k: row[k] for k in CANDIDATE_KEYS if k in row})
        return self._ok({"items": items, "limit": action.limit, "role": action.role,
                         "total": len(items)})

    def _availability(self, action: AvailabilityAction) -> dict:
        argv = ["subtask", "availability"]
        if action.meta_task_id:
            argv.append(_opt("--meta-task-id", action.meta_task_id))
        if action.routing_key:
            argv.append(_opt("--routing-key", action.routing_key))
        payload = self._call(argv)
        if payload.get("ok") is not True or not isinstance(payload.get("data"), dict):
            return _map_transport_failure(payload)
        return self._ok(payload["data"])

    def _stuck(self, action: StuckAction) -> dict:
        payload = self._call(
            ["subtask", "stuck", _opt("--older-than-ms", action.older_than_ms),
             _opt("--limit", action.limit)],
            data_list=True,
        )
        if payload.get("ok") is not True:
            return _map_transport_failure(payload)
        rows = payload.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return self._fail("invalid_envelope", "subtask stuck returned malformed rows")
        items = []
        for row in rows:
            subtask = row.get("subtask")
            if isinstance(subtask, dict):
                items.append({
                    "subtask": _project_task(subtask),
                    "claim": row.get("claim"),
                    "session": row.get("session"),
                    "idle_for_ms": row.get("idle_for_ms"),
                })
        return self._ok({"items": items, "limit": action.limit,
                         "older_than_ms": action.older_than_ms, "total": len(items)})

    # -- request idempotency ---------------------------------------------------

    def _request_state(self, action: Any) -> _RequestState:
        request_id = action.request_id
        content = json.dumps(action, sort_keys=True, default=str)
        digest = hashlib.sha256(content.encode()).hexdigest()
        state = self._request_states.get(request_id)
        if state is None:
            state = _RequestState(digest=digest)
            self._request_states[request_id] = state
            return state
        if state.digest != digest:
            # Same request id with changed content is a conflict, never a merge.
            raise ActionReject(
                "idempotency_conflict",
                f"request_id {request_id} was already used for different content; "
                "use a new request_id",
            )
        # An unresolved earlier attempt (timeout/malformed result) is resolved
        # by replaying this SAME request: the retained principal/subkeys make
        # the backend answer from its idempotency record or perform the effect
        # once.  Never a permanent lockout, never a fresh-key retry.
        for role, token in state.tokens.items():
            if token in self._dead_tokens:
                raise ActionReject(
                    "replay_refused",
                    f"the session token used for request_id {request_id} was retired; "
                    "inspect state with lists/show and issue a new explicit action "
                    "with a fresh request_id",
                )
        return state

    def _register_role(self, role: str, state: _RequestState) -> tuple[str, str | None, dict | None]:
        """Lazy role registration; the registration substep is retained so an
        explicit same-request retry reuses the original principal and key.
        Returns (role, token, error_envelope)."""
        if role in self._token:
            token = self._token[role]
            state.tokens[role] = token
            return role, token, None
        principal = state.principals.get(role)
        if principal is None:
            # Fresh per-runtime AND per-role principal; never derived from
            # session/profile, so a crashed principal is never reused.
            principal = f"hermes-covey-tasks:{role}:{uuid.uuid4()}"
            state.principals[role] = principal
        elif principal in self._dead_principals:
            return role, None, _envelope_failure(
                "replay_refused",
                "the principal registered for this request_id was retired; "
                "issue a new explicit action with a fresh request_id",
            )
        payload = self._call(
            [
                "session", "register",
                _opt("--agent-principal-id", principal),
                _opt("--agent-instance-id", f"{self.instance_id}:{role}"),
                _opt("--role", role),
                _opt("--idempotency-key", f"{principal}:register"),
            ]
        )
        if payload.get("ok") is True and isinstance(payload.get("data"), dict):
            token = payload["data"].get("session_token")
            if isinstance(token, str) and token:
                if token in self._dead_tokens:
                    # Idempotent register replay handed back a token we already
                    # retired; never remint retired authority.
                    self._dead_principals.add(principal)
                    return role, None, _envelope_failure(
                        "replay_refused",
                        "registration replay returned a retired session; "
                        "issue a new explicit action with a fresh request_id",
                    )
                if self._closed:
                    # Late in-flight registration must not publish authority.
                    self._attempt_exit(
                        role, token, max(0.1, min(self.teardown_timeout, NORMAL_TIMEOUT)),
                        teardown=True,
                    )
                    return role, None, _envelope_failure(
                        "closed", "runtime closed during session registration"
                    )
                self._token[role] = token
                self._token_principal[token] = principal
                state.tokens[role] = token
                return role, token, None
            return role, None, _envelope_failure(
                "invalid_envelope", "session register returned no session token"
            )
        if payload.get("code") in UNCERTAIN_CODES:
            state.uncertain = True
            return role, None, None
        return role, None, _map_transport_failure(payload)

    # -- mutations --------------------------------------------------------------

    def _mutation(self, action: Any, requested: str = "") -> dict:
        request_id = getattr(action, "request_id", None)
        if self.agent_ref is None or self.agent_ref() is None:
            # agent=None may perform diagnostics/reads but refuses all mutations.
            return self._fail(
                "agent_unbound",
                "no live Hermes agent is bound to this runtime; mutations are refused",
                request_id,
            )
        caps_error = self._require_capabilities()
        if caps_error is not None:
            return self._with_request_id(caps_error, request_id)
        # Serialize whole logical mutations with teardown/maintenance state.
        with self._lock:
            if self._closed:
                return self._fail(
                    "closed", "runtime is closed; host will create a fresh runtime", request_id
                )
            try:
                state = self._request_state(action)
                state.uncertain = False  # re-derived by this (replay) attempt
                if isinstance(action, CreateAction):
                    return self._with_request_id(self._do_create(action, state), request_id)
                if isinstance(action, AddAction):
                    return self._with_request_id(self._do_add(action, state), request_id)
                if isinstance(action, StartAction):
                    return self._with_request_id(self._do_start(action, state), request_id)
                return self._with_request_id(self._do_outcome(action, state, requested), request_id)
            except ActionReject as reject:
                return self._fail(reject.code, reject.message, request_id)

    @staticmethod
    def _with_request_id(env: dict, request_id: str | None) -> dict:
        if request_id is not None:
            env["request_id"] = request_id
        return env

    def _ensure_role(self, role: str, state: _RequestState, action: Any):
        """Returns (token, failure_envelope_or_None)."""
        registered_role, token, error = self._register_role(role, state)
        if token is None:
            if error is None:
                error = self._uncertain_result(
                    state, request_id=action.request_id,
                    failed_op=f"{role} session registration",
                )
            return None, error
        return token, None

    def _do_create(self, action: CreateAction, state: _RequestState) -> dict:
        token, error = self._ensure_role("orchestrator", state, action)
        if token is None:
            return error
        payload = self._call(
            ["meta", "submit", _opt("--session-token", token), _opt("--prompt-text", action.prompt),
             _opt("--idempotency-key", f"{token}:meta-submit:{action.request_id}")]
        )
        if payload.get("ok") is True and isinstance(payload.get("data"), dict):
            meta_task_id = payload["data"].get("meta_task_id")
            if isinstance(meta_task_id, str) and meta_task_id:
                return self._ok(
                    {
                        "meta_task_id": meta_task_id,
                        "state": "planning",
                        "guidance": CREATE_GUIDANCE,
                    },
                    action.request_id,
                )
        return self._failure_or_uncertain(payload, state, action.request_id, "meta submit",
                                          role="orchestrator")

    def _do_add(self, action: AddAction, state: _RequestState) -> dict:
        token, error = self._ensure_role("orchestrator", state, action)
        if token is None:
            return error
        # Explicit simple policy: direct completion, default route, optional
        # evidence. Completed/cancelled worklists are refused by the engine
        # with a conflict; we never reopen or silently roll over.
        payload = self._call(
            ["subtask", "create",
             _opt("--session-token", token),
             _opt("--meta-task-id", action.meta_task_id),
             _opt("--title", action.title),
             _opt("--kind", "work"),
             _opt("--priority", action.priority),
             _opt("--completion-policy", "direct"),
             _opt("--evidence-policy", "optional"),
             _opt("--routing-key", "default"),
             _opt("--idempotency-key", f"{token}:subtask-create:{action.request_id}")]
        )
        if payload.get("ok") is True and isinstance(payload.get("data"), dict):
            subtask_id = payload["data"].get("subtask_id")
            if isinstance(subtask_id, str) and subtask_id:
                return self._ok(
                    {
                        "subtask_id": subtask_id,
                        "meta_task_id": action.meta_task_id,
                        "title": action.title,
                        "priority": action.priority,
                        "state": "available",
                    },
                    action.request_id,
                )
        return self._failure_or_uncertain(payload, state, action.request_id, "subtask create",
                                          role="orchestrator")

    def _live_claim_matches(self, live: Any, claim: _Claim, token: str | None) -> bool:
        """Strictly typed check that the persisted claim is this executor's live one."""
        return (
            token is not None
            and isinstance(live, dict)
            and live.get("claim_id") == claim.claim_id
            and _is_int(live.get("fence_seq"))
            and live["fence_seq"] == claim.fence_seq
            and live.get("owner_session_token") == token
            and _is_int(live.get("lease_deadline"))
            and live["lease_deadline"] > self._observed_now_ms()
        )

    @staticmethod
    def _started_data(claim: _Claim) -> dict:
        data = {
            "subtask_id": claim.subtask_id,
            "claim_id": claim.claim_id,
            "fence_seq": claim.fence_seq,
            "lease_deadline_ms": claim.lease_deadline_ms,
            "state": "in_progress",
            "guidance": START_GUIDANCE,
        }
        if claim.workspace:
            data["workspace"] = claim.workspace
            data["workspace_guidance"] = (
                f"native file/terminal cwd is routed to {claim.workspace}; file mutations "
                "targeting paths outside it are refused. This is cooperative workspace "
                "isolation, not a security sandbox."
            )
        return data

    def _do_start(self, action: StartAction, state: _RequestState) -> dict:
        # Existing local claim: check its authoritative status first.
        claim = self._claim
        if claim is not None:
            # Authority of the CURRENT claim is established first, against its
            # own subtask; only then is a different requested task considered.
            status = self._read_task_state(claim.subtask_id)
            if status is None:
                return self._fail(
                    "outcome_unknown",
                    "the authoritative status of the current claim could not be confirmed",
                    action.request_id,
                )
            live_claim = status["claim"]
            if live_claim is not None and not isinstance(live_claim, dict):
                return self._fail(
                    "invalid_envelope", "subtask status claim had unexpected shape",
                    action.request_id,
                )
            token = self._token.get("executor")
            if self._live_claim_matches(live_claim, claim, token):
                claim.lease_deadline_ms = live_claim["lease_deadline"]
                persisted = status["subtask"].get("state")
                if (
                    claim.subtask_id == action.subtask_id
                    and not claim.started
                    and claim.request_id == action.request_id
                    and state.tokens.get("executor") == token
                ):
                    # Partial start of THIS request under the same valid
                    # ownership: truthful completion, never a second claim.
                    if persisted == "in_progress":
                        claim.started = True  # the earlier start did commit
                        self._drop_replaced_guard()
                        claim.uncertain = False
                        return self._ok(self._started_data(claim), action.request_id)
                    if persisted == "claimed":
                        return self._issue_start(action, token, claim)
                if claim.subtask_id != action.subtask_id:
                    # Valid live authority is never stolen or released here.
                    return self._fail(
                        "active_claim_held",
                        f"this agent already owns {claim.subtask_id}; finish, retry, fail, "
                        "release or abandon it before starting another task",
                        action.request_id,
                        data={"active_subtask_id": claim.subtask_id},
                    )
                return self._fail(
                    "active_claim_held",
                    f"this agent still holds the live claim on {claim.subtask_id}",
                    action.request_id,
                    data={"active_subtask_id": claim.subtask_id, "state": persisted},
                )
            # Local claim authority is gone: retire the old token (never reuse
            # it, even if exit fails).  A fresh claim under a replacement
            # token is a NEW explicit action, never a replay of this request.
            old_token = self._token.get("executor")
            self._retire_executor("local claim authority lost or expired")
            if old_token is not None and state.tokens.get("executor") == old_token:
                raise ActionReject(
                    "replay_refused",
                    f"the session token used for request_id {action.request_id} was retired; "
                    "inspect state with lists/show and issue a new explicit action "
                    "with a fresh request_id",
                )
            claim = None

        status = self._read_task_state(action.subtask_id)
        if status is None:
            return self._fail("outcome_unknown", "subtask status could not be read", action.request_id)
        task = status["subtask"]
        if task["state"] != "available":
            # A persisted claimed/in_progress state with no local authority is
            # an expired claim: `subtask claim` reclaims it inline (engine
            # expires stale claims before validating), so fall through to a
            # fresh claim rather than refusing. Terminal states are refused
            # here; the engine remains authoritative for everything else.
            if task["state"] not in ("claimed", "in_progress"):
                return self._fail(
                    "invalid_transition",
                    f"subtask {action.subtask_id} is in state {task['state']}; "
                    "start requires available",
                    action.request_id,
                    data={"subtask_id": action.subtask_id, "state": task["state"]},
                )
        if task["completion_policy"] != "direct":
            # Governed tasks refuse adapter execution; no policy changes.
            return self._fail(
                "governed_task_unsupported",
                f"subtask {action.subtask_id} uses completion policy "
                f"{task['completion_policy']}; this adapter only executes direct-completion tasks",
                action.request_id,
                data={"subtask_id": action.subtask_id, "completion_policy": task["completion_policy"]},
            )
        bound_workspace = ""
        if action.workspace:
            # Workspace binding is refused BEFORE any claim/subprocess side
            # effect: the target must be this machine's canonical LINKED Git
            # worktree (main checkout and superprojects are rejected).
            bound_workspace = _validate_workspace(action.workspace)
            if bound_workspace is None:
                return self._fail(
                    "workspace_invalid",
                    f"workspace {action.workspace!r} is not a canonical linked Git worktree "
                    "on this machine (main checkouts, superprojects and non-Git paths are "
                    "refused); pass the real linked worktree root",
                    action.request_id,
                )
            busy = self._acquire_workspace_lock(bound_workspace)
            if busy is not None:
                return self._with_request_id(busy, action.request_id)
        token, error = self._ensure_role("executor", state, action)
        if token is None:
            # Registration failed/timed out BEFORE the claim was published:
            # never strand the worktree flock on an unpublished start.
            self._release_workspace_lock()
            return error
        # Try to exit a retired pending token once, on the action thread.
        pending = self._retired.get("executor")
        if pending is not None:
            self._retired["executor"] = None
            self._attempt_exit("executor", pending, NORMAL_TIMEOUT)

        claim_payload = self._call(
            ["subtask", "claim", _opt("--session-token", token), _opt("--subtask-id", action.subtask_id),
             _opt("--lease-duration-ms", self.lease_duration_ms),
             _opt("--idempotency-key", f"{token}:claim:{action.request_id}")]
        )
        if claim_payload.get("ok") is True and isinstance(claim_payload.get("data"), dict):
            claim_data = claim_payload["data"]
            claim_id = claim_data.get("claim_id")
            fence_seq = claim_data.get("fence_seq")
            lease_deadline = claim_data.get("lease_deadline")
            if not (isinstance(claim_id, str) and _is_int(fence_seq) and _is_int(lease_deadline)):
                # Malformed claim response: the claim may exist natively but
                # was never installed locally; drop our unpublished lock so
                # a retry or peer is not blocked by this runtime's handle.
                self._release_workspace_lock()
                return self._fail(
                    "invalid_envelope", "claim response had unexpected shape", action.request_id
                )
        else:
            # A reaped idle executor (SessionNotActive) is retired here so the
            # next explicit start registers a fresh principal.
            env = self._failure_or_uncertain(
                claim_payload, state, action.request_id, "subtask claim", role="executor",
            )
            self._release_workspace_lock()
            return env
        # Publication is atomic: _set_claim refuses under the same guard that
        # serializes close()'s revocation, so a claim whose backend response
        # passed every earlier _closed check can still never publish into a
        # revoked runtime (the thread holds the runtime lock and can pass
        # any check-then-check pattern close's budget branch).
        if not self._set_claim(claim_id, fence_seq, lease_deadline,
                               action.subtask_id, action.request_id,
                               bound_workspace):
            # The native claim truthfully stays with lease expiry; only local
            # handles (flock/cwd routing) are released, nothing is published.
            self._release_workspace_lock()
            return self._fail(
                "closed",
                "runtime closed before the claim could be applied",
                action.request_id,
            )
        return self._issue_start(action, token, self._claim)

    def _issue_start(self, action: StartAction, token: str, claim: _Claim) -> dict:
        """Issue (or replay with the retained key) `subtask start` for a held claim."""
        start_payload = self._call(
            ["subtask", "start", _opt("--session-token", token), _opt("--claim-id", claim.claim_id),
             _opt("--fence-seq", claim.fence_seq),
             _opt("--idempotency-key", f"{token}:start:{action.request_id}")]
        )
        if start_payload.get("ok") is True and isinstance(start_payload.get("data"), dict):
            if self._closed:
                # Teardown already revoked state; do not publish authority.
                self._claim = None
                self._release_workspace_lock()
                self._guarded_workspace = claim.workspace or self._guarded_workspace
                self._stop_event.set()
                return self._fail(
                    "closed",
                    "runtime closed before the start result could be applied",
                    action.request_id,
                )
            claim.started = True
            self._drop_replaced_guard()
            claim.uncertain = False
            return self._ok(self._started_data(claim), action.request_id)
        # Truthful partial state: the claim exists; report the PERSISTED state.
        message = str(start_payload.get("message") or "subtask start failed")
        code = str(start_payload.get("code") or "transport_error")
        status = self._read_task_state(claim.subtask_id)
        persisted = status["subtask"].get("state") if status is not None else None
        data = {
            "subtask_id": action.subtask_id,
            "claim_id": claim.claim_id,
            "fence_seq": claim.fence_seq,
            "state": persisted if persisted is not None else "unknown",
            "claim_state": persisted if persisted is not None else "unknown",
            "started": (persisted == "in_progress") if persisted is not None else None,
            "note": "claim succeeded but start did not complete; retry with the same "
                    "request_id to re-issue start, or release the claim",
        }
        if _is_session_inactive(start_payload):
            self._retire_executor(f"executor session inactive during start: {message}")
            data["note"] = "the executor session is no longer active; the claim was lost"
            return self._fail(
                code, message, action.request_id,
                exit_code=start_payload.get("exit_code") if isinstance(start_payload.get("exit_code"), int) else None,
                data=data,
            )
        if code in UNCERTAIN_CODES:
            claim.uncertain = True
            return self._fail(
                "outcome_unknown",
                f"{message} (the claim may exist; reconcile before continuing)",
                action.request_id,
                data=data,
            )
        return self._fail(
            code, message, action.request_id,
            exit_code=start_payload.get("exit_code") if isinstance(start_payload.get("exit_code"), int) else None,
            data=data,
        )

    def _read_task_state(self, subtask_id: str) -> dict | None:
        payload = self._call(["subtask", "status", _opt("--subtask-id", subtask_id)])
        if payload.get("ok") is True and isinstance(payload.get("data"), dict):
            data = payload["data"]
            task = data.get("subtask")
            if isinstance(task, dict) and {"state", "completion_policy"} <= set(task):
                return {"subtask": task, "claim": data.get("claim"),
                        "attempt_outcomes": data.get("attempt_outcomes")}
        return None

    def _do_outcome(self, action: Any, state: _RequestState, requested: str = "") -> dict:
        claim = self._claim
        if claim is None or claim.subtask_id != action.subtask_id:
            return self._fail(
                "not_claimed",
                f"this agent has no active claim on {action.subtask_id}; start the task first",
                action.request_id,
            )
        if claim.uncertain:
            if not self._reconcile():
                if self._claim is None:
                    return self._fail(
                        "not_claimed",
                        f"ownership of {action.subtask_id} was lost "
                        f"({self._lost_reason or 'reconciliation found no live claim'}); "
                        "start the task again with a new request",
                        action.request_id,
                    )
                return self._fail(
                    "ownership_uncertain",
                    "the previous maintenance outcome is unresolved and reconciliation has not "
                    "confirmed a live held claim; inspect state with show first",
                    action.request_id,
                )
        token = self._token.get("executor")
        if token is None:
            return self._fail("executor_unregistered", "executor session token is unavailable",
                              action.request_id)
        state.tokens["executor"] = token  # request is scoped to this token

        # Required evidence applies to receipt operations only (finish/retry/fail);
        # abandon/release carry no evidence.
        evidence = getattr(action, "evidence_digest", None)
        if evidence is None and isinstance(action, (FinishAction, RetryFailAction, CheckpointAction)):
            status = self._read_task_state(action.subtask_id)
            if status is not None and status["subtask"].get("evidence_policy") == "required":
                # Required evidence is an error, never an invented digest.
                return self._fail(
                    "evidence_required",
                    f"subtask {action.subtask_id} has evidence policy required; "
                    "supply evidence_digest (algorithm-prefixed)",
                    action.request_id,
                )
        requested = str(getattr(action, "op", requested) or requested or "")
        if requested == "checkpoint":
            # Explicit checkpoint = native `retry`: ONE transaction persists
            # the marked handoff receipt and releases the claim atomically.
            op = "retry"
            summary = encode_handoff(action.handoff, claim.workspace)
            argv_tail = [_opt("--summary", summary), _opt("--failure-code", "checkpointed")]
            expected_kind = "retryable_failure"
        elif requested == "retry" and isinstance(action, RetryFailAction):
            summary = action.summary
            if action.handoff is not None:
                summary = encode_handoff(action.handoff, claim.workspace)
            op = "retry"
            argv_tail = [_opt("--summary", summary), _opt("--failure-code", action.failure_code)]
            expected_kind = "retryable_failure"
        elif requested == "fail" and isinstance(action, RetryFailAction):
            summary = action.summary
            if action.handoff is not None:
                summary = encode_handoff(action.handoff, claim.workspace)
            op = "fail"
            argv_tail = [_opt("--summary", summary), _opt("--failure-code", action.failure_code)]
            expected_kind = "terminal_failure"
        elif isinstance(action, FinishAction):
            summary = action.summary
            if action.handoff is not None:
                summary = encode_handoff(action.handoff, claim.workspace)
            op = "finish"
            argv_tail = [_opt("--summary", summary)]
            expected_kind = "succeeded"
        elif isinstance(action, ReleaseAbandonAction) and requested == "abandon":
            op, argv_tail = "abandon", []
            expected_kind = "abandoned"
        else:
            op, argv_tail = "release", []
            expected_kind = "released"
        receipt_kind = expected_kind if expected_kind in ("succeeded", "retryable_failure",
                                                          "terminal_failure") else None
        if op == "release":
            # Release lives under `claim`, not `subtask` (no SubtaskCommand::Release).
            argv = ["claim", "release", _opt("--session-token", token), _opt("--claim-id", claim.claim_id),
                    _opt("--fence-seq", claim.fence_seq),
                    _opt("--idempotency-key", f"{token}:{op}:{action.request_id}")]
        else:
            argv = ["subtask", op, _opt("--session-token", token), _opt("--claim-id", claim.claim_id),
                    _opt("--fence-seq", claim.fence_seq), *argv_tail]
            if evidence is not None:
                argv.append(_opt("--evidence-digest", evidence))
            argv.append(_opt("--idempotency-key", f"{token}:{op}:{action.request_id}"))
        payload = self._call(argv)
        if payload.get("ok") is not True or not isinstance(payload.get("data"), dict):
            return self._failure_or_uncertain(
                payload, state, action.request_id, f"{'claim' if op == 'release' else 'subtask'} {op}",
                role="executor",
            )
        confirmed = self._confirm_outcome(claim, op, receipt_kind)
        if confirmed is not True:
            if confirmed is None:
                return self._fail(
                    "outcome_unknown",
                    "the receipt/state could not be confirmed; reconcile with show before retrying",
                    action.request_id,
                    data={"subtask_id": claim.subtask_id, "state": "uncertain"},
                )
            return self._fail(
                confirmed, "persisted outcome contradicts the reported result", action.request_id,
                data={"subtask_id": claim.subtask_id},
            )
        # Revoke ownership before projecting a confirmed committed receipt:
        # a projection failure must never retain its claim or worktree lock.
        released = self._clear_claim_and_retire_executor()
        state.uncertain = False
        outcome = None
        if receipt_kind is not None:
            outcome = _project_outcome({
                "claim_id": claim.claim_id,
                "subtask_id": claim.subtask_id,
                "fence_seq": claim.fence_seq,
                "summary": summary,
                "outcome_kind": expected_kind,
                "evidence_digest": evidence,
                "failure_code": "checkpointed" if isinstance(action, CheckpointAction)
                                else getattr(action, "failure_code", None),
                "recorded_at": None,
            })
        env = self._ok(
            {"subtask_id": claim.subtask_id, "state": TERMINAL_STATES.get(op, "released"),
             "outcome": outcome},
            action.request_id,
        )
        if released is not None:
            env["diagnostics"] = released
        return env

    def _confirm_outcome(self, claim: _Claim, op: str,
                         receipt_kind: str | None) -> bool | str | None:
        """Authoritative post-mutation confirmation. True = confirmed;
        None = the check itself could not be performed (unknown outcome);
        otherwise a machine-readable mismatch code."""
        status = self._read_task_state(claim.subtask_id)
        if status is None:
            return None
        if receipt_kind is None:
            # abandon writes no attempt outcome; absence of the claim is
            # verified below together with the terminal state check.
            pass
        else:
            outcomes = status.get("attempt_outcomes") or []
            receipt_ok = any(
                isinstance(row, dict)
                and row.get("claim_id") == claim.claim_id
                and row.get("fence_seq") == claim.fence_seq
                and row.get("outcome_kind") == receipt_kind
                for row in outcomes
            )
            if not receipt_ok:
                return "receipt_mismatch"
        live = status.get("claim")
        if live is not None:
            # After release/retry the task is claimable again, so a DIFFERENT
            # live claim means another agent already took it; that is fine.
            if (
                op in ("release", "retry")
                and isinstance(live, dict)
                and live.get("claim_id") != claim.claim_id
            ):
                return True
            return "claim_still_held"
        if status["subtask"].get("state") != TERMINAL_STATES.get(op):
            return "state_mismatch"
        return True

    def _failure_or_uncertain(self, payload: dict, state: _RequestState,
                              request_id: str, failed_op: str, *,
                              role: str | None = None) -> dict:
        # An ok envelope that reached here failed the per-operation shape
        # check: the effect may have committed, so it is unknown, not failed.
        code = "invalid_envelope" if payload.get("ok") is True else str(
            payload.get("code") or "transport_error")
        if code in UNCERTAIN_CODES:
            state.uncertain = True
            return self._fail(
                "outcome_unknown",
                f"{failed_op} produced no conclusive result ({code}); the effect may have "
                f"committed. Inspect with lists/show, or retry the same request_id to replay "
                "it with the same idempotency key (never a fresh key).",
                request_id,
            )
        env = _map_transport_failure(payload)
        if role is not None and _is_session_inactive(payload):
            # Reaped/exited session: report the failed action, retire the role
            # token so the next explicit action registers fresh, no replay.
            self._retire_role(role, f"{role} session is no longer active ({failed_op})")
            env["data"] = {
                "retired_role": role,
                "note": "the session was no longer active and was retired; "
                        "issue a new explicit action",
            }
        return env

    def _uncertain_result(self, state: _RequestState, *, request_id: str, failed_op: str) -> dict:
        if state.uncertain:
            return self._fail(
                "outcome_unknown",
                f"{failed_op} produced no conclusive result; the role principal is retained — "
                f"retry with the same request_id (the original principal and idempotency key "
                "are reused), or reconcile with lists/show",
                request_id,
            )
        return self._fail("transport_error", f"{failed_op} failed", request_id)

    # -- claim authority / maintenance ---------------------------------------------

    def _drop_replaced_guard(self) -> None:
        """Successful unbound claim: the previous workspace guard is dropped
        (legacy coordination-only work is no longer bound); the fresh claim
        itself has no workspace, so nothing is guarded.  Never drops the
        guard of a claim that carries its own workspace."""
        claim = self._claim
        if self._replaced_guard is not None and claim is not None and not claim.workspace:
            self._guarded_workspace = None
            self._replaced_guard = None

    def _set_claim(self, claim_id: str, fence_seq: int, lease_deadline_ms: int,
                   subtask_id: str, request_id: str, workspace: str = "") -> bool:
        # Publication is atomic against close()'s revocation on the same
        # _close_guard used by workspace_guard and the revoke block: an
        # in-flight action thread holds the runtime lock, so close's budget
        # branch cannot rely on checks OUTSIDE this guard — the thread can
        # pass any _closed check and continue publishing.  Inside the guard,
        # either the claim publishes and close's revoke then restores cwd
        # (revocation follows every publication), or the claim is refused.
        # The critical section holds host updates only — never the runtime
        # lock wait, a subprocess, or an exit attempt.
        with self._close_guard:
            if self._closed:
                return False  # revoked: never publish authority after revocation
            self._claim = _Claim(
                subtask_id=subtask_id, claim_id=claim_id, fence_seq=fence_seq,
                lease_deadline_ms=lease_deadline_ms, request_id=request_id,
                workspace=workspace,
            )
            if workspace:
                # Fresh valid start on a bound workspace: the guard ENGAGES for it
                # (live claim: path-checked; claim lost: all guarded mutations
                # refused until the next valid start) and the agent's native
                # file/terminal cwd is routed there.
                self._guarded_workspace = workspace
                self._bind_session_cwd(workspace)
            else:
                # Unbound claims must never re-inherit the PREVIOUS workspace
                # guard.  It is dropped only when the new claim actually STARTS
                # (see _issue_start); a partial/failed start is still a live
                # claim, so its stale guard stays closed until the same request
                # completes or the claim is retired.
                self._replaced_guard = self._guarded_workspace
            self._lost_reason = None
            self._last_heartbeat_mono = time.monotonic()
            self._last_renew_mono = time.monotonic()
            self._start_maintenance()
        return True

    def _start_maintenance(self) -> None:
        if self._closed:
            return
        thread = self._thread
        if thread is not None and thread.is_alive() and not self._stop_event.is_set():
            return
        # Each loop owns its OWN stop event: a still-draining loop that was
        # told to stop can never veto or inherit a new claim's maintenance.
        stop = threading.Event()
        self._stop_event = stop
        self._thread = threading.Thread(
            target=self._maintenance_loop, args=(stop,),
            name=f"covey-tasks-maint-{self.instance_id}", daemon=True,
        )
        self._thread.start()

    def _maintenance_loop(self, stop: threading.Event) -> None:
        interval = max(min(self.heartbeat_interval_ms, self.renew_interval_ms), 50) / 1000.0
        while not stop.wait(interval):
            try:
                self.maintenance_once()
            except Exception as exc:  # keep the daemon alive; report next status
                logger.warning("covey-tasks maintenance tick failed: %s", _redact(str(exc)))

    def maintenance_once(self) -> None:
        """One maintenance tick; shared with the background thread (test seam)."""
        with self._lock:
            if self._closed or self._claim is None:
                return
            if self.agent_ref is not None and self.agent_ref() is None:
                # Weak agent death is the stop signal; do bounded best-effort
                # cleanup (never from a GC callback).
                self._retire_executor("agent object was garbage collected")
                self._attempt_exit("executor", self._retired.get("executor") or "", max(1.0, self.teardown_timeout))
                self._retired["executor"] = None
                self._stop_event.set()
                return
            claim = self._claim
            if claim.uncertain:
                # Proof first: no heartbeat/renew until an authoritative status
                # confirms this executor's live claim (or proves it lost).
                if not self._reconcile():
                    return
                claim = self._claim
                if claim is None:
                    return
            token = self._token.get("executor")
            if token is None:
                return
            now = time.monotonic()
            heartbeat_due = (now - self._last_heartbeat_mono) * 1000 >= self.heartbeat_interval_ms
            renew_due = (now - self._last_renew_mono) * 1000 >= self.renew_interval_ms
            if heartbeat_due:
                hb = self._call(["session", "heartbeat", _opt("--session-token", token)])
                if self._closed:
                    return  # a result returning after teardown publishes nothing
                self._last_heartbeat_mono = time.monotonic()
                if hb.get("ok") is not True:
                    self._handle_heartbeat_failure(hb)
                    return
                renew_due = True
            if renew_due:
                self._renew_once(claim)

    def _handle_heartbeat_failure(self, hb: dict) -> None:
        claim = self._claim
        if claim is None:
            return
        if _is_definite_authority_loss(hb):
            # SessionNotActive / heartbeat IllegalTransition(Session) from an
            # external reaper = definite authority loss; retire without replay.
            self._retire_executor(
                f"executor session heartbeat failed: {str(hb.get('message') or '')}"
            )
            return
        # Timeout, malformed output, DB busy/internal error, missing binary...:
        # not proof of loss.  Stay uncertain; the next tick reconciles
        # authoritatively before any renew.
        claim.uncertain = True

    def _renew_once(self, claim: _Claim) -> None:
        token = self._token.get("executor")
        if token is None:
            return
        now_ms = self._observed_now_ms()
        # Headroom-restoring extension from the last authoritative deadline.
        extend_by_ms = max(1, min(now_ms + self.lease_duration_ms - claim.lease_deadline_ms,
                                  self.lease_duration_ms))
        # Fresh key every tick: never replay a previous tick's idempotency key.
        payload = self._call(
            ["claim", "renew", _opt("--session-token", token), _opt("--claim-id", claim.claim_id),
             _opt("--fence-seq", claim.fence_seq), _opt("--extend-by-ms", extend_by_ms)]
        )
        if self._closed:
            return
        self._last_renew_mono = time.monotonic()
        if payload.get("ok") is True and isinstance(payload.get("data"), dict):
            deadline = payload["data"].get("lease_deadline")
            if _is_int(deadline):
                claim.lease_deadline_ms = deadline
                self._last_heartbeat_mono = max(self._last_heartbeat_mono,
                                                time.monotonic() - self.heartbeat_interval_ms / 1000.0)
                return
            claim.uncertain = True
            return
        if _is_definite_authority_loss(payload):
            # Engine rejects already-expired/foreign/stale claims; that is
            # retirement/reclaim, not renewal success.
            self._retire_executor(f"lease renewal failed: {str(payload.get('message') or '')}")
            return
        claim.uncertain = True

    def _reconcile(self) -> bool:
        """Authoritative check of the current live held claim; returns True if
        ownership is confirmed and uncertainty is cleared.  Retires the
        executor only on an authoritative mismatch, never on a read fault."""
        if self._closed:
            return False
        claim = self._claim
        token = self._token.get("executor")
        if claim is None or token is None:
            return False
        status = self._read_task_state(claim.subtask_id)
        if status is None:
            return False
        live = status.get("claim")
        if live is not None and not isinstance(live, dict):
            return False
        if self._live_claim_matches(live, claim, token):
            claim.lease_deadline_ms = live["lease_deadline"]
            claim.uncertain = False
            return True
        self._retire_executor("reconciliation found no live claim held by this executor")
        return False

    def _mark_dead(self, token: str | None) -> None:
        """Record a retired token (and its principal) so it is never re-minted
        or replayed, even after its pending exit slot is cleared."""
        if token is None:
            return
        self._dead_tokens.add(token)
        principal = self._token_principal.get(token)
        if principal is not None:
            self._dead_principals.add(principal)

    def _retire_role(self, role: str, reason: str) -> None:
        if role == "executor":
            self._retire_executor(reason)
            return
        # Orchestrator: reaped/exited sessions have nothing left to exit.
        self._mark_dead(self._token.pop(role, None))

    def _retire_executor(self, reason: str) -> None:
        """Definite authority loss: retire the executor TOKEN itself, record
        one pending exit, stop maintenance, and never act on it again."""
        claim = self._claim
        self._claim = None
        self._release_workspace_lock()
        if claim is not None and claim.workspace and self._guarded_workspace is None:
            self._guarded_workspace = claim.workspace  # open guard until fresh valid start
        self._lost_reason = reason
        self._stop_event.set()
        token = self._token.pop("executor", None)
        if token is not None:
            self._retired["executor"] = token
            self._mark_dead(token)

    def _clear_claim_and_retire_executor(self) -> dict | None:
        """Definite outcome cleared: clear claim, stop renewals, retire token
        with one bounded best-effort exit attempt on the action thread."""
        claim = self._claim
        self._claim = None
        self._release_workspace_lock()
        if claim is not None and claim.workspace and self._guarded_workspace is None:
            self._guarded_workspace = claim.workspace  # open guard until fresh valid start
        self._stop_event.set()
        token = self._token.pop("executor", None)
        diagnostics = None
        if token is not None:
            self._mark_dead(token)
            diagnostics = self._attempt_exit("executor", token, NORMAL_TIMEOUT)
        return diagnostics

    def _attempt_exit(self, role: str, token: str, timeout: float, *,
                      teardown: bool = False) -> dict | None:
        if not token:
            return None
        if self._closed and not teardown:
            return _envelope_failure("closed", "runtime closed; session exit was not attempted")
        payload = self._call(["session", "exit", _opt("--session-token", token)],
                             timeout=timeout, teardown=True)
        if payload.get("ok") is True:
            return None
        # Cleanup failures are sanitized diagnostics, never successful exits.
        return _map_transport_failure(payload)

    # -- teardown --------------------------------------------------------------

    def close(self) -> None:
        """Bounded, idempotent teardown.  Revokes local authority first —
        the short publish mutex may wait for an in-flight publication's
        atomic host updates (never for a subprocess or the runtime lock),
        and the wait is charged to the budget — then exits each role once
        within a single total monotonic deadline that also covers thread
        join and lock acquisition."""
        # The total teardown budget is fixed BEFORE the publish-revoke guard:
        # time waiting for an in-flight publication (host updates only) is
        # charged against the budget and cannot grant a fresh deadline to
        # the join/lock/backend operations that follow.
        deadline = time.monotonic() + max(0.1, self.teardown_timeout)
        with self._close_guard:
            if self._close_started:
                return
            self._close_started = True
            self._closed = True  # gates every action, tick and backend call
            self._epoch += 1
            self._stop_event.set()
            self._guarded_workspace = None  # closed runtime guards nothing;
            # fresh runtime decides.  Cwd routing is revoked together with
            # authority: a late `workspace_guard` can then never re-bind, and
            # the budget-exhausted branch below is the last chance to stop
            # stale cwd from routing ordinary relative writes into a retired
            # worktree (engine fences do not govern plain filesystem writes).
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            # The in-flight command consumed the budget: stay revoked — the
            # native claim outcome is uncertain, so it truthfully stays with
            # lease expiry (never fabricate a release).  But cwd routing and
            # the worktree flock are LOCAL and ours to release now: this
            # runtime is evicted from the plugin registry, nothing else will
            # clean the cwd record, and the quiescent-config boundary cannot
            # repair a retired runtime.  The late in-flight thread sees
            # `_closed` (the workspace_guard and _set_claim checks under the
            # publish guard refuse publication) and cannot re-bind.
            self._release_workspace_lock()
            logger.warning("covey-tasks teardown budget exhausted before exit; relying on lease expiry")
            return
        try:
            self._claim = None
            self._release_workspace_lock()
            # exit_session releases held claims and clears active_subtask_id;
            # no redundant claim-release call.
            for role in ("executor", "orchestrator"):
                tokens = [self._retired.pop(role, None), self._token.pop(role, None)]
                for token in tokens:
                    if not token:
                        continue
                    self._mark_dead(token)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # Budget exhausted: leave revoked state.
                        continue
                    diag = self._attempt_exit(role, token, remaining, teardown=True)
                    if diag is not None:
                        logger.warning("covey-tasks teardown exit for %s failed: %s", role,
                                       _redact(str(diag.get("message") or "")))
        finally:
            self._lock.release()

