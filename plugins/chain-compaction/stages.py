"""Pure cascade policy for chain-compaction.

Stages are pure (messages in, messages+stats out, no I/O): the engine owns the
classifier transport, token estimation and the built-in fallback. Each stage
either produces a candidate transcript plus its own reduction, or raises
StageError so the engine advances to the next stage — a failing stage never
costs the compression.

Contract the engine enforces around this module:
- stages run in configured order until cumulative reduction >= target_reduction;
- a stage producing less than stage_min_reduction counts as a miss;
- after the last stage, an insufficient cascade falls through to the untouched
  built-in summary (via super().compress), never a half-cascade.
"""
from __future__ import annotations

DEFAULTS = {
    "enabled": True,
    "pipeline": "prune,clip,drop_stale",
    "target_reduction": 0.25,
    "stage_min_reduction": 0.05,
    "endpoint": "http://192.168.50.71:8787",
    "api_key_env": "CLASSIFIER_API_KEY",
    "request_timeout": 20.0,
    "preserve_recent_messages": 6,
    "clip_head_chars": 1200,
    "clip_trigger_chars": 8000,
    "drop_stale_keep_pairs": 4,
    # prune stage: P(drop) required to delete a tool call/result. 0.5 is plain argmax
    # (the service's own choice); higher demands stronger evidence before context goes.
    "drop_confidence": 0.5,
    "prune_defaults": {},
    # Proactive mid-session eviction (host turn_preflight seam), same contract as
    # fast-compaction: off / fifo (positional, no classifier call) / classifier
    # (keep/drop pass first, degrades to fifo per-turn).
    "proactive_prune_mode": "off",
    "proactive_trigger_tokens": 8000,
    "proactive_target_tokens": 2000,
}


class StageError(Exception):
    """This stage cannot contribute (bad input, unavailable dependency, no effect)."""


def parse_pipeline(raw, extra=()):
    allowed = set(STAGES) | {str(e) for e in extra}
    names = [n.strip().lower() for n in str(raw or "").split(",") if n.strip()]
    if not names:
        raise ValueError("pipeline must name at least one stage")
    unknown = [n for n in names if n not in allowed]
    if unknown:
        raise ValueError(f"unknown stage(s): {', '.join(unknown)}; available: {', '.join(sorted(allowed))}")
    return names


def message_text(content):
    # Shared with the fast-compaction pruner; kept local to avoid a cross-plugin
    # import (plugins load independently by directory).
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return "" if content is None else str(content)


def _replace_content(message, text):
    copy_ = dict(message)
    copy_["content"] = text
    copy_.pop("api_content", None)  # never let a sidecar replay pre-clip bytes
    return copy_


def _tool_pairs(messages, preserve_recent):
    """Call-id pairing shared by drop_stale (and mirrors the pruner's contract)."""
    results = {}
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "tool":
            results.setdefault(str(message.get("tool_call_id") or ""), index)
    pairs = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for tc in message.get("tool_calls") or []:
            if isinstance(tc, dict) and tc.get("id"):
                cid = str(tc["id"])
                result_index = results.get(cid)
                recent_start = len(messages) - max(1, int(preserve_recent))
                pairs.append({"id": cid, "call_index": index,
                              "result_index": result_index,
                              # either half inside the window pins the whole pair
                              # (same contract as fast-compaction's pruner)
                              "recent": (index >= recent_start
                                         or (result_index is not None
                                             and result_index >= recent_start))})
    return pairs


def stage_clip(messages, cfg, tokens):
    """Truncate oversized tool results to head+tail with a note. Position-independent,
    no classifier: catches the one thing prune cannot (a single huge live result)."""
    head, trigger = int(cfg["clip_head_chars"]), int(cfg["clip_trigger_chars"])
    if head <= 0 or trigger <= 0:
        raise StageError("clip_disabled")
    out, touched, saved_chars = [], 0, 0
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "tool":
            text = message_text(message.get("content"))
            if len(text) > trigger:
                note = (f"\n[chain-compaction clip: {len(text) - 2 * head} of "
                        f"{len(text)} chars omitted]\n")
                clipped = text[:head] + note + text[-head:]
                saved_chars += len(text) - len(clipped)
                touched += 1
                out.append(_replace_content(message, clipped))
                continue
        out.append(message)
    if not touched:
        raise StageError("clip_nothing_over_trigger")
    return out, {"stage": "clip", "touched": touched, "chars_saved": saved_chars}


def stage_drop_stale(messages, cfg, tokens):
    """Positional fallback when the classifier is down: remove every tool pair older
    than the keep window. Blunter than prune, which is exactly why it is last."""
    keep = max(1, int(cfg["drop_stale_keep_pairs"]))
    pairs = [p for p in _tool_pairs(messages, cfg["preserve_recent_messages"])
             if p["result_index"] is not None and not p["recent"]]
    stale = {p["id"] for p in pairs[:-keep]} if len(pairs) > keep else set()
    if not stale:
        raise StageError("drop_stale_nothing_beyond_window")
    dropped = truncated = 0
    out = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "tool":
            if str(message.get("tool_call_id") or "") in stale:
                dropped += 1
                continue
        if (isinstance(message, dict) and message.get("role") == "assistant"
                and message.get("tool_calls")):
            kept_calls = [tc for tc in message["tool_calls"]
                          if not (isinstance(tc, dict) and str(tc.get("id") or "") in stale)]
            if len(kept_calls) != len(message["tool_calls"]):
                if not message_text(message.get("content")).strip() and not kept_calls:
                    truncated += 1
                    continue  # a message that lost all its content is removed
                copy_ = dict(message)
                copy_["tool_calls"] = kept_calls
                out.append(copy_)
                continue
        out.append(message)
    return out, {"stage": "drop_stale", "dropped_pairs": dropped,
                 "removed_call_msgs": truncated}


def run_stage(name, messages, cfg, tokens):
    """Returns (messages, stats); raises StageError (never any other exception the
    engine cannot treat as a miss) when the stage cannot contribute."""
    if name not in STAGES:
        raise StageError(f"unknown_stage:{name}")
    return STAGES[name](messages, cfg, tokens)


STAGES = {
    "clip": stage_clip,
    "drop_stale": stage_drop_stale,
    # "prune" is injected by the engine (needs the classifier client); kept out of
    # this module so the pure policy imports nothing beyond stdlib.
}
