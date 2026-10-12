"""Task prewalk — port of dsh-prewalk: strong model plans, cheap model executes.

One-way per logical session, driven by prewalk_ready for the top-level
lane and automatically for the child lane: when prewalk arms, the planner
sees the plan nudge in the SAME TURN — the prewalk_arm tool result carries
it — and delegated children get it on their first pre_llm_call. The session
switches to the target model when (and only when) prewalk_ready validates
the complete handoff: located files with workspace paths that actually
exist, symbols/callers, constraints, non-goals, chosen approach, rejected
alternatives, falsification checks, replan triggers and acceptance IDs,
on top of a genuine native todo plan (real agent._todo_store checked, not
tool args). The first successful edit/write NEVER switches. The child lane
(delegated children armed via subagent_start) hands off AUTOMATICALLY on
the child's first completed post_tool_call — mid-turn, after the child's
first model response requested work and before the next model call —
bypassing prewalk_ready and the todo gate; the lane is consumed exactly
once per child, a failed switch is logged with the receipt reason at
warning level and never retried, and the child simply continues on its
current model. The switch runs on the
currently-bound agent, invoked from prewalk_ready itself. Same-endpoint
targets (no target_provider, or one equal to the agent's provider) keep
Hermes' NATIVE agent_runtime_helpers.switch_model path (provider/base_url/
api_mode/api_key kept as-is; switch_model rebuilds clients/caching/
compressor/reasoning_config with rollback on failure). Cross-provider
targets — target_provider naming a different provider, or a routed
executor role resolved through the host PluginContext.resolve_role_model
seam — are validated at ARM time via PluginContext.switch_agent_model
(dry run) and handed off through that seam, which owns credential
reload, destination capabilities, and rollback; provider kwargs are
never rewritten on the native path. An operator-enabled, bounded
replan (default off) can return the session to the ORIGINAL model AND
provider once, only with a concrete disconfirmed assumption/check —
never automatic oscillation. Zero extra LLM calls.

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
import os
import weakref
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
NUDGE = (
    "PREWALK: plan first. Lay out the full todo list and concrete steps before "
    "any mutation; then call prewalk_ready with the complete handoff (located "
    "files, symbols/callers, constraints, non-goals, chosen approach and "
    "rejected alternatives, falsification checks, replan triggers, acceptance "
    "IDs) to switch this session to the execution model. Edits/writes alone "
    "never switch the model."
)


_HANDOFF_LISTS = ("files", "constraints", "non_goals", "rejected_approaches")
_OPTIONAL_LISTS = ("falsification_checks", "replan_triggers", "acceptance_ids")


def _validate_handoff(args: dict, session_id: str = "",
                      task_id: str = "") -> tuple[bool, list]:
    """Structural + path validation for prewalk_ready. Prose fields are
    validated for presence only — never executed or semantically judged;
    file paths are evidence and must resolve to REGULAR FILES inside the
    workspace (resolved-path confinement: symlink escape rejected, and
    directories never count as located evidence)."""
    problems = []
    for field in _HANDOFF_LISTS:
        v = args.get(field)
        if not isinstance(v, list) or not v or any(
                not isinstance(x, str) or not x.strip() for x in v):
            problems.append(f"{field}: non-empty list of non-empty strings required")
    for field in _OPTIONAL_LISTS:
        v = args.get(field)
        if v is None:
            v = args[field] = []  # optional: absent defaults to empty
        if not isinstance(v, list) or any(
                not isinstance(x, str) or not x.strip() for x in v):
            problems.append(f"{field}: optional list of non-empty strings")
    symbols = args.get("symbols")
    if not isinstance(symbols, list) or not symbols or any(
            not isinstance(x, str) or not x.strip() for x in symbols):
        problems.append("symbols: non-empty list of non-empty strings required")
    callers = args.get("callers")
    if callers is None:
        callers = []
    if not isinstance(callers, list) or any(
            not isinstance(x, str) or not x.strip() for x in callers):
        problems.append("callers: list of non-empty strings required")
    # callers may legitimately be empty with no reason given — advisory only.
    if args.get("rule_refs") is not None and (
            not isinstance(args["rule_refs"], list)
            or any(not isinstance(x, str) for x in args["rule_refs"])):
        problems.append("rule_refs: list of strings required")
    chosen = args.get("chosen_approach")
    if not isinstance(chosen, str) or not chosen.strip():
        problems.append("chosen_approach: non-empty string required")
    if problems:
        return False, problems
    workspace = None
    # Native file/terminal execution resolves against the TASK cwd registry
    # (terminal_tool, keyed by the effective task id): after a workspace
    # override or a persistent `cd`, only that registry names the tree the
    # check actually runs in. The configured/launch cwd is a FALLBACK only.
    try:
        from tools.terminal_tool import get_session_cwd
        for identity in (str(task_id or ""), str(session_id or "")):
            if not identity:
                continue
            try:
                recorded = get_session_cwd(identity)
            except Exception:
                recorded = None
            if recorded:
                workspace = recorded
                break
    except Exception:
        workspace = None
    if workspace is None:
        try:
            from agent.runtime_cwd import resolve_agent_cwd, resolve_context_cwd
        except Exception:
            resolve_context_cwd = resolve_agent_cwd = None
        if resolve_context_cwd is not None:
            try:
                workspace = resolve_context_cwd()
            except Exception:
                workspace = None
        if workspace is None and resolve_agent_cwd is not None:
            # Legitimate launch cwd (plain CLI started in the project): the
            # agent's own workspace is real workspace evidence too.
            try:
                workspace = resolve_agent_cwd()
            except Exception:
                workspace = None
    if workspace is None:
        return False, ["workspace_unavailable: cannot validate file references"]
    try:
        root = Path(str(workspace)).resolve()
        for raw in args.get("files") or []:
            raw = str(raw).strip()
            if os.path.isabs(raw) or raw.startswith("~") or ".." in Path(raw).parts:
                return False, [f"files: workspace-relative paths only, "
                               f"no absolute or .. entries: {raw}"]
            full = (root / raw).resolve()
            if not full.is_relative_to(root):
                return False, [f"files: {raw} resolves outside the workspace "
                               f"(symlink escape)"]
            if not full.is_file():
                return False, [f"files: {raw} is not an existing regular file "
                               f"in the workspace"]
    except OSError:
        return False, ["workspace path cannot be resolved"]
    return True, []


def _compact_handoff(args: dict) -> dict:
    """Bounded single-record copy of the handoff for inspection/status."""
    def _s(v):
        return str(v).strip()[:500]
    def _l(k):
        return [_s(x) for x in (args.get(k) or []) if isinstance(x, str)][:32]
    return {"files": _l("files"), "symbols": _l("symbols"), "callers": _l("callers"),
            "callers_absence_reason": _s(args.get("callers_absence_reason")),
            "constraints": _l("constraints"), "non_goals": _l("non_goals"),
            "chosen_approach": _s(args.get("chosen_approach")),
            "rejected_approaches": _l("rejected_approaches"),
            "falsification_checks": _l("falsification_checks"),
            "replan_triggers": _l("replan_triggers"),
            "acceptance_ids": _l("acceptance_ids"), "rule_refs": _l("rule_refs")}


def _bound_agent():
    """The agent whose live turn is bound in this thread's context, or None."""
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
        return get_active_subagent_parent()
    except Exception:
        return None


