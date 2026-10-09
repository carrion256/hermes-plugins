"""Task prewalk — port of dsh-prewalk: strong model plans, cheap model executes.

One-way, one-shot per logical session: when prewalk arms, the planner sees the
plan nudge in the SAME TURN — the prewalk_arm tool result carries it — and
delegated children get it on their first pre_llm_call. At the first confirmed
successful edit/write mutation past the todo gate (real agent._todo_store
checked, not tool args), the session switches to the target model using
Hermes' NATIVE live switch (agent_runtime_helpers.switch_model on the
currently-bound agent, invoked from post_tool_call after the mutation is
confirmed and before the next request). Provider kwargs are never rewritten —
raw model-string rewriting after provider kwargs are built is unsafe;
switch_model rebuilds clients/caching/compressor/reasoning_config with
rollback on failure. The target model must be served by the same endpoint
(provider/base_url/api_mode/api_key are kept as-is). Zero extra LLM calls.

Session identity follows the native active-agent binding: the turn facade
binds ``bind_subagent_parent(self)`` around the whole conversation
(agent/turn_facade.py:128), tool-worker submissions propagate the parent
thread's contextvars via tools.thread_context.propagate_context_to_thread
(agent/tool_executor.py:857 timeout worker, :1245 concurrent batch), and the
bounded hook runner executes callbacks inside ``copy_context().run``
(hermes_cli/plugins_dispatch.py:228,240). The accessor therefore resolves on
every legitimate dispatch path; the plugin never maintains a guessed agent
map.
"""

from __future__ import annotations

import json
import weakref
import logging

logger = logging.getLogger(__name__)

MUTATING_TOOLS = {"patch", "write_file"}
TODO_TOOL = "todo_list"
NUDGE = (
    "PREWALK: plan first. Lay out the full todo list and concrete steps before any "
    "mutation; the execution model will take over at your first edit/write."
)


def _bound_agent():
    """The agent whose live turn is bound in this thread's context, or None."""
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
        return get_active_subagent_parent()
    except Exception:
        return None


