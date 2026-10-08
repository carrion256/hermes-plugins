"""Loop guard — port of dsh-loop-guard (turn-level detectors).

Two cross-turn guards, both detect+steer via ctx.inject_message:
1. tool-call loop: N consecutive executed tool-call batches with identical
   signatures (name+args, order-insensitive) -> "never call X with those args".
2. answer repetition: N consecutive near-identical text answers with no tool
   call -> "repeating the answer is not progress".

Streaming reasoning-loop detectors (Z-algorithm cycles) are deliberately NOT
ported: on_stream_delta exists but mid-stream steering has no inject channel —
detect-only there would be inert infrastructure. Turn-level guards carry the
practical value.

v1 note: the tool-call detector signatures individual calls (name+args), not
whole batches — a mixed batch repeated verbatim won't trip it. Single-tool
streaks are the dominant real pathology.
"""

from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)


def _norm_batch(calls) -> str:
    items = sorted(
        f"{c.get('function', {}).get('name', '')}:{json.dumps(c.get('function', {}).get('arguments', ''), sort_keys=True, default=str)}"
        for c in (calls or [])
    )
    return hashlib.sha256("|".join(items).encode()).hexdigest()[:16]


def _shingles(text: str) -> set:
    words = text.lower().split()
    return {tuple(words[i:i + 4]) for i in range(len(words) - 3)} if len(words) > 8 else {tuple(words)}


def _similar(a: str, b: str) -> bool:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return False
    return len(sa & sb) / len(sa | sb) >= 0.85


def register(ctx) -> None:
    streak_n = max(2, int(ctx.get_config("tool_call_streak", 5) or 5))
    answer_n = max(2, int(ctx.get_config("answer_repetition", 3) or 3))
    cap = max(1, int(ctx.get_config("redirect_cap", 2) or 2))
    # session_id -> state
    batch_state: dict[str, tuple[str, int]] = {}
    answer_state: dict[str, tuple[str, int]] = {}
    redirects: dict[str, int] = {}

    def post_tool_call(tool_name: str = "", args=None, tool_call_id: str = "",
                       session_id: str = "", **_kw):
        # Accumulate the batch signature per (session, turn) — the turn's calls
        # share turn_id; we key on session and reset the streak on signature change.
        sig = _norm_batch([{"function": {"name": tool_name, "arguments": args}}])
        prev_sig, count = batch_state.get(session_id, ("", 0))
        batch_state[session_id] = (sig, count + 1 if sig == prev_sig else 1)
        if batch_state[session_id][1] >= streak_n and redirects.get(session_id, 0) < cap:
            redirects[session_id] = redirects.get(session_id, 0) + 1
            batch_state[session_id] = (sig, 0)  # reset so we don't spam
            try:
                ctx.inject_message(
                    f"LOOP GUARD: you called {tool_name} with identical arguments "
                    f"{streak_n}+ times with no progress. Do not call {tool_name} "
                    "with those arguments again — take a different action or state "
                    "the blocker.", session_key=session_id or None)
            except Exception:
                logger.debug("loop-guard: inject failed", exc_info=True)

    def post_api_request(session_id: str = "", response=None,
                         assistant_tool_call_count: int = 0, **_kw):
        if assistant_tool_call_count:
            return  # text answers only
        text = ""
        if isinstance(response, dict):
            try:
                text = (response["choices"][0]["message"].get("content") or "").strip()
            except (KeyError, IndexError, TypeError):
                text = ""
        if len(text.split()) < 12:
            return
        prev, count = answer_state.get(session_id, ("", 0))
        if prev and _similar(prev, text):
            count += 1
            if count >= answer_n and redirects.get(session_id, 0) < cap:
                redirects[session_id] = redirects.get(session_id, 0) + 1
                answer_state[session_id] = (text, 0)
                try:
                    ctx.inject_message(
                        "LOOP GUARD: you have repeated the same answer several times "
                        "without any tool calls. Repeating the answer is not progress — "
                        "take the next concrete tool action or finish with something new.",
                        session_key=session_id or None)
                except Exception:
                    logger.debug("loop-guard: inject failed", exc_info=True)
                return
        else:
            count = 0
        answer_state[session_id] = (text, count)

    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("post_api_request", post_api_request)
    logger.info("loop-guard: active (tool streak %d, answer repetition %d)", streak_n, answer_n)