def register(ctx) -> None:
    target_model = (ctx.get_config("target_model", "") or "").strip()
    target_provider = (ctx.get_config("target_provider", "") or "").strip()
    do_nudge = bool(ctx.get_config("plan_nudge", True))
    # OMP parity: delegated (bundled "task") agents prewalk by default.
    child_prewalk = bool(ctx.get_config("child_prewalk", True))
    # OMP parity: the child lane only arms for children that requested one of these
    # routing roles — or requested none at all (a plain delegation, which OMP prewalks
    # too). Any other role (scout, architect, reviewer, ...) arms nothing.
    raw_roles = ctx.get_config("child_prewalk_roles", ["task"])
    child_prewalk_roles = (
        {str(r).strip() for r in raw_roles if str(r).strip()}
        if isinstance(raw_roles, (list, tuple)) else {"task"}
    )
    todo_gate = bool(ctx.get_config("todo_gate", True))
    # Operator opt-in only: bounded one-shot replan back to the ORIGINAL
    # model AND provider. Default stays OFF; the handoff itself never oscillates.
    replan_enabled = bool(ctx.get_config("replan_enabled", False))
    # ponytail: per-process in-memory state; if handoff state ever must
    # survive restarts, persist per-session rows in the session DB instead.
    armed: dict[str, dict] = {}      # session_id -> resolved target while planning
    handed_off: set[str] = set()      # completed the one-way handoff
    nudged: set[str] = set()          # plan nudge already injected (one-shot)
    ready_handoffs: dict[str, dict] = {}   # session_id -> validated handoff
    original_models: dict[str, str] = {}   # session_id -> planner model at handoff
    original_providers: dict[str, str] = {}  # session_id -> planner provider at handoff
    session_targets: dict[str, dict] = {}    # session_id -> target used at handoff (status)
    replan_used: set[str] = set()          # bounded: one replan per session, ever
    child_lane: set[str] = set()      # sessions armed via the child (subagent_start) lane

    # Live agent object -> session id last seen for it. Compaction rotation
    # (agent/conversation_compression.py) reassigns agent.session_id on the
    # SAME object; delegation children arrive with a fresh object whose
    # _parent_session_id is DELEGATION lineage (agent_init.py:1142), NOT a
    # rotation origin — migrating on it would steal the parent's armed
    # state into the child. Track each bound agent's own identity instead.
    _agent_session = weakref.WeakKeyDictionary()

    def _resolve_target(agent) -> dict | None:
        """Handoff target precedence: explicit target_model(+target_provider)
        > route 'executor' receipt > none. Returns {"model","provider",
        "source"} or None; never raises."""
        if target_model:
            return {"model": target_model, "provider": target_provider,
                    "source": "explicit"}
        resolver = getattr(ctx, "resolve_role_model", None)
        if callable(resolver):
            try:
                receipt = resolver("executor", agent=agent)
            except Exception as exc:  # host contract: never raises; stay defensive
                logger.warning("prewalk: executor route lookup failed (%s)", exc)
                receipt = None
            if isinstance(receipt, dict) and receipt.get("source") == "route":
                model = str(receipt.get("model") or "").strip()
                provider = str(receipt.get("provider") or "").strip()
                if model:
                    return {"model": model, "provider": provider, "source": "route"}
        return None

    def _agent_provider(agent) -> str:
        return str(getattr(agent, "provider", "") or "") if agent is not None else ""

    def _cross_provider_problem(target: dict, agent) -> str | None:
        """ARM-time validation for cross-provider targets: the destination
        must be resolvable through the host switch_agent_model seam (dry
        run). Returns an actionable refusal reason, or None to arm."""
        provider = target.get("provider", "")
        if not provider or provider == _agent_provider(agent):
            return None
        seam = getattr(ctx, "switch_agent_model", None)
        if not callable(seam):
            return (f"cross-provider target {target['model']} on provider "
                    f"'{provider}' requires the host switch_agent_model seam; "
                    f"this host does not provide it — set target_provider to "
                    f"the current provider or upgrade the host")
        try:
            receipt = seam(target["model"], provider,
                           reason="prewalk arm validation", dry_run=True, agent=agent)
        except Exception as exc:  # defensive: the seam must not raise
            receipt = {"resolvable": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not isinstance(receipt, dict) or receipt.get("resolvable") is not True:
            detail = (str(receipt.get("reason") or "malformed receipt")
                      if isinstance(receipt, dict) else "malformed receipt")
            return f"prewalk target provider '{provider}' is not resolvable: {detail}"
        return None

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
        for s in (handed_off, nudged, replan_used, child_lane):
            if origin in s:
                s.discard(origin)
                s.add(session_id)
        for d in (ready_handoffs, original_models, original_providers, session_targets):
            if origin in d:
                d[session_id] = d.pop(origin)
        logger.info("prewalk: session %s -> %s (agent rotation); state carried forward",
                    origin, session_id)

    def _clear(session_id: str) -> None:
        armed.pop(session_id, None)
        handed_off.discard(session_id)
        nudged.discard(session_id)
        ready_handoffs.pop(session_id, None)
        original_models.pop(session_id, None)
        original_providers.pop(session_id, None)
        session_targets.pop(session_id, None)
        replan_used.discard(session_id)
        child_lane.discard(session_id)

    def prewalk_arm(session_id: str = "", **_kw) -> str:
        _migrate(session_id)
        agent = _bound_agent()
        target = _resolve_target(agent)
        if target is None:
            return json.dumps({"success": False, "error": "prewalk target_model not configured"})
        if session_id in handed_off or session_id in armed:
            return json.dumps({"success": False, "error": "prewalk already armed/handed off for this session"})
        if session_id in replan_used:
            # Bounded: the one permitted replan was already spent; no
            # arm->ready->replan->arm oscillation.
            return json.dumps({"success": False, "error": "prewalk replan budget spent for this session"})
        problem = _cross_provider_problem(target, agent)
        if problem:
            logger.warning("prewalk: refusing to arm session %s: %s", session_id, problem)
            return json.dumps({"success": False, "error": problem})
        armed[session_id] = target
        logger.info("prewalk: armed session %s -> %s (%s)",
                    session_id, target["model"], target["provider"] or "same provider")
        # The tool result carries the nudge so the planner sees it THIS turn,
        # before the next pre_llm_call (which would only fire next turn).
        return json.dumps({"success": True, "armed": True,
                           "target": target["model"],
                           "target_provider": target["provider"],
                           "target_source": target["source"],
                           "plan_nudge": NUDGE})

    ctx.register_tool(
        name="prewalk_arm", toolset="prewalk",
        schema={"name": "prewalk_arm",
                "description": "Arm prewalk for this session: planning model steers; the handoff happens ONLY on a validated prewalk_ready, never on the first edit/write.",
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: prewalk_arm(**kw),
        description="Arm the plan/exec model handoff for this session.",
    )

    def prewalk_status(session_id: str = "", **_kw) -> str:
        _migrate(session_id)
        if session_id in handed_off:
            state = "handed_off"
        elif session_id in armed:
            state = "armed"
        else:
            state = "inactive"
        target = (armed.get(session_id) or session_targets.get(session_id)
                  or _resolve_target(_bound_agent()))
        return json.dumps({"success": True, "session_id": session_id, "state": state,
                           "target": target["model"] if target else target_model,
                           "target_model": target["model"] if target else target_model,
                           "target_provider": target["provider"] if target else "",
                           "target_source": target["source"] if target else "",
                           "handoff": bool(ready_handoffs.get(session_id)),
                           "original_model": original_models.get(session_id),
                           "original_provider": original_providers.get(session_id),
                           "replan_available": (bool(replan_enabled)
                                                 and session_id in handed_off
                                                 and session_id not in replan_used),
                           "replan_used": session_id in replan_used})

    ctx.register_tool(
        name="prewalk_status", toolset="prewalk",
        schema={"name": "prewalk_status",
                "description": "Report prewalk state (armed/handed_off/inactive), validated handoff and replan availability for this session.",
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: prewalk_status(**kw),
        description="Report the plan/exec handoff state for this session.",
    )

    def prewalk_reset(session_id: str = "", **_kw) -> str:
        _migrate(session_id)
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
    def on_subagent_start(child_session_id: str = "", child_model_role=None, **_kw):
        # OMP parity: only children that requested the "task" lane prewalk. The host
        # records the child's effective routing role in child_model_role; None means no
        # role was requested at all — a plain delegation, which OMP prewalks too. Any
        # other role (scout, reviewer, ...) keeps its own lane and arms nothing.
        role = str(child_model_role or "").strip() or None
        if (child_prewalk and child_session_id
                and (role is None or role in child_prewalk_roles)
                and child_session_id not in handed_off
                and child_session_id not in armed):
            agent = _bound_agent()
            target = _resolve_target(agent)
            if not target:
                return
            # Same ARM-time gate as prewalk_arm: cross-provider targets are
            # validated (seam present + dry-run resolvable); a refused
            # child lane simply stays un-armed, loudly.
            problem = _cross_provider_problem(target, agent)
            if problem:
                logger.warning("prewalk: refusing to arm child %s: %s",
                               child_session_id, problem)
                return
            armed.setdefault(child_session_id, target)
            child_lane.add(child_session_id)

    ctx.register_hook("subagent_start", on_subagent_start)

    def child_handoff(session_id: str = "", **_kw):
        """OMP parity: the child lane hands off AUTOMATICALLY on the child's first
        completed post_tool_call — no prewalk_ready, no todo gate (the planning nudge
        may still fire on that same first call). post_tool_call fires once per tool
        result, so the switch lands mid-turn after the child's first model response
        requested work — before the next model call. A child whose first response
        ends the run never fires this hook; for it the handoff is moot.
        One-shot: the lane is consumed before the switch is attempted, so a failed
        switch is logged with the receipt reason and never retried; the child simply
        continues on its current model. Top-level sessions are untouched — they arm
        via prewalk_arm and switch only via a validated prewalk_ready."""
        if (not session_id or session_id not in child_lane
                or session_id not in armed or session_id in handed_off):
            return None
        _migrate(session_id)
        target = armed.get(session_id)
        # Consume the lane BEFORE attempting the switch: exactly once per child, ever.
        armed.pop(session_id, None)
        child_lane.discard(session_id)
        if target is None:
            return None
        agent = _bound_agent()
        dest = str(target["model"])
        dest_provider = str(target.get("provider") or "")
        current = str(getattr(agent, "model", "") or "") if agent is not None else ""
        provider_ok = (not dest_provider
                       or dest_provider == _agent_provider(agent))
        if agent is not None and current == dest and provider_ok:
            handed_off.add(session_id)
            session_targets[session_id] = dict(target)
            logger.info("prewalk: child %s already runs on target %s; no switch needed",
                        session_id, dest)
            return None
        switched, detail = _switch(session_id, "prewalk child auto handoff",
                                   model=dest, provider=dest_provider)
        if switched:
            session_targets[session_id] = dict(target)
        else:
            logger.warning("prewalk: child auto handoff failed for session %s: %s "
                           "(child continues on its current model; never retried)",
                           session_id, detail)
        return None

    ctx.register_hook("post_tool_call", child_handoff)

    if do_nudge:
        def nudge(session_id: str = "", **_kw):
            _migrate(session_id)
            if session_id in armed and session_id not in handed_off and session_id not in nudged:
                nudged.add(session_id)  # one-shot: never nudges again this session
                return {"context": NUDGE}
            return None

        ctx.register_hook("pre_llm_call", nudge)
    def _switch(session_id: str, reason: str, model: str = "",
                provider: str = "") -> tuple[bool, str]:
        target = model or target_model
        dest_provider = provider if model else target_provider
        agent = _bound_agent()
        if agent is None or getattr(agent, "session_id", None) != session_id:
            logger.warning("prewalk: session %s has no live bound agent; staying armed", session_id)
            return False, "no live bound agent for this session"
        if getattr(agent, "_persist_disabled", False):
            logger.warning("prewalk: session %s agent is detached (_persist_disabled); staying armed",
                           session_id)
            return False, "agent is detached (_persist_disabled)"
        original_models.setdefault(session_id, str(getattr(agent, "model", "") or ""))
        original_providers.setdefault(session_id, str(getattr(agent, "provider", "") or ""))
        seam = getattr(ctx, "switch_agent_model", None)
        if callable(seam):
            try:
                receipt = seam(target, dest_provider, reason=reason, agent=agent)
            except Exception as exc:
                logger.warning("prewalk: switch_agent_model raised for session %s (%s)",
                               session_id, exc)
                return False, f"switch_agent_model raised: {type(exc).__name__}: {exc}"
            if not isinstance(receipt, dict):
                logger.warning("prewalk: malformed receipt from switch_agent_model "
                               "for session %s; staying armed", session_id)
                return False, "malformed receipt from switch_agent_model"
            if receipt.get("switched") is not True:
                detail = str(receipt.get("reason") or "switch_agent_model declined the switch")
                logger.warning("prewalk: switch_agent_model declined for session %s: %s",
                               session_id, detail)
                return False, detail
        else:
            # No host seam: native same-endpoint path only — provider kwargs
            # are never rewritten here.
            if dest_provider and dest_provider != str(getattr(agent, "provider", "") or ""):
                return False, (f"cross-provider switch to '{dest_provider}' requires the "
                               f"host switch_agent_model seam")
            try:
                from agent.agent_runtime_helpers import switch_model
                switch_model(
                    agent, target, getattr(agent, "provider", "") or "",
                    api_key=getattr(agent, "api_key", "") or "",
                    base_url=getattr(agent, "base_url", "") or "",
                    api_mode=getattr(agent, "api_mode", "") or "",
                )
            except Exception:
                logger.warning("prewalk: live switch to %s failed for session %s; staying armed",
                               target, session_id, exc_info=True)
                return False, f"native switch_model failed for session {session_id}"
        handed_off.add(session_id)
        armed.pop(session_id, None)
        logger.info("prewalk: handed off session %s -> %s via %s", session_id, target, reason)
        return True, ""

    def prewalk_ready(args=None, session_id: str = "", **_kw) -> str:
        """The ONLY handoff path: validated full handoff + genuine native
        plan, switched live (host seam when present, native same-endpoint
        switch_model otherwise)."""
        _migrate(session_id)
        if session_id not in armed or session_id in handed_off:
            return json.dumps({"success": False,
                               "error": "prewalk is not armed for this session"})
        args = args if isinstance(args, dict) else {}
        # validate located evidence against the EFFECTIVE task workspace,
        # not the launch tree (workspace overrides / persistent cd).
        ok, problems = _validate_handoff(args, session_id,
                                         str(_kw.get("task_id") or ""))
        if not ok:
            return json.dumps({"success": False, "error": "handoff incomplete or invalid",
                               "problems": problems})
        if todo_gate:
            agent = _bound_agent()
            store = getattr(agent, "_todo_store", None) if agent is not None else None
            has_items = getattr(store, "has_items", None)
            if (agent is None or str(getattr(agent, "session_id", "")) != session_id
                    or not (callable(has_items) and has_items())):
                return json.dumps({"success": False,
                                   "error": "genuine native todo plan required before ready "
                                            "(non-empty native todo store not observed for this session)"})
        handoff = _compact_handoff(args)
        target = armed.get(session_id) or {}
        switched, detail = _switch(session_id, "prewalk_ready",
                                   model=str(target.get("model") or target_model),
                                   provider=str(target.get("provider") or ""))
        if not switched:
            return json.dumps({"success": False,
                               "error": f"live switch unavailable or refused ({detail}); "
                                        f"session stays armed"})
        session_targets[session_id] = dict(target)
        ready_handoffs[session_id] = handoff
        return json.dumps({"success": True, "handed_off": True,
                           "target": target.get("model") or target_model,
                           "target_provider": target.get("provider", ""),
                           "target_source": target.get("source", ""),
                           "handoff": handoff})

    ctx.register_tool(
        name="prewalk_ready", toolset="prewalk",
        schema={"name": "prewalk_ready",
                "description": "Declare the planning handoff complete and switch to the execution model: requires a genuine native todo plan plus the full evidence-backed handoff (located files, symbols/callers, constraints, non-goals, chosen approach, rejected alternatives, falsification checks, replan triggers, acceptance IDs).",
                "parameters": {"type": "object", "properties": {
                    "files": {"type": "array", "items": {"type": "string"}, "description": "Workspace-relative paths of located evidence; each must exist."},
                    "symbols": {"type": "array", "items": {"type": "string"}, "description": "Located symbols central to the task; non-empty."},
                    "callers": {"type": "array", "items": {"type": "string"}, "description": "Callers of the located symbol; may be empty (optionally with callers_absence_reason)."},
                    "callers_absence_reason": {"type": "string", "description": "Optional: why callers legitimately do not exist."},
                    "constraints": {"type": "array", "items": {"type": "string"}, "description": "Constraints the execution model must respect."},
                    "non_goals": {"type": "array", "items": {"type": "string"}},
                    "chosen_approach": {"type": "string",
                                        "description": "The approach the execution model will run; non-empty."},
                    "rejected_approaches": {"type": "array", "items": {"type": "string"}},
                    "falsification_checks": {"type": "array", "items": {"type": "string"}},
                    "replan_triggers": {"type": "array", "items": {"type": "string"}},
                    "acceptance_ids": {"type": "array", "items": {"type": "string"}, "description": "Acceptance criterion IDs (native acceptance tool ids where enabled)."},
                    "rule_refs": {"type": "array", "items": {"type": "string"}, "description": "Optional rule references."}},
                    "required": ["files", "symbols", "constraints", "non_goals",
                                 "chosen_approach", "rejected_approaches"]}},
        handler=lambda args, **kw: prewalk_ready(args, **kw),
        description="Hand off to the execution model with a validated evidence-backed plan.",
    )

    def prewalk_replan(args=None, session_id: str = "", **_kw) -> str:
        """Bounded, operator-gated return to the ORIGINAL model AND
        provider with the concrete disconfirmed assumption/check. Never
        oscillates."""
        _migrate(session_id)
        args = args if isinstance(args, dict) else {}
        if not replan_enabled:
            return json.dumps({"success": False,
                               "error": "prewalk replan is operator-disabled (replan_enabled)"})
        if session_id in armed or session_id not in handed_off:
            return json.dumps({"success": False,
                               "error": "prewalk replan is only available after a completed handoff"})
        if session_id in replan_used:
            return json.dumps({"success": False,
                               "error": "prewalk replan budget already spent for this session"})
        original = original_models.get(session_id)
        if not original:
            return json.dumps({"success": False,
                               "error": "no original planner model recorded for this session"})
        original_provider = str(original_providers.get(session_id) or "")
        assumption = str(args.get("failed_assumption") or "").strip()
        failed_check = str(args.get("failed_check") or "").strip()
        if not assumption or not failed_check:
            return json.dumps({"success": False,
                               "error": "replan requires a concrete failed_assumption and failed_check"})
        switched, detail = _switch(session_id, "prewalk_replan",
                                   model=original, provider=original_provider)
        if not switched:
            return json.dumps({"success": False,
                               "error": f"live switch back to the original model failed "
                                        f"({detail})"})
        replan_used.add(session_id)
        return json.dumps({"success": True, "replanned": True,
                           "original_model": original,
                           "original_provider": original_provider,
                           "failed_assumption": assumption, "failed_check": failed_check})

    ctx.register_tool(
        name="prewalk_replan", toolset="prewalk",
        schema={"name": "prewalk_replan",
                "description": "Request the one bounded replan back to the original planning model (operator-enabled): must cite the concrete disconfirmed assumption and the check that disproved it.",
                "parameters": {"type": "object", "properties": {
                    "failed_assumption": {"type": "string", "description": "The concrete plan assumption the evidence disconfirmed."},
                    "failed_check": {"type": "string", "description": "The falsification check whose actual result disproved it."}},
                    "required": ["failed_assumption", "failed_check"]}},
        handler=lambda args, **kw: prewalk_replan(args, **kw),
        description="Bounded return to the original planner with disconfirming evidence.",
    )

    logger.info("prewalk: active (target=%s, replan=%s)",
                target_model or "unset — arm refused until configured",
                "bounded-once" if replan_enabled else "off")
