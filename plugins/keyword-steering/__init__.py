"""Keyword steering — port of dsh-keyword-steering onto pre_llm_call.

Injects config-defined steering text when the user message triggers on a
keyword (case-insensitive, whole-word match).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


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


def register(ctx) -> None:
    table = _read_config(ctx)
    if not table:
        logger.info("keyword-steering: no keywords configured; hook registered but idle")

    # Precompile whole-word patterns so hot path is regex-only.
    patterns = {
        kw: re.compile(rf"(?<!\w){re.escape(kw)}(?!\w)", re.IGNORECASE)
        for kw in table
    }

    def pre_llm_call(user_message: str = "", **kwargs):
        if not user_message or not patterns:
            return None
        hits = [table[kw] for kw, rx in patterns.items() if rx.search(user_message)]
        if not hits:
            return None
        return {"context": "\n\n".join(hits)}

    ctx.register_hook("pre_llm_call", pre_llm_call)
    logger.info("keyword-steering: active with %d keywords", len(table))
