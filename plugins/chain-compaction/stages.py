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

from typing import Final

DEFAULTS = {
    "enabled": True,
    "pipeline": "drop_reasoning,prune,clip,drop_stale",
    "target_reduction": 0.25,
    "stage_min_reduction": 0.05,
    "request_timeout": 20.0,
    "preserve_recent_messages": 6,
    "clip_head_chars": 1200,
    "clip_trigger_chars": 8000,
    "drop_stale_keep_pairs": 4,
    # prune stage: P(drop) required to delete a tool call/result. 0.5 is plain argmax
    # (the service's own choice); higher demands stronger evidence before context goes.
    "drop_confidence": 0.5,
    "prune_defaults": {},
    # Tool names whose call/result pairs no stage may clip or drop. prewalk_ready carries
    # the validated plan handoff. Kept in sync with plugin.yaml; the pure stages treat an
    # absent/empty value as no extra pins, i.e. byte-identical to the previous behaviour.
    "pinned_tools": ("prewalk_ready",),
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


def _call_name(tool_call: dict) -> str:
    """Tool name of a call, tolerant of both the OpenAI and the flat shape."""
    fn = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    return str(fn.get("name") or tool_call.get("name") or "")


def pinned_names(cfg) -> frozenset[str]:
    """Configured tool names no stage may clip or drop.

    An absent/empty value is the empty set, so every stage behaves byte-identically to
    the version without pins. Names come from a list/tuple/set of strings in config; the
    engine parses them once at register (pruner.parse_pinned_tools).
    """
    raw = (cfg or {}).get("pinned_tools") or ()
    if isinstance(raw, str):
        raw = (raw,)
    return frozenset(name for name in (str(item).strip() for item in raw) if name)


def _tool_pairs(messages, preserve_recent, pinned_tools: frozenset[str] = frozenset()):
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
                                             and result_index >= recent_start)),
                              # configured tool pin: the pair is the task record, so
                              # neither clip nor drop_stale may touch it
                              "pinned": _call_name(tc) in pinned_tools})
    return pairs


def stage_clip(messages, cfg, tokens):
    """Truncate oversized tool results to head+tail with a note. Position-independent,
    no classifier: catches the one thing prune cannot (a single huge live result)."""
    head, trigger = int(cfg["clip_head_chars"]), int(cfg["clip_trigger_chars"])
    if head <= 0 or trigger <= 0:
        raise StageError("clip_disabled")
    # Configured tool pins survive clip verbatim: an empty pin set protects nothing,
    # which is exactly the behaviour before the setting existed.
    protected = {pair["result_index"]
                 for pair in _tool_pairs(messages, cfg["preserve_recent_messages"],
                                         pinned_names(cfg))
                 if pair["pinned"] and pair["result_index"] is not None}
    out, touched, saved_chars = [], 0, 0
    for index, message in enumerate(messages):
        if (index not in protected and isinstance(message, dict)
                and message.get("role") == "tool"):
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
    pairs = [p for p in _tool_pairs(messages, cfg["preserve_recent_messages"],
                                    pinned_names(cfg))
             if p["result_index"] is not None and not p["recent"] and not p["pinned"]]
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


# Assistant-side chain-of-thought carriers, read off the host's own replay law:
#   agent/context_compressor.py:1266,1272 -- _REPLAY_BUDGET_KEYS /
#       _NEWEST_TURN_ONLY_BUDGET_KEYS = "reasoning", "reasoning_content"; the wire ships
#       generic thinking keys for the NEWEST turn only, so an older turn's copy is dead
#       budget that never leaves the process.
#   agent/message_sanitization.py:662 -- RC, R, RD = "reasoning_content", "reasoning",
#       "reasoning_details" (REASONING_CARRIERS); these three are the carriers.
#   agent/agent_runtime_helpers.py:826 and agent/anthropic_message_convert.py:18 --
#       ordered content blocks of type "thinking" / "reasoning" / "redacted_thinking".
# Deliberately NOT pruned: "codex_reasoning_items" / "codex_message_items". The host's
# own _prune_stale_reasoning_replay (agent/context_compressor.py, called from
# _commit below) keeps per-turn codex items precisely because "a later switch to a route
# that reads earlier-turn reasoning needs them"; this stage must not undo that.
# Removing a stale reasoning_content is also safe on the must-echo providers
# (DeepSeek/Kimi): the request builder re-pads a tool-call turn with " " at build time
# (agent/chat_completion_helpers.py:1679-1687), so deletion cannot cause an HTTP 400.
REASONING_FIELDS: Final[tuple[str, ...]] = ("reasoning", "reasoning_content", "reasoning_details")
THINKING_BLOCK_TYPES: Final[frozenset[str]] = frozenset(("thinking", "reasoning", "redacted_thinking"))


