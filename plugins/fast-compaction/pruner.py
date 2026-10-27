"""Pure compaction policy ported from tamaratran/fast-jev-compaction (src/).

No I/O and no host imports: pairing, state fitting, request batching and decision
application operate on OpenAI-format messages, so the whole pipeline is unit-testable
with a fake classifier. The engine supplies the classifier transport, egress redaction
and the token estimator.

Invariant: only tool calls and their paired results are ever candidates. User and
assistant text is never removed or shortened in the output (it is only abridged in the
state the classifier sees). Nothing kept is ever rewritten.
"""
from __future__ import annotations

import json

MAX_TEXT = 100_000            # service limit on the text field
STATE_MAX_CHARS = 90_000      # keeps the body under the 256 KB cap with schema room
MAX_HEADS = 32                # service limit per request
# One request carries two heads per tool call ({tag}_call and {tag}_result), so 16 tool
# calls = 32 heads = one request already at the service cap. Per-request latency is
# dominated by re-reading the state (measured ~0.1 ms/char), not by head count, so the
# only lever that matters is how many times the state is sent -- keep it at one.
HEADS_PER_TOOL_CALL = 2
TOOL_CALLS_PER_REQUEST = MAX_HEADS // HEADS_PER_TOOL_CALL
_VERDICTS = ("keep", "drop")

TRUNC_NOTE = ("[fast-compaction: tool output no longer needed; first {kept} of {total} "
              "chars shown, remainder omitted]")
COLLAPSE_NOTE = "[… {n} chars omitted …]"
OMITTED_NOTE = "ok, {total} chars (omitted)"

DEFAULTS = {
    "enabled": True,
    "endpoint": "http://192.168.50.71:8787",
    "api_key_env": "CLASSIFIER_API_KEY",
    "preserve_recent_messages": 6,
    "min_reduction": 0.25,
    "max_state_tokens": 20000,
    "max_request_tokens": 28000,
    "truncate_head_chars": 300,
    "request_timeout": 20.0,
    # Proactive mid-session eviction (host turn_preflight seam). off keeps the
    # host's deterministic passes; fifo drops oldest unpinned tool pairs with no
    # classifier call; classifier runs the keep/drop pass first and degrades to
    # fifo per-turn on any classifier fault.
    "proactive_prune_mode": "off",
    "proactive_trigger_tokens": 8000,
    "proactive_target_tokens": 2000,
}


class PruneError(Exception):
    """Any condition that must route this compression to the built-in summary path."""


def message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return "" if content is None else str(content)


def _clip(text, keep):
    if not isinstance(text, str) or len(text) <= 2 * keep:
        return text
    return f"{text[:keep]} … [{len(text) - 2 * keep} chars omitted] … {text[-keep:]}"


