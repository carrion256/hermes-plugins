"""Owned, bounded loop recovery; a proposed correction is not a delivered one."""
from __future__ import annotations

from collections import deque
import hashlib
import json
import logging
import threading
import uuid
import weakref

_POLL_ACTIONS = {"list", "poll", "log", "wait"}
_HISTORY_LIMIT = 24
logger = logging.getLogger(__name__)


def _signature(name, args) -> str:
    return hashlib.sha256(json.dumps([name, args], sort_keys=True, ensure_ascii=False,
                                    default=str).encode()).hexdigest()


def _similar(a: str, b: str) -> bool:
    def shingles(text):
        words = text.lower().split()
        return {tuple(words[i:i + 4]) for i in range(max(0, len(words) - 3))}
    left, right = shingles(a), shingles(b)
    return bool(left and right) and len(left & right) / len(left | right) >= 0.85


def _payload(result):
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            value = json.loads(result)
            return value if isinstance(value, dict) else {}
        except ValueError:
            pass
    return {}


def _terminal_cwd(args, session_id, task_id):
    from tools.approval import get_current_session_key
    from tools.terminal_tool import (
        _get_env_config, _resolve_command_cwd, resolve_task_overrides)
    from pathlib import Path

    config = _get_env_config()
    cwd = _resolve_command_cwd(
        workdir=args.get("workdir"),
        default_cwd=resolve_task_overrides(task_id).get("cwd") or config["cwd"],
        session_key=get_current_session_key(default="") or task_id,
        env_type=config["env_type"])
    return str(Path(cwd).resolve())


def _check(payload, args, session_id, cwd):
    summary = payload.get("verification_evidence")
    if not isinstance(summary, dict):
        return None, None
    identity = _signature("check", [session_id, cwd, args.get("command")])
    row_id = summary.get("id")
    if type(row_id) is int:
        from agent.verification_evidence import verification_evidence_by_id

        found = verification_evidence_by_id(row_id, session_id=session_id, cwd=cwd)
        row = found.get("evidence")
        if isinstance(row, dict) and row.get("cwd") == cwd and row.get("command") == args.get("command"):
            # Identity is the native row's exact command; canonical_command
            # loses test targets and must never key a blocker.
            return _signature("check", [session_id, cwd, row["command"]]), found.get("status")
    # Unresolvable evidence: a recorded failure still blocks; an asserted pass
    # is never recovery proof (identity falls back to the literal invocation).
    return identity, "failed" if summary.get("status") == "failed" else "unverified"


def _target(tool_name, args, task_id):
    if tool_name == "terminal":
        return None  # Requires the effective cwd, resolved on the terminal event path.
    if tool_name == "patch" and args.get("mode") == "patch":
        from tools.file_tools import _collect_v4a_header_paths, _resolve_or_none
        paths, _ = _collect_v4a_header_paths(args.get("patch") or "")
        resolved = [_resolve_or_none(path, task_id) for path in paths]
        return _signature("target", sorted(resolved)) if resolved and all(resolved) else None
    if tool_name in {"patch", "read_file", "write_file"} and isinstance(args.get("path"), str):
        from tools.file_tools import _resolve_or_none
        return _resolve_or_none(args["path"], task_id)
    return _signature("target", [task_id, args])


def _failure(payload, error_type, error_message):
    error = payload.get("error")
    if error_type or error_message or error or payload.get("success") is False or payload.get("ok") is False:
        # Exact structured error identity, not fuzzy similarity of legitimate edits.
        identity = [error_type, payload.get("code"), error, error_message]
        code = payload.get("code") or error_type or "unclassified"
        return _signature("failure", identity), "repeated tool error (" + str(code)[:80] + ")"
    evidence = payload.get("verification_evidence")
    if isinstance(evidence, dict) and evidence.get("status") == "failed":
        # Terminal evidence normally resolves the exact native row on the event
        # path; this fallback keys the literal recorded invocation, never the
        # target-stripped canonical_command.
        check = str(evidence.get("command") or "unknown check")
        return ("verification:" + _signature("check", [check]),
                "verification still fails: " + check[:160])
    return None, None


