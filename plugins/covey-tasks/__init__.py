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

# Maintenance timing. A long task can legitimately outlive the 300 s claim
# lease, so maintenance ticks every 60 s and renews with headroom-restoring
# extensions. These are process-level constants, not per-profile config:
# operators cannot mis-tune them into ownership loss.
LEASE_DURATION_MS = 300000
RENEW_INTERVAL_MS = 60000
HEARTBEAT_INTERVAL_MS = 60000

DEFAULT_BINARY = "covey"

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
        "show(subtask_id) for authoritative state; create(prompt) for a new "
        "finite worklist; add(meta_task_id, title, priority) for a work task; "
        "start(subtask_id) to claim+start the exact task; finish/retry/fail "
        "(subtask_id, summary[, evidence_digest]); release(subtask_id); "
        "abandon(subtask_id). Plan and add every known task BEFORE starting "
        "execution; once a worklist is completed it stays history — create a "
        "new one. Any direct work task can be started; the task's persisted "
        "evidence policy is still enforced (a task that requires evidence "
        "needs evidence_digest to finish), and governed tasks stay outside "
        "this tool. Completion here is not review or landing verification."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "lists", "show", "create", "add", "start",
                         "finish", "retry", "fail", "release", "abandon"],
            },
            "meta_task_id": {"type": "string"},
            "subtask_id": {"type": "string"},
            "prompt": {"type": "string"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "failure_code": {"type": "string"},
            "evidence_digest": {"type": "string"},
            "request_id": {"type": "string"},
            "priority": {"type": "integer"},
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


class _Entry:
    __slots__ = ("runtime", "ref")

    def __init__(self, runtime, ref):
        self.runtime = runtime
        self.ref = ref  # weakref to the live agent; None for diagnostic runtimes


def register(ctx) -> None:
    CoveyRuntime = _load_runtime_module().CoveyRuntime

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
            entry = entries.get(key)
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

    def _unload():
        with lock:
            victims = [entry.runtime for entry in entries.values()]
            entries.clear()
        _close_all(victims)

    ctx.on_unload(_unload)
    logger.info("covey-tasks: active")