def register(ctx) -> None:
    target_model = (ctx.get_config("target_model", "") or "").strip()
    do_nudge = bool(ctx.get_config("plan_nudge", True))
    # OMP parity: delegated (bundled "task") agents prewalk by default.
    child_prewalk = bool(ctx.get_config("child_prewalk", True))
    todo_gate = bool(ctx.get_config("todo_gate", True))
    # ponytail: per-process in-memory state; if handoff state ever must
    # survive restarts, persist per-session rows in the session DB instead.
    armed: dict[str, str] = {}        # session_id -> target while planning
    handed_off: set[str] = set()      # completed the one-way handoff
    nudged: set[str] = set()          # plan nudge already injected (one-shot)
    planned: set[str] = set()         # todo gate passed (real todo store non-empty)

    # Live agent object -> session id last seen for it. Compaction rotation
    # (agent/conversation_compression.py) reassigns agent.session_id on the
    # SAME object; delegation children arrive with a fresh object whose
    # _parent_session_id is DELEGATION lineage (agent_init.py:1142), NOT a
    # rotation origin — migrating on it would steal the parent's armed
    # state into the child. Track each bound agent's own identity instead.
    _agent_session = weakref.WeakKeyDictionary()

    def _migrate(session_id: str) -> None:
        agent = _bound_agent()
        if agent is None or getattr(agent, "session_id", None) != session_id:
            return
        try:
            origin = _agent_session.get(agent, session_id)
            _agent_session[agent] = session_id
        except TypeError:  # unhashable/unweakrefable agent: no tracking
            return
        if origin == session_id:
            return
        if origin in armed:
            armed[session_id] = armed.pop(origin)
        for s in (handed_off, nudged, planned):
            if origin in s:
                s.discard(origin)
                s.add(session_id)
        logger.info("prewalk: session %s -> %s (agent rotation); state carried forward",
                    origin, session_id)

    def _clear(session_id: str) -> None:
        armed.pop(session_id, None)
        handed_off.discard(session_id)
        nudged.discard(session_id)
        planned.discard(session_id)

    def prewalk_arm(session_id: str = "", **_kw) -> str:
        if not target_model:
            return json.dumps({"success": False, "error": "prewalk target_model not configured"})
        if session_id in handed_off or session_id in armed:
            return json.dumps({"success": False, "error": "prewalk already armed/handed off for this session"})
        armed[session_id] = target_model
        logger.info("prewalk: armed session %s -> %s", session_id, target_model)
        # The tool result carries the nudge so the planner sees it THIS turn,
        # before the next pre_llm_call (which would only fire next turn).
        return json.dumps({"success": True, "armed": True, "target": target_model,
                           "plan_nudge": NUDGE})

    ctx.register_tool(
        name="prewalk_arm", toolset="prewalk",
        schema={"name": "prewalk_arm",
                "description": "Arm prewalk for this session: planning model steers, target model takes over at the first planned, successful edit/write.",
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: prewalk_arm(**kw),
        description="Arm the plan/exec model handoff for this session.",
    )

    def prewalk_status(session_id: str = "", **_kw) -> str:
        if session_id in handed_off:
            state = "handed_off"
        elif session_id in armed:
            state = "armed"
        else:
            state = "inactive"
        return json.dumps({"success": True, "session_id": session_id, "state": state,
                           "target": target_model})

    ctx.register_tool(
        name="prewalk_status", toolset="prewalk",
        schema={"name": "prewalk_status",
                "description": "Report prewalk state (armed/handed_off/inactive) for this session.",
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: prewalk_status(**kw),
        description="Report the plan/exec handoff state for this session.",
    )

    def prewalk_reset(session_id: str = "", **_kw) -> str:
        was = session_id in armed or session_id in handed_off
        _clear(session_id)
        logger.info("prewalk: session %s %s", session_id, "reset" if was else "cleaned")
        return json.dumps({"success": True, "reset": was})

    ctx.register_tool(
        name="prewalk_reset", toolset="prewalk",
        schema={"name": "prewalk_reset",
                "description": "Explicitly reset prewalk state for this session (the only way back; the handoff itself is one-way).",
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: prewalk_reset(**kw),
        description="Reset the plan/exec handoff state for this session.",
    )

    # Logical-session cleanup: /new (reset) and finalization drop state.
    # Deliberately NO on_session_end cleanup — it fires on conversation
    # (per-turn) boundaries and would silently undo the one-way guarantee.
    def _cleanup(session_id: str = "", **_kw):
        if session_id:
            _clear(session_id)

    ctx.register_hook("on_session_reset", _cleanup)
    ctx.register_hook("on_session_finalize", _cleanup)

    # Child lane: every delegated child inherits the armed planning lane
    # (config child_prewalk). The child's goal rides its first user message;
    # the plan nudge fires once on that same first turn.
    def on_subagent_start(child_session_id: str = "", **_kw):
        if child_prewalk and target_model and child_session_id and child_session_id not in handed_off:
            armed.setdefault(child_session_id, target_model)

    ctx.register_hook("subagent_start", on_subagent_start)

    if do_nudge:
        def nudge(session_id: str = "", **_kw):
            _migrate(session_id)
            if session_id in armed and session_id not in handed_off and session_id not in nudged:
                nudged.add(session_id)  # one-shot: never nudges again this session
                return {"context": NUDGE}
            return None

        ctx.register_hook("pre_llm_call", nudge)

    def _result_dict(result):
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except ValueError:
                return None
            if isinstance(parsed, dict):
                return parsed
        return None

    def _mutation_succeeded(tool_name: str, result, error_type, error_message, status) -> bool:
        # Real WriteResult/PatchResult fields: a failed call carries
        # error_type/error_message/status, an {"error": ...} payload, or —
        # patch — success=False. Malformed or absent results are NOT success.
        if error_type or error_message or (status and str(status).lower() not in {"ok", "success"}):
            return False
        parsed = _result_dict(result)
        if parsed is None:
            return False
        if tool_name == "patch":
            return parsed.get("success") is True
        # write_file: WriteResult — error is the failure flag; verified=False is
        # already a hard error upstream, verified=None means unverifiable.
        if parsed.get("error"):
            return False
        return int(parsed.get("bytes_written") or 0) > 0

    def _switch(session_id: str, tool_name: str) -> None:
        agent = _bound_agent()
        if agent is None or getattr(agent, "session_id", None) != session_id:
            # No live bound agent for THIS session: refuse, stay armed, and let
            # the next confirmed mutation retry. Never rewrite provider kwargs.
            logger.warning("prewalk: session %s has no live bound agent; staying armed", session_id)
            return
        if getattr(agent, "_persist_disabled", False):
            logger.warning("prewalk: session %s agent is detached (_persist_disabled); staying armed",
                           session_id)
            return
        try:
            from agent.agent_runtime_helpers import switch_model
            switch_model(
                agent, target_model, getattr(agent, "provider", "") or "",
                api_key=getattr(agent, "api_key", "") or "",
                base_url=getattr(agent, "base_url", "") or "",
                api_mode=getattr(agent, "api_mode", "") or "",
            )
        except Exception:
            logger.warning("prewalk: live switch to %s failed for session %s; staying armed",
                           target_model, session_id, exc_info=True)
            return
        handed_off.add(session_id)
        armed.pop(session_id, None)
        logger.info("prewalk: handed off session %s -> %s at first planned, successful %s",
                    session_id, target_model, tool_name)

    def watch_mutation(tool_name: str = "", session_id: str = "", result=None,
                       error_type=None, error_message=None, status=None, **_kw):
        _migrate(session_id)
        if session_id not in armed or session_id in handed_off:
            return
        if tool_name == TODO_TOOL:
            # Observational todo gate on the REAL native todo store: only a
            # confirmed-successful todo_list call whose store is non-empty
            # passes the gate (args of a failed call are untrusted).
            if _mutation_succeeded_todos(result, error_type, error_message, status):
                agent = _bound_agent()
                store = getattr(agent, "_todo_store", None) if agent is not None else None
                has_items = getattr(store, "has_items", None)
                if callable(has_items) and has_items():
                    planned.add(session_id)
            return
        if tool_name not in MUTATING_TOOLS:
            return  # e.g. terminal/bash NEVER switches (OMP parity E1)
        if not _mutation_succeeded(tool_name, result, error_type, error_message, status):
            return
        if todo_gate and session_id not in planned:
            logger.info("prewalk: session %s mutated before any todo plan; not switching yet", session_id)
            return
        _switch(session_id, tool_name)

    def _mutation_succeeded_todos(result, error_type, error_message, status) -> bool:
        if error_type or error_message or (status and str(status).lower() not in {"ok", "success"}):
            return False
        parsed = _result_dict(result)
        return parsed is None or not parsed.get("error")

    ctx.register_hook("post_tool_call", watch_mutation)
    logger.info("prewalk: active (target=%s)", target_model or "unset — arm refused until configured")
