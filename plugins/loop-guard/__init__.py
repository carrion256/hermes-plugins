"""Owned, result-aware loop reminders without injecting new conversation tasks."""
from __future__ import annotations

import hashlib
import json
import threading
import weakref

_POLL_TOOLS = {"process_manage"}
_POLL_ACTIONS = {"list", "poll", "log", "wait"}


def _signature(name, args) -> str:
    return json.dumps([name, args], sort_keys=True, ensure_ascii=False, default=str)


def _similar(a: str, b: str) -> bool:
    def shingles(text):
        words = text.lower().split()
        return {tuple(words[i:i + 4]) for i in range(max(0, len(words) - 3))}
    left, right = shingles(a), shingles(b)
    return bool(left and right) and len(left & right) / len(left | right) >= 0.85


def register(ctx) -> None:
    streak = max(2, int(ctx.get_config("tool_call_streak", 5)))
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
        return states.setdefault(agent, {"call": None, "result": None, "count": 0,
                                        "tool_redirects": 0, "answer_redirects": 0,
                                        "answer": "", "answers": 0})

    def before(tool_name="", args=None, session_id="", **_kw):
        agent = owner(session_id)
        if agent is None:
            return None
        with lock:
            s = state(agent)
            if (s["call"] == _signature(tool_name, args) and s["count"] >= streak
                    and s["tool_redirects"] < cap):
                s["count"] = 0
                s["tool_redirects"] += 1
                return {"action": "block", "message": (
                    f"LOOP GUARD: {tool_name} returned the same result {streak} times. "
                    "Repeating that exact call is not progress. Use a different query/action "
                    "or explain the blocker. Read-only polling is exempt."
                )}
        return None

    def after(tool_name="", args=None, result=None, session_id="", **_kw):
        agent = owner(session_id)
        if agent is None:
            return
        with lock:
            s = state(agent)
            s["answer"], s["answers"] = "", 0
            if tool_name in _POLL_TOOLS and isinstance(args, dict) and args.get("action") in _POLL_ACTIONS:
                s["call"], s["count"] = None, 0
                return
            if result is None or "LOOP GUARD:" in str(result):
                return
            signature = _signature(tool_name, args)
            digest = hashlib.sha256(str(result).encode()).hexdigest()
            s["count"] = s["count"] + 1 if (signature, digest) == (s["call"], s["result"]) else 1
            s["call"], s["result"] = signature, digest

    def finished(session_id="", assistant_response="", **_kw):
        agent = owner(session_id)
        if agent is None or not isinstance(assistant_response, str):
            return
        with lock:
            s = state(agent)
            s["call"], s["result"], s["count"] = None, None, 0
            if len(assistant_response.split()) < 12:
                s["answer"], s["answers"] = "", 0
                return
            s["answers"] = s["answers"] + 1 if _similar(s["answer"], assistant_response) else 1
            s["answer"] = assistant_response

    def reminder(session_id="", **_kw):
        agent = owner(session_id)
        if agent is None:
            return None
        with lock:
            s = state(agent)
            if s["answers"] >= answer_limit and s["answer_redirects"] < cap:
                s["answers"] = 0
                s["answer_redirects"] += 1
                return {"context": (
                    "LOOP GUARD: prior answers repeated without intervening tool progress. "
                    "Address the current user request with new evidence or a concrete blocker; "
                    "do not substitute another repetition."
                )}
        return None

    def clear(session_id="", **_kw):
        agent = owner(session_id)
        if agent is not None:
            with lock:
                states.pop(agent, None)

    ctx.register_hook("pre_tool_call", before)
    ctx.register_hook("post_tool_call", after)
    ctx.register_hook("post_llm_call", finished)
    ctx.register_hook("pre_llm_call", reminder)
    ctx.register_hook("on_session_reset", clear)
    ctx.register_hook("on_session_finalize", clear)
