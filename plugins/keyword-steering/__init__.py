"""Keyword steering — config-driven steering text on genuine prose keywords.

Detection ignores fenced code blocks, inline code spans, and quoted spans;
whole-word matching already excludes identifier/path contexts (a keyword glued
to ``/``, ``_``, or word characters never matches). Guidance is injected once
per user turn via ``pre_llm_call`` (the native per-turn seam), deduplicated by
``(session_id, turn_id)``. Turn keys are dropped on ``on_session_reset`` /
``on_session_finalize`` and on ``agent_loop_stopped`` (bounded LRU backstop).
No messages or tool args are ever rewritten.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_FENCE_RX = re.compile(r"(?s)^(`{3,}|~{3,})")
_INLINE_RX = re.compile(r"(?s)`[^`]*`")
_QUOTE_RX = re.compile(r"(?:\"[^\"]*\"|'[^']*')")
_MAX_TURNS = 512


def _read_config(ctx) -> dict:
    raw = ctx.get_config("keywords") or {}
    if not isinstance(raw, dict):
        return {}
    table = {}
    for kw, text in raw.items():
        if not isinstance(kw, str) or not isinstance(text, str) or not kw.strip() or not text.strip():
            logger.warning("keyword-steering: skipping invalid entry %r", kw)
            continue
        table[kw.strip().lower()] = text.strip()
    return table


def _prose_only(message: str) -> str:
    """Strip fenced blocks, inline code, and quoted spans; prose stays."""
    kept = []
    in_fence = False
    for line in message.splitlines():
        m = _FENCE_RX.match(line.strip())
        if m:
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = _INLINE_RX.sub(" ", line)
        line = _QUOTE_RX.sub(" ", line)
        kept.append(line)
    return "\n".join(kept)


def register(ctx) -> None:
    table = _read_config(ctx)
    if not table:
        logger.info("keyword-steering: no keywords configured; hook registered but idle")

    # Whole-word AND path-safe: no word char, and no path separator or
    # extension dot touching the keyword ("src/deploy/hooks.py" never hits).
    patterns = {
        kw: re.compile(
            rf"(?<![\w./\\-]){re.escape(kw)}(?!\w)(?![./\\][A-Za-z0-9])",
            re.IGNORECASE)
        for kw in table
    }

    seen_turns: dict = {}  # insertion-ordered; oldest evicted past _MAX_TURNS

    def pre_llm_call(user_message: str = "", session_id: str = "",
                     turn_id: str = "", **_kw):
        if not user_message or not patterns:
            return None
        key = (str(session_id or ""), str(turn_id or ""))
        if key in seen_turns:
            return None
        if all((session_id, turn_id)):  # dedup only when both ids are real
            seen_turns[key] = True
            while len(seen_turns) > _MAX_TURNS:
                seen_turns.pop(next(iter(seen_turns)))
        hits = [table[kw] for kw, rx in patterns.items()
                if rx.search(_prose_only(user_message))]
        if not hits:
            return None
        return {"context": "\n\n".join(hits)}

    def _drop_turns(**_kw):
        seen_turns.clear()

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_reset", _drop_turns)
    ctx.register_hook("on_session_finalize", _drop_turns)
    ctx.register_hook("agent_loop_stopped", _drop_turns)
    logger.info("keyword-steering: active with %d keywords", len(table))