def _payload_chars(value) -> int:
    """Characters of readable text in a reasoning payload (opaque blobs count as their own
    chars; nothing here is charged for structure)."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_payload_chars(key) + _payload_chars(item)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_payload_chars(item) for item in value)
    return 0


def _strip_reasoning(message: dict) -> tuple[dict, int] | None:
    """Copy ``message`` without its reasoning payloads, plus the characters removed.

    Returns ``None`` when the message carried none, so the caller keeps the original
    object. Visible text (a string ``content``, or non-thinking text blocks) and
    ``tool_calls`` are never rewritten."""
    copy_ = dict(message)
    changed = False
    removed = 0
    for field in REASONING_FIELDS:
        if field in copy_:
            removed += _payload_chars(copy_.pop(field))
            changed = True
    content = copy_.get("content")
    if isinstance(content, list):
        def _is_thinking(block) -> bool:
            return (isinstance(block, dict)
                    and str(block.get("type") or "").strip().lower() in THINKING_BLOCK_TYPES)
        kept = [block for block in content if not _is_thinking(block)]
        if len(kept) != len(content):
            removed += sum(_payload_chars(block) for block in content if _is_thinking(block))
            copy_["content"] = kept
            changed = True
    if not changed:
        return None
    copy_.pop("api_content", None)  # a sidecar would replay the pre-strip bytes
    return copy_, removed


def stage_drop_reasoning(messages, cfg, tokens):
    """Delete chain-of-thought from assistant turns older than the protected window.

    Pure deletion, the one reduction no other stage can make: reasoning is the largest
    per-turn payload a reasoning model produces and the only one whose loss cannot change
    an answer, because the visible text and the tool calls stay byte-identical. Only
    assistant messages outside the newest ``preserve_recent_messages`` are touched, so the
    turn the model is still reasoning through survives; user and tool messages are never
    rewritten."""
    window_start = len(messages) - max(1, int(cfg["preserve_recent_messages"]))
    out, touched, saved_chars = [], 0, 0
    for index, message in enumerate(messages):
        copy_ = None
        if index < window_start and isinstance(message, dict) and message.get("role") == "assistant":
            copy_ = _strip_reasoning(message)
        if copy_ is None:
            out.append(message)
            continue
        stripped, removed = copy_
        touched += 1
        saved_chars += removed
        out.append(stripped)
    if not touched:
        raise StageError("no_reasoning")
    return out, {"stage": "drop_reasoning", "touched": touched, "chars_saved": saved_chars}


def run_stage(name, messages, cfg, tokens):
    """Returns (messages, stats); raises StageError (never any other exception the
    engine cannot treat as a miss) when the stage cannot contribute."""
    if name not in STAGES:
        raise StageError(f"unknown_stage:{name}")
    return STAGES[name](messages, cfg, tokens)


STAGES = {
    "drop_reasoning": stage_drop_reasoning,
    "clip": stage_clip,
    "drop_stale": stage_drop_stale,
    # "prune" is injected by the engine (needs the classifier client); kept out of
    # this module so the pure policy imports nothing beyond stdlib.
}


def load_stage_modules(specs, reserved=()):
    """Import configured stage modules: file paths or dotted module names.

    A stage module exposes ``STAGE_NAME`` (a non-empty string) and
    ``run_stage(messages, cfg, tokens) -> (messages, stats)`` — the same contract the
    built-in stages honour. Names must not shadow a built-in or engine stage, because a
    pipeline resolves stage names to callables.

    Returns ``(stages, problems)``. A module that will not load is reported and skipped
    rather than raised: a bad third-party stage must not take the engine down with it.
    """
    import importlib
    import importlib.util
    from pathlib import Path

    loaded, problems = {}, []
    if specs is None:
        return loaded, problems
    if isinstance(specs, str):
        specs = [specs]
    if not isinstance(specs, (list, tuple)):
        return loaded, [f"stage_modules must be a list, got {type(specs).__name__}"]
    reserved = {str(name) for name in reserved}
    for spec in specs:
        text = str(spec or "").strip()
        if not text:
            continue
        try:
            if text.endswith(".py") or "/" in text:
                path = Path(text).expanduser()
                if not path.is_file():
                    raise ValueError(f"no such file: {path}")
                module_spec = importlib.util.spec_from_file_location(
                    f"chain_stage_{path.stem}", path)
                if module_spec is None or module_spec.loader is None:
                    raise ValueError(f"cannot load {path}")
                module = importlib.util.module_from_spec(module_spec)
                module_spec.loader.exec_module(module)
            else:
                module = importlib.import_module(text)
            stage_name = getattr(module, "STAGE_NAME", None)
            runner = getattr(module, "run_stage", None)
            if not isinstance(stage_name, str) or not stage_name.strip():
                raise ValueError("missing STAGE_NAME")
            stage_name = stage_name.strip().lower()
            if not callable(runner):
                raise ValueError("missing run_stage(messages, cfg, tokens)")
            if stage_name in reserved:
                raise ValueError(f"stage name {stage_name!r} shadows a built-in stage")
            if stage_name in loaded:
                raise ValueError(f"stage name {stage_name!r} already registered")
            loaded[stage_name] = runner
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            problems.append(f"{text}: {type(exc).__name__}: {exc}")
    return loaded, problems