def register(ctx) -> None:
    from agent.redact import redact_for_egress

    streak = max(2, min(_HISTORY_LIMIT, int(ctx.get_config("tool_call_streak", 5))))
    answer_limit = max(2, int(ctx.get_config("answer_repetition", 3)))
    cap = max(0, int(ctx.get_config("redirect_cap", 2)))
    states = weakref.WeakKeyDictionary()
    lock = threading.RLock()

    def owner(session_id):
        from agent.subagent_lifecycle import get_active_subagent_parent
        agent = get_active_subagent_parent()
        if (agent is None or str(getattr(agent, "session_id", "") or "") != session_id
                or getattr(agent, "_persist_disabled", False)
                or getattr(agent, "_interrupt_requested", False)):
            return None
        return agent

    def state(agent):
        turn = str(getattr(agent, "_current_turn_id", "") or "")
        value = states.get(agent)
        if value is None or (turn and value["turn"] and value["turn"] != turn):
            previous_answer = value["answer"] if value else ""
            previous_count = value["answers"] if value else 0
            value = {"turn": turn, "history": deque(maxlen=_HISTORY_LIMIT),
                     "failures": deque(maxlen=_HISTORY_LIMIT), "delivered": 0,
                     "suppressed": 0, "pending": {}, "blocker": None,
                     "permit": None, "hypothesis": None, "recoveries": 0,
                     "answer": previous_answer, "answers": previous_count,
                     "blocker_event": None, "answer_reminded": False}
            states[agent] = value
        return value

    def problem(s, candidate=None):
        history = list(s["history"])
        if len(history) >= streak:
            recent = history[-streak:]
            if len({(event["call"], event["result"]) for event in recent}) == 1:
                if candidate is None or candidate == recent[-1]["call"]:
                    return "identical call and result repeated", recent
        # Three repetitions distinguish a cycle from an ordinary A/B diagnostic pair.
        for width in (2, 3, 4):
            if len(history) >= width * 3:
                recent = history[-width * 3:]
                pairs = [(e["call"], e["result"]) for e in recent]
                if pairs[:width] == pairs[width:2 * width] == pairs[2 * width:]:
                    if candidate is None or candidate == recent[0]["call"]:
                        return f"unchanged {width}-step action cycle", recent
        failures = list(s["failures"])
        if len(failures) >= streak:
            recent = failures[-streak:]
            if len({(e["tool"], e["failure"]) for e in recent}) == 1:
                return recent[-1]["invariant"], recent
        return None, []

    def blocker(reason, events):
        approaches = [{"tool": e["tool"], "argument_digest": e["call"][:16],
                        "check_status": e.get("check_status")} for e in events[-4:]]
        return {"invariant": redact_for_egress(reason), "approaches": approaches,
                "next_step": "loop_recovery hypothesis: name the assumption and one exact check."}

    def before(tool_name="", args=None, session_id="", tool_call_id="", **_kw):
        agent = owner(session_id)
        if agent is None or tool_name == "loop_recovery" or (
                tool_name == "process_manage" and isinstance(args, dict) and args.get("action") in _POLL_ACTIONS):
            return None
        with lock:
            s = state(agent)
            signature = _signature(tool_name, args)
            if s["permit"] == signature:
                s["permit"] = None
                return None
            reason, events = problem(s, signature)
            if not reason:
                return None
            # A diagnostic using a different tool is not another repetition of the failure.
            if events and events[-1].get("failure") and tool_name != events[-1]["tool"]:
                return None
            if s["blocker"] is None:
                s["blocker"] = blocker(reason, events)
                s["blocker_event"] = events[-1]
            if cap == 0:
                return None
            # Reserve against the cap BEFORE native arbitration acknowledges; the
            # spent counter moves only on the authoritative delivered notification.
            if s["delivered"] + len(s["pending"]) >= cap:
                return None
            directive = {"action": "block",
                         "message": "LOOP GUARD: " + json.dumps(s["blocker"], ensure_ascii=False)}
            if tool_call_id:
                directive_id = "loop:" + uuid.uuid4().hex
                s["pending"][directive_id] = tool_call_id
                # Bound reservations even if the host never acknowledges them.
                while len(s["pending"]) > _HISTORY_LIMIT:
                    s["pending"].pop(next(iter(s["pending"])))
                directive["directive_id"] = directive_id
            # else: a proposal without a native tool_call_id cannot be acknowledged
            # and would hold immortal capacity; it is emitted unreserved.
            return directive

    def delivered(selected_directive_id="", suppressed_directive_ids=None, session_id="",
                  delivered=False, action=None, **_kw):
        agent = owner(session_id)
        if agent is None:
            return
        with lock:
            s = state(agent)
            if delivered and action == "block" and selected_directive_id in s["pending"]:
                s["pending"].pop(selected_directive_id)
                s["delivered"] += 1
            for identity in suppressed_directive_ids or []:
                if identity in s["pending"]:
                    s["pending"].pop(identity)
                    s["suppressed"] += 1

    def after(tool_name="", args=None, result=None, session_id="", error_type=None,
              error_message=None, **_kw):
        agent = owner(session_id)
        if agent is None or tool_name == "loop_recovery":
            return
        if tool_name == "process_manage" and isinstance(args, dict) and args.get("action") in _POLL_ACTIONS:
            return
        if result is None or "LOOP GUARD:" in str(result):
            return
        payload = _payload(result)
        failure, invariant = _failure(payload, error_type, error_message)
        target, check, check_status = "", None, None
        try:
            target = _target(tool_name, args if isinstance(args, dict) else {},
                             str(_kw.get("task_id") or "default"))
            if tool_name == "terminal":
                cwd = _terminal_cwd(args if isinstance(args, dict) else {},
                                    session_id, str(_kw.get("task_id") or "default"))
                check, check_status = _check(payload, args if isinstance(args, dict) else {},
                                             session_id, cwd)
                target = _signature("target", [cwd, args.get("command")])
                if check_status == "failed":
                    failure = "verification:" + check
                    invariant = "verification still fails: " + str(args.get("command"))[:160]
        except Exception as exc:
            # Snapshot extraction is best-effort (the event must still record),
            # but a missing evidence check must never hide WHY it is missing.
            check_status = "snapshot_error"
            logger.warning("loop-guard evidence snapshot failed: %s: %s",
                           type(exc).__name__, exc, exc_info=True)
        event = {"tool": tool_name, "call": _signature(tool_name, args),
                 "result": _signature("result", result), "failure": failure,
                 "invariant": invariant, "target": target, "check": check,
                 "check_status": check_status}
        with lock:
            s = state(agent)
            s["answer"], s["answers"] = "", 0
            s["history"].append(event)
            if failure:
                s["failures"].append(event)
            else:
                def recovered(failed):
                    if not failed or not failed["failure"]:
                        return False
                    if failed["failure"].startswith("verification:"):
                        return check_status == "passed" and failed["check"] == check
                    return bool(target) and failed["tool"] == tool_name and failed["target"] == target

                established = s["blocker_event"]
                repeat_progress = (established and not established["failure"]
                                   and established["call"] == event["call"]
                                   and established["result"] != event["result"])
                # Retire only resolved operations; succeeding on B cannot erase A.
                s["failures"] = deque((e for e in s["failures"] if not recovered(e)),
                                      maxlen=_HISTORY_LIMIT)
                if s["blocker"] and (recovered(established) or repeat_progress):
                    s["recoveries"] += 1
                    s["blocker"] = None
                    s["blocker_event"] = None
                    s["hypothesis"] = None
            reason, events = problem(s)
            if reason and s["blocker"] is None:
                s["blocker"] = blocker(reason, events)
                s["blocker_event"] = events[-1]

    def finished(session_id="", assistant_response="", **_kw):
        agent = owner(session_id)
        if agent is None or not isinstance(assistant_response, str) or not assistant_response.strip():
            return
        with lock:
            s = state(agent)
            s["history"].clear()
            if len(assistant_response.split()) < 12:
                s["answer"], s["answers"] = "", 0
                return
            s["answers"] = s["answers"] + 1 if _similar(s["answer"], assistant_response) else 1
            s["answer"] = assistant_response

    def reminder(session_id="", **_kw):
        agent = owner(session_id)
        if agent is None or cap == 0:
            return None
        with lock:
            s = state(agent)
            if s["answers"] >= answer_limit and not s["answer_reminded"]:
                s["answer_reminded"] = True
                return {"context": "LOOP GUARD: answers repeated without new evidence. "
                        "Run a discriminating check or state the blocker."}
        return None

    def pre_verify(session_id="", **_kw):
        agent = owner(session_id)
        if agent is None or cap == 0:
            return None
        with lock:
            s = state(agent)
            if s["blocker"]:
                return {"action": "continue", "message": "Loop recovery unresolved: " + json.dumps(s["blocker"])}
        return None

    def final_label(response_text="", session_id="", **_kw):
        agent = owner(session_id)
        if agent is None or not isinstance(response_text, str) or cap == 0:
            return None
        with lock:
            unresolved = state(agent)["blocker"]
        if unresolved and "Loop recovery unresolved:" not in response_text:
            return response_text + "\n\nLoop recovery unresolved: " + unresolved["invariant"]
        return None

    def recovery(args, session_id="", **_kw):
        agent = owner(session_id)
        if agent is None:
            return json.dumps({"ok": False, "error": "no owned live agent"})
        if not isinstance(args, dict) or args.get("action") not in {"status", "hypothesis"}:
            return json.dumps({"ok": False, "error": "action must be status or hypothesis"})
        with lock:
            s = state(agent)
            if args["action"] == "hypothesis":
                fields = ("hypothesis", "next_tool", "expected_observation")
                if (not s["blocker"] or any(not isinstance(args.get(k), str) or not args[k].strip()
                                            or len(args[k]) > 1000 for k in fields)
                        or not isinstance(args.get("next_arguments"), dict)
                        or len(json.dumps(args["next_arguments"], default=str)) > 8000
                        or args["next_tool"] == "loop_recovery"):
                    return json.dumps({"ok": False, "error": "an unresolved blocker and bounded explicit hypothesis/check/expected observation are required"})
                s["permit"] = _signature(args["next_tool"], args["next_arguments"])
                s["hypothesis"] = {key: redact_for_egress(args[key]) for key in fields}
            return json.dumps({"ok": True, "blocker": s["blocker"], "hypothesis": s["hypothesis"],
                               "delivered_interventions": s["delivered"], "suppressed_interventions": s["suppressed"],
                               "observed_recoveries": s["recoveries"], "remaining_interventions": max(0, cap - s["delivered"]),
                               "note": "A hypothesis permits one exact check, not permission to bypass native approvals."})

    def clear(session_id="", **_kw):
        agent = owner(session_id)
        if agent is not None:
            with lock:
                states.pop(agent, None)

    ctx.register_tool(name="loop_recovery", toolset="loop_guard", schema={
        "name": "loop_recovery", "description": "Inspect recovery evidence or propose one discriminating check. Does not execute it or override native approval.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["status", "hypothesis"]},
            "hypothesis": {"type": "string"}, "next_tool": {"type": "string"},
            "next_arguments": {"type": "object"}, "expected_observation": {"type": "string"}},
            "required": ["action"], "additionalProperties": False}}, handler=recovery)
    for name, callback in (("pre_tool_call", before), ("post_tool_call", after),
                           ("post_tool_directive", delivered), ("post_llm_call", finished),
                           ("pre_llm_call", reminder), ("pre_verify", pre_verify),
                           ("on_session_reset", clear), ("on_session_finalize", clear)):
        ctx.register_hook(name, callback)
    ctx.register_hook("transform_llm_output", final_label)
