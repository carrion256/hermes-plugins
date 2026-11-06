"""covey-tasks — durable finite worklists and fenced task ownership via the Covey CLI.

One tool, `covey_tasks`; one runtime per live bound agent + profile + DB. The
runtime (runtime.py, loaded from this directory) owns sessions, claims, leases
and idempotency; this module owns identity binding, capability discovery and
teardown. Native `todo_list` is deliberately untouched: native todos are
conversation execution steps, Covey tasks are durable shared work.

Session identity follows the native active-agent binding (see prewalk): the
turn facade binds the agent for the whole conversation, so `_bound_agent()`
resolves on every legitimate dispatch path. Runtime identity is the live agent
OBJECT, not its session_id: compaction rotates session_id on the same object
and must preserve ownership, while children/reset/new processes never inherit
a token. Runtimes are tracked by weak agent identity, so hook cleanup can
dereference them at hook time instead of trusting a stale alias table.

Settings and the profile home are resolved on EVERY call/hook, not captured in
register(): the native manager is shared across multiplexed profiles, and both
`ctx.get_config` and `get_hermes_home()` are context-local reads.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import threading
import weakref
from pathlib import Path
from typing import Final

# Maintenance timing. A long task can legitimately outlive the 300 s claim
# lease, so maintenance ticks every 60 s and renews with headroom-restoring
# extensions. These are process-level constants, not per-profile config:
# operators cannot mis-tune them into ownership loss.
LEASE_DURATION_MS = 300000
RENEW_INTERVAL_MS = 60000
HEARTBEAT_INTERVAL_MS = 60000

DEFAULT_BINARY = "covey"

# loop-guard owns the redirect budget; the bridge fires only when that budget is
# spent, so "exhausted" cannot mean two different things in two plugins.  An
# unreadable setting falls back to loop-guard's own documented default.
LOOP_GUARD_PLUGIN: Final = "loop-guard"
LOOP_GUARD_DEFAULT_REDIRECT_CAP: Final = 2

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).resolve().parent
_RUNTIME_MODULE = "_covey_tasks_runtime"
_LOAD_LOCK = threading.Lock()


def _load_runtime_module():
    """Load runtime.py once as a private, sys.modules-registered module.

    The module must be in sys.modules BEFORE exec: dataclass (and typing)
    resolve string annotations through sys.modules[cls.__module__]. A failed
    exec leaves no half-initialised entry behind.
    """
    module = sys.modules.get(_RUNTIME_MODULE)
    if module is not None:
        return module
    with _LOAD_LOCK:
        module = sys.modules.get(_RUNTIME_MODULE)
        if module is not None:
            return module
        spec = importlib.util.spec_from_file_location(
            _RUNTIME_MODULE, _PLUGIN_DIR / "runtime.py"
        )
        if spec is None or spec.loader is None:
            raise ImportError("covey-tasks runtime.py cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_RUNTIME_MODULE] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(_RUNTIME_MODULE, None)
            raise
        return module


def _bound_agent():
    """The agent whose live turn is bound in this thread's context, or None."""
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
        return get_active_subagent_parent()
    except Exception:
        return None


def _profile_home() -> str:
    """Canonical home of the profile active in THIS context ('' if unknown)."""
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home().resolve())
    except Exception:
        return ""