def _call_fields(tc):
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
    name = fn.get("name") or tc.get("name") or "tool"
    args = fn.get("arguments")
    if args is None:
        args = tc.get("input")
    if not isinstance(args, str):
        try:
            args = json.dumps(args, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            args = str(args)
    return str(tc.get("id") or ""), str(name), args


def collect_tool_calls(messages, preserve_recent):
    """Pair every tool_use with its tool_result by call id; pin the first message,
    the newest ``preserve_recent`` messages, and any call whose result is missing."""
    results = {}
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            results.setdefault(call_id, index)
    preserve_recent = max(1, int(preserve_recent))
    recent_start = len(messages) - preserve_recent
    calls = {}
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for tc in message.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            call_id, name, args = _call_fields(tc)
            if not call_id:
                continue
            result_index = results.get(call_id)
            record = {
                "call_id": call_id, "tool": name, "arguments": args,
                "assistant_index": index, "result_index": result_index,
                "result_text": (message_text(messages[result_index].get("content"))
                                if result_index is not None else ""),
                # A pair is recent if EITHER half is: a call sitting just outside
                # the window whose result is inside it is part of the live task
                # (found live 2026-10-24: the classifier dropped the fix's
                # edit_file call exactly on that boundary).
                "pinned": (index == 0 or index >= recent_start
                           or result_index is None
                           or result_index >= recent_start),
            }
            calls.setdefault(call_id, record)
    tag = 0
    for record in calls.values():
        record["tag"] = f"t{tag}"
        tag += 1
    return calls


def eligible_calls(calls):
    return [record for record in calls.values() if not record["pinned"]]


def extract_goal(messages):
    texts = [message_text(m.get("content")).strip() for m in messages
             if isinstance(m, dict) and m.get("role") == "user"]
    texts = [t for t in texts if t][-3:]
    return _clip(" | ".join(texts), 400)


def _render_state(messages, calls, args_clip, text_clip, collapse):
    """Whole conversation, oldest first; every tool result replaced by a short note;
    tool inputs included; texts included (abridged only at the later stages)."""
    by_assistant = {}
    for record in calls.values():
        by_assistant.setdefault(record["assistant_index"], []).append(record)
    rendered_by_result = {record["result_index"] for record in calls.values()
                          if record["result_index"] is not None}
    lines = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "tool":
            if index in rendered_by_result:
                continue  # rendered on its call line
            total = len(message_text(message.get("content")))
            lines.append(f"[{index}] tool: {OMITTED_NOTE.format(total=total)}")
            continue
        text = message_text(message.get("content")).strip()
        if text:
            if collapse and index != 0:
                lines.append(f"[{index}] {role}: {COLLAPSE_NOTE.format(n=len(text))}")
            else:
                lines.append(f"[{index}] {role}: {_clip(text, text_clip) if text_clip else text}")
        for record in by_assistant.get(index, ()):
            head = (f"[{index}] {record['tag']} {record['tool']} "
                    f"{_clip(record['arguments'], args_clip)}")
            if record["result_index"] is not None:
                head += f" → {OMITTED_NOTE.format(total=len(record['result_text']))}"
            else:
                head += " → (no result yet)"
            lines.append(head)
    return "\n".join(lines)


def fit_state(messages, calls, goal, max_state_tokens, max_request_tokens, text_tokens):
    """Fit the state in stages, each applied only when the previous one was not enough
    (upstream behaviour). Raises PruneError when even the last stage does not fit."""
    goal_header = f"Goal: {goal}\n\n" if goal else ""
    candidates = (
        (1000, None, False), (200, None, False), (60, None, False),
        (60, 600, False), (60, 200, False), (60, 80, False), (60, 80, True),
    )
    for args_clip, text_clip, collapse in candidates:
        state = goal_header + _render_state(messages, calls, args_clip, text_clip, collapse)
        if not state.strip():
            raise PruneError("empty_state")
        if (len(state) <= STATE_MAX_CHARS
                and text_tokens(state) <= min(max_state_tokens, max_request_tokens) - 2000):
            return state
    raise PruneError("state_does_not_fit")


def build_batches(calls):
    return [calls[i:i + TOOL_CALLS_PER_REQUEST] for i in range(0, len(calls), TOOL_CALLS_PER_REQUEST)]


def _head_descriptions(record):
    args = _clip(record["arguments"], 120)
    total = len(record["result_text"])
    call = {"keep": (f"line {record['tag']}: the assistant once ran {record['tool']} with "
                     f"{args}; knowing this call happened still matters for continuing the "
                     "current task"),
            "drop": (f"line {record['tag']}: the {record['tool']} call {args} is stale; "
                     "the fact that it was made no longer matters for the current task")}
    result = {"keep": (f"line {record['tag']}: the {total}-char verbatim output of "
                       f"{record['tool']} is still needed; re-running the tool would not "
                       "reproduce it"),
              "drop": (f"line {record['tag']}: the {total}-char output of {record['tool']} "
                       "is stale, irrelevant, or could be re-obtained by re-running the tool")}
    return call, result


def build_request(batch, state):
    """One /classify request: the same full state plus two single-label (choice) heads per
    tool call -- {tag}_call and {tag}_result, each carrying its own label descriptions.
    Returns (text, schema); raises PruneError when the request shape is invalid."""
    schema = {}
    for record in batch:
        call_labels, result_labels = _head_descriptions(record)
        schema[f"{record['tag']}_call"] = {"labels": call_labels}
        schema[f"{record['tag']}_result"] = {"labels": result_labels}
    if not 1 <= len(schema) <= MAX_HEADS:
        raise PruneError("invalid_head_count")
    if len(state) > MAX_TEXT:
        raise PruneError("state_too_long")
    return state, schema


def parse_response(data, batch):
    """Strict: every head for every call must answer keep|drop, or the whole
    compression falls back (a malformed answer is never a partial prune)."""
    if not isinstance(data, dict):
        raise PruneError("malformed_answer")
    decisions = {}
    for record in batch:
        got = {}
        for kind in ("call", "result"):
            verdict = data.get(f"{record['tag']}_{kind}")
            if not isinstance(verdict, str) or verdict not in _VERDICTS:
                raise PruneError(f"malformed_answer:{record['tag']}_{kind}")
            got[kind] = verdict == "keep"
        decisions[record["call_id"]] = got
    return decisions


def _replace_content(message, text):
    copy_ = dict(message)
    copy_["content"] = text
    copy_.pop("api_content", None)  # never let a sidecar replay pre-prune bytes
    return copy_


def apply_decisions(messages, calls, decisions, truncate_head_chars):
    """keepResult → keep both; keepCall → keep the call, truncate the result to a head
    excerpt plus a one-line note; otherwise → remove the call together with its result.
    Copy-on-write: untouched messages are returned as the same objects."""
    drop_results, truncate_results, drop_calls = set(), set(), set()
    for call_id, got in decisions.items():
        record = calls.get(call_id)
        if record is None or record["pinned"]:
            continue
        if got["result"]:
            continue
        if got["call"]:
            truncate_results.add(call_id)
        else:
            drop_calls.add(call_id)
            drop_results.add(call_id)

    truncate_head_chars = max(0, int(truncate_head_chars))
    out, dropped, truncated, chars_saved = [], 0, 0, 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            out.append(message)
            continue
        if message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id in drop_results:
                dropped += 1
                chars_saved += len(message_text(message.get("content")))
                continue
            if call_id in truncate_results:
                original = message_text(message.get("content"))
                head = original[:truncate_head_chars]
                note = TRUNC_NOTE.format(kept=len(head), total=len(original))
                truncated += 1
                chars_saved += max(0, len(original) - len(head) - len(note))
                out.append(_replace_content(message, (head + "\n" + note) if head else note))
                continue
            out.append(message)
            continue
        if message.get("role") == "assistant" and message.get("tool_calls"):
            kept_calls = [tc for tc in message["tool_calls"]
                          if not (isinstance(tc, dict)
                                  and str(tc.get("id") or "") in drop_calls)]
            if len(kept_calls) != len(message["tool_calls"]):
                if not message_text(message.get("content")).strip() and not kept_calls:
                    continue  # a message that lost all its content is removed
                copy_ = dict(message)
                copy_["tool_calls"] = kept_calls
                out.append(copy_)
                continue
        out.append(message)
    stats = {"candidates": len(decisions), "dropped": dropped, "truncated": truncated,
             "kept": len(decisions) - dropped - truncated, "chars_saved": chars_saved}
    return out, stats
