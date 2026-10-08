"""Prompt continuation — port of dsh-continuation onto on_session_end + inject_message.

Catches closing turns that left work undone and injects one short steer so the
session resumes: unexpected-stop ("I'll fix that now" with no tool call) and
empty-stop. At most one steer per closing turn, capped per session.

Seam note (verified 2026-10): the installed Hermes has no GoalProvider registry.
post_api_request carries the raw response dict — the plugin captures the last
assistant message (text, tool_calls) there; on_session_end (fires at the end of
every run_conversation, kwargs carry no messages) decides and steers via
ctx.inject_message (queues into CLI, or gateway with session_key). The hook
cannot veto the turn — the steer arrives as a new queued message.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Future-tense promises without an accompanying tool call are the OMP
# unexpected-stop signature. Conservative: only strong action promises.
_PROMISE_RX = re.compile(
    r"\b(i(?:'| a)m going to|i(?:' ll| will)|let me|now i(?:' ll| will| am going to))\b"
    r"[^.]{0,120}\b(fix|check|run|test|apply|implement|update|read|open|install|start|deploy|try)\b",
    re.IGNORECASE,
)

_EMPTY_STEER = "Your last turn produced no text, reasoning, or tool calls. An empty turn is not an answer — continue the work or state the blocker."
_PROMISE_STEER = "Your last message promised action ({promise}) but no tool call followed. Do it now, or explain what is blocking you."


def register(ctx) -> None:
    max_steers = max(0, int(ctx.get_config("max_steers_per_session", 3) or 3))
    do_empty = bool(ctx.get_config("empty_stop", True))
    do_promise = bool(ctx.get_config("unexpected_stop", True))
    steered: dict[str, int] = {}
    # session_id -> (text, has_tool_calls) of the last API response this turn
    last_response: dict[str, tuple[str, bool]] = {}

    def post_api_request(session_id: str = "", response=None, assistant_content_chars: int = 0,
                        assistant_tool_call_count: int = 0, **_kw) -> None:
        text = ""
        if isinstance(response, dict):
            try:
                text = (response["choices"][0]["message"].get("content") or "")
            except (KeyError, IndexError, TypeError):
                text = ""
        if not text and assistant_content_chars:
            text = "x"  # non-empty marker; we only test emptiness here
        last_response[session_id] = (text, bool(assistant_tool_call_count))

    def on_session_end(session_id: str = "", completed: bool = True,
                       interrupted: bool = False, **kwargs) -> None:
        if session_id in steered and steered[session_id] >= max_steers:
            return
        if interrupted:
            # The user deliberately stopped this turn; do not steer over it.
            last_response.pop(session_id, None)
            return
        last = last_response.pop(session_id, None)
        if last is None:
            return
        text, has_tools = last

        steer = None
        if do_empty and not text and not has_tools:
            steer = _EMPTY_STEER
        elif do_promise and text and not has_tools:
            m = _PROMISE_RX.search(text)
            if m:
                steer = _PROMISE_STEER.format(promise=m.group(0)[:60])
        if not steer:
            return
        if steered.get(session_id, 0) >= max_steers:
            logger.info("continuation: steer cap reached for session %s", session_id)
            return
        # session_key doubles as session_id in gateway mode; CLI injection
        # ignores it. inject_message returns False when not allowed/available.
        try:
            ok = ctx.inject_message(steer, session_key=session_id or None)
        except Exception:
            logger.debug("continuation: inject failed", exc_info=True)
            return
        if ok:
            steered[session_id] = steered.get(session_id, 0) + 1
            logger.info("continuation: steered session %s (attempt %d)", session_id, steered[session_id])

    ctx.register_hook("post_api_request", post_api_request)
    ctx.register_hook("on_session_end", on_session_end)
    logger.info("continuation: active (max %d steers/session)", max_steers)