TOOL_SCHEMA = {
    "name": "covey_tasks",
    "description": (
        "Durable shared task lists backed by Covey. Actions: status; "
        "lists(limit, offset) to discover worklists; show(meta_task_id) or "
        "show(subtask_id) for authoritative state; resume(subtask_id) for "
        "prior-attempt recovery context (validated marked handoffs + "
        "provenance, self-reported claims must be re-verified); "
        "candidates(role, [meta_task_id, routing_key, limit]), availability "
        "([meta_task_id, routing_key]) and stuck(older_than_ms, [limit]) "
        "read-only scheduler diagnostics; create(prompt) for a new "
        "finite worklist; add(meta_task_id, title, priority) for a work task; "
        "start(subtask_id, [workspace]) to claim+start the exact task, "
        "optionally binding it to a canonical linked Git worktree (cooperative "
        "workspace isolation: file/terminal cwd is routed there, explicit file "
        "mutations outside it are refused); finish/retry/fail "
        "(subtask_id, summary[, evidence_digest, handoff]); checkpoint "
        "(subtask_id, handoff[, evidence_digest]) persists a marked resumable "
        "handoff and returns the task to the pool via native retry; "
        "release(subtask_id); abandon(subtask_id). Plan and add every known "
        "task BEFORE starting execution; once a worklist is completed it stays "
        "history — create a new one. Any direct work task can be started; the "
        "task's persisted evidence policy is still enforced (a task that "
        "requires evidence needs evidence_digest to finish), and governed "
        "tasks stay outside this tool. Completion here is not review or "
        "landing verification. finish additionally accepts governed=true "
        "(opt-in, per call): it then refuses unless this session's continuation "
        "review closure is closed and records the evidence digest derived from "
        "that reviewed evidence; without it, finish stays direct."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "lists", "show", "create", "add", "start",
                         "finish", "retry", "fail", "release", "abandon",
                         "checkpoint", "resume", "candidates", "availability",
                         "stuck"],
            },
            "meta_task_id": {"type": "string"},
            "subtask_id": {"type": "string"},
            "prompt": {"type": "string"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "failure_code": {"type": "string"},
            "evidence_digest": {"type": "string"},
            "governed": {
                "type": "boolean",
                "description": ("finish only, opt-in per call: refuse the finish unless "
                                "this session's continuation review closure is closed and "
                                "record the evidence digest derived from that reviewed "
                                "evidence. Default false keeps finish direct."),
            },
            "request_id": {"type": "string"},
            "workspace": {"type": "string"},
            "handoff": {
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "changes": {"type": "array", "items": {"type": "string"}},
                    "verification_refs": {"type": "array", "items": {"type": "string"}},
                    "failed_approaches": {"type": "array", "items": {"type": "string"}},
                    "remaining": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["objective", "changes", "verification_refs",
                             "failed_approaches", "remaining"],
                "additionalProperties": False,
            },
            "role": {"type": "string", "enum": ["executor", "reviewer"]},
            "routing_key": {"type": "string"},
            "older_than_ms": {"type": "integer", "minimum": 1},
            "priority": {"type": "integer", "minimum": 0, "maximum": 1000},
            "limit": {"type": "integer"},
            "offset": {"type": "integer"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _failure(code: str, message: str, suggestions: str | None = None) -> str:
    payload = {"ok": False, "code": code, "message": message}
    if suggestions:
        payload["suggestions"] = suggestions
    return json.dumps(payload)


_CONFIG_HELP = (
    "Set covey-tasks settings for this profile: binary (default "
    "'covey') and db_path (absolute local SQLite path). The plugin "
    "never falls back to ambient config or creates a default DB."
)


def _read_settings(ctx) -> tuple[str, str, str]:
    """(binary, db_path, canonical_db) for the profile active NOW, or raise ValueError.

    Wrong types are rejected, never coerced: a number or list must not become
    a path or executable name.
    """
    try:
        binary = ctx.get_config("binary", DEFAULT_BINARY)
        db_path = ctx.get_config("db_path", "")
    except Exception as exc:
        raise ValueError(f"covey-tasks settings could not be read ({type(exc).__name__}).") from None
    if binary is None:
        binary = DEFAULT_BINARY
    if not isinstance(binary, str) or not binary.strip():
        raise ValueError("covey-tasks setting 'binary' must be a non-empty executable name or absolute path string.")
    if (
        not isinstance(db_path, str)
        or not db_path
        or not os.path.isabs(db_path)
        or ".." in Path(db_path).parts
    ):
        raise ValueError(
            "covey-tasks setting 'db_path' must be an absolute local path "
            "string to the Covey SQLite database for this profile; no default "
            "or fallback DB is used."
        )
    return binary, db_path, os.path.realpath(db_path)


def _blocker_mode(ctx) -> str:
    """The validated `blocker_checkpoint` mode for the profile active NOW.

    The vocabulary belongs to the runtime (one definition, one refusal), so an
    unknown value is a ValueError that fails registration instead of quietly
    behaving like some other mode.
    """
    try:
        raw = ctx.get_config("blocker_checkpoint", "exhausted")
    except (AttributeError, LookupError, TypeError, ValueError) as exc:
        raise ValueError(
            f"covey-tasks setting 'blocker_checkpoint' could not be read "
            f"({type(exc).__name__}: {exc}).") from exc
    return _load_runtime_module().parse_blocker_mode(raw)


def _loop_guard_redirect_cap() -> int:
    """loop-guard's own redirect budget, read with the host config reader.

    The default mode waits for the guard to spend this budget, so reading it at
    the source is the only way the two plugins cannot drift apart.  A missing or
    malformed value degrades to loop-guard's documented default and says so:
    a bridge that silently never fires is worse than a counted, logged one.
    """
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        raw = cfg_get(load_config_readonly(), "plugins", "entries",
                       LOOP_GUARD_PLUGIN, "settings", "redirect_cap",
                       default=LOOP_GUARD_DEFAULT_REDIRECT_CAP)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logger.warning(
            "covey-tasks: loop-guard budget could not be read (%s); using default %s",
            type(exc).__name__, LOOP_GUARD_DEFAULT_REDIRECT_CAP)
        return LOOP_GUARD_DEFAULT_REDIRECT_CAP
    if isinstance(raw, bool) or not isinstance(raw, int):
        logger.warning(
            "covey-tasks: loop-guard setting 'redirect_cap' is not an integer (%r); "
            "using default %s", raw, LOOP_GUARD_DEFAULT_REDIRECT_CAP)
        return LOOP_GUARD_DEFAULT_REDIRECT_CAP
    return raw


def _selected_guard_directive(prefix: str, selected_directive_id: object,
                              directives: object) -> tuple[str, str] | None:
    """The authoritative delivered loop-guard block as (directive_id, message).

    Only the SELECTED directive counts: a suppressed guard block never reached
    the model, so checkpointing on it would release a claim over work the model
    was never shown.  Returns None for anything else, including a selected
    directive from another plugin.
    """
    if not isinstance(directives, list):
        return None
    records = [item for item in directives if isinstance(item, dict)]
    selected = None
    for record in records:
        if record.get("directive_id") == selected_directive_id and selected_directive_id:
            selected = record
            break
    if selected is None and not selected_directive_id:
        delivered = [r for r in records if r.get("outcome") == "delivered"]
        selected = delivered[-1] if delivered else None
    if selected is None or selected.get("action") != "block":
        return None
    if selected.get("outcome") not in (None, "delivered"):
        return None
    message = selected.get("message")
    directive_id = selected.get("directive_id")
    if not isinstance(message, str) or not message.startswith(prefix):
        return None
    if not isinstance(directive_id, str) or not directive_id:
        return None
    return directive_id, message


class _Entry:
    __slots__ = ("runtime", "ref")

    def __init__(self, runtime, ref):
        self.runtime = runtime
        self.ref = ref  # weakref to the live agent; None for diagnostic runtimes


def register(ctx) -> None:
    runtime_module = _load_runtime_module()
    CoveyRuntime = runtime_module.CoveyRuntime
    # Fail at registration, not at the first stall: an unreadable mode would
    # otherwise surface as a checkpoint that never happens.
    _blocker_mode(ctx)

    # (agent identity | None, profile home, canonical DB) -> entry. The agent
    # slot is id(agent) validated against the entry's weakref, so a recycled id
    # can never alias a dead agent's runtime. Compaction keeps the object
    # (ownership survives); new agents, delegated children and restarts get
    # fresh runtimes and never inherit tokens. The lock guards the dict ONLY:
    # runtime.close() (subprocess calls, thread joins) always runs outside it.
    entries: dict[tuple, _Entry] = {}
    lock = threading.Lock()

    def _close_all(runtimes) -> None:
        for runtime in runtimes:
            try:
                runtime.close()
            except Exception as exc:
                logger.warning("covey-tasks: runtime close failed (%s)", type(exc).__name__)

    def _selected_entry(key, agent):
        # Caller holds entries' lock. Switching databases changes cwd routing,
        # not claim ownership: keep the other runtime's lease and flock alive.
        for other_key, other in entries.items():
            if other_key != key and other.ref is not None and other.ref() is agent:
                # ponytail: DB changes must be quiescent for this agent.
                # This restores sequential routing without releasing claims;
                # concurrent hot-switching needs invocation-scoped native cwd.
                other.runtime._restore_session_cwd()
        return entries.get(key)

    def _runtime_for(agent, profile: str, binary: str, db_path: str, canonical_db: str):
        """Runtime for `agent` (None => read-only diagnostic runtime)."""
        ref = None
        if agent is not None:
            try:
                ref = weakref.ref(agent)
            except TypeError:
                agent = None  # cannot track identity: fail closed, never guess
        key = (id(agent) if agent is not None else None, profile, canonical_db)
        stale = []
        with lock:
            for k, entry in list(entries.items()):
                dead = entry.ref is not None and entry.ref() is None
                if dead or entry.runtime.closed:
                    entries.pop(k, None)
                    if not entry.runtime.closed:
                        stale.append(entry.runtime)
            entry = _selected_entry(key, agent)
            if entry is not None and (agent is None or entry.ref() is agent):
                runtime = entry.runtime
            else:
                if entry is not None:
                    stale.append(entry.runtime)
                # New action on a reset/retired agent or unseen object: fresh
                # runtime, fresh per-role principals (Covey uniqueness is on
                # the principal, so a retired token is never reused).
                runtime = CoveyRuntime(
                    agent, binary=binary, db_path=db_path, profile_home=profile,
                    lease_duration_ms=LEASE_DURATION_MS,
                    renew_interval_ms=RENEW_INTERVAL_MS,
                    heartbeat_interval_ms=HEARTBEAT_INTERVAL_MS)
                entries[key] = _Entry(runtime, ref)
        _close_all(stale)
        return runtime

    def _close_matching(session_ids) -> None:
        """Close this profile's runtimes whose agent's LIVE session id matches.

        A last-seen session id is consulted only once the agent is gone; a
        live agent that rotated its session_id (compaction) is judged by its
        current id, never by a stale one.
        """
        targets = {s for s in session_ids if isinstance(s, str) and s}
        profile = _profile_home()
        if not targets or not profile:
            return
        victims = []
        with lock:
            for k, entry in list(entries.items()):
                if entry.ref is None or k[1] != profile:
                    continue
                live = entry.ref()
                sid = getattr(live, "session_id", None) if live is not None else entry.runtime.last_session_id
                if sid in targets:
                    entries.pop(k, None)
                    victims.append(entry.runtime)
        _close_all(victims)

    def covey_tasks(args, **_kw) -> str:
        try:
            binary, db_path, canonical_db = _read_settings(ctx)
        except ValueError as exc:
            return _failure("invalid_config", str(exc), _CONFIG_HELP)
        if not isinstance(args, dict):
            return _failure("invalid_arguments", "covey_tasks arguments must be an object.")
        profile = _profile_home()
        if not profile:
            return _failure("profile_unresolved", "The active Hermes profile could not be resolved.")
        runtime = _runtime_for(_bound_agent(), profile, binary, db_path, canonical_db)
        try:
            return json.dumps(runtime.execute(args))
        except Exception as exc:
            return _failure("internal_error", f"covey-tasks failed unexpectedly ({type(exc).__name__}).")

    ctx.register_tool(
        name="covey_tasks", toolset="covey-tasks", schema=TOOL_SCHEMA,
        handler=covey_tasks,
        description="Durable Covey worklists: create/add/start/finish with fenced ownership.",
    )


    # Cooperative workspace lease guard (NOT a security sandbox — a shell can
    # deliberately escape): live claim routes file/terminal cwd to the bound
    # worktree and refuses explicitly path-bearing mutations outside it;
    # after definite claim loss the guard stays open (file AND terminal
    # mutations refused, read-only process polling exempt) until a fresh
    # valid start.  Unbound workspaces (legacy coordination-only starts)
    # never engage the guard.
    def workspace_guard(tool_name="", args=None, **_kw):
        # Same ownership rules as covey_tasks: the guard engages ONLY the
        # runtime owned by the CURRENT live agent for the CURRENT profile
        # (the profile selects the canonical database).  A stale runtime for
        # a different database is never selected, one expired bound task can
        # never block an unrelated agent/profile, and no runtime is created
        # for this read-only path.
        agent = _bound_agent()
        if agent is None:
            return None
        profile = _profile_home()
        try:
            _, _, canonical_db = _read_settings(ctx)
        except ValueError:
            return None  # no current runtime identity; never inherit another DB's guard
        with lock:
            entry = _selected_entry((id(agent), profile, canonical_db), agent)
            runtime = entry.runtime if entry is not None and entry.ref() is agent else None
        if runtime is None or not getattr(runtime, "profile_home", profile):
            return None
        directive = runtime.workspace_guard(
            str(tool_name), args if isinstance(args, dict) else {})
        if directive is None:
            return None
        return {"action": "block",
                "message": str(directive.get("message") or "covey workspace guard")}

    ctx.register_hook("pre_tool_call", workspace_guard)

    # subagent_stop fires with the CHILD's live session_id
    # (delegate_tool_results.py:365-371); finalize/reset with the session's
    # (reset may also carry the previous id).
    def on_subagent_stop(child_session_id=None, **_kw):
        _close_matching([child_session_id])

    def on_session_end(session_id=None, old_session_id=None, **_kw):
        _close_matching([session_id, old_session_id])

    ctx.register_hook("subagent_stop", on_subagent_stop)
    ctx.register_hook("on_session_finalize", on_session_end)
    ctx.register_hook("on_session_reset", on_session_end)

    # A loop-guard block is the model saying, with evidence, that this exact
    # approach is not working.  `blocker_checkpoint` decides when that evidence
    # is strong enough to hand the task back: a checkpoint persists a resumable
    # marked handoff and RELEASES the claim (native retry semantics), so the
    # default waits until the guard's own redirect budget is spent rather than
    # shedding ownership over a single bad guess.  Nothing here activates
    # without that explicit setting, and nothing here raises into the dispatch.
    def observe_loop_blocker(*, action=None, delivered=None, selected_directive_id=None,
                             directives=None, session_id="", **_kw) -> None:
        try:
            if not delivered or action != "block":
                return  # suppressed or non-block directives never count
            selected = _selected_guard_directive(
                runtime_module.BLOCKER_PREFIX, selected_directive_id, directives)
            if selected is None:
                return  # another plugin's directive, or a block that was not selected
            directive_id, message = selected
            # Parsed exactly once, at the boundary; a malformed report is counted
            # by the runtime and never turned into an invented handoff.
            blocker = runtime_module.parse_loop_blocker(message)
            mode = _blocker_mode(ctx)
            agent = _bound_agent()
            if agent is None:
                return
            profile = _profile_home()
            _, _, canonical_db = _read_settings(ctx)
            with lock:
                entry = _selected_entry((id(agent), profile, canonical_db), agent)
                runtime = entry.runtime if entry is not None and entry.ref() is agent else None
            if runtime is None or runtime.profile_home != profile:
                return  # no runtime for this agent+profile: nothing here is claimed
            runtime.observe_blocker(
                session_id=str(session_id or "") or runtime.last_session_id,
                directive_id=directive_id, blocker=blocker, mode=mode,
                cap=_loop_guard_redirect_cap())
        except Exception as exc:  # boundary: an observer never fails the turn
            logger.warning(
                "covey-tasks: loop-guard block could not be observed (%s: %s)",
                type(exc).__name__, exc, exc_info=True)

    ctx.register_hook("post_tool_directive", observe_loop_blocker)

    def _unload():
        with lock:
            victims = [entry.runtime for entry in entries.values()]
            entries.clear()
        _close_all(victims)

    ctx.on_unload(_unload)
    logger.info("covey-tasks: active")
