"""Text helpers shared by the gate, signals and addons: canonical JSON, native
egress redaction, Factory-Judge transcript rendering and head+tail windows."""
from __future__ import annotations

import json


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def egress(text):
    """Native egress redaction; raises when the scrubber cannot vouch for the text."""
    from agent.redact import REDACTION_UNAVAILABLE, redact_for_egress
    safe = redact_for_egress(text)
    if not isinstance(safe, str) or REDACTION_UNAVAILABLE in safe:
        raise ValueError("redaction unavailable")
    return safe


def clip(text, keep=600):
    if len(text) <= 2 * keep:
        return text
    return f"{text[:keep]} … [{len(text) - 2 * keep} chars omitted] … {text[-keep:]}"


def message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return "" if content is None else str(content)


def render_transcript(messages, keep=600):
    """Factory-Judge style rendering: role-tagged lines, tool payloads clipped head+tail."""
    lines = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "")).upper()
        if role == "SYSTEM":
            continue
        text = message_text(message.get("content"))
        if role == "TOOL":
            lines.append(f"TOOL_RESULT [id={index}]: {clip(text, keep)}")
            continue
        if text:
            lines.append(f"{role or 'UNKNOWN'} [id={index}]: {text}")
        for call in message.get("tool_calls") or []:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            lines.append(f"{role} [id={index}] TOOL_CALL {function.get('name', '?')}: "
                         f"{clip(str(function.get('arguments', '')), keep)}")
    return "\n".join(lines)


def window(text, limit, head_share=0.33):
    """Head+tail window with an explicit gap marker, mirroring droid's TooLarge handling.

    The marker is charged against ``limit`` rather than added on top, so the returned string
    never exceeds it. Callers size this against the service's text cap, where an overshoot of
    even the marker length caused the whole payload to be rejected as too long.
    """
    if len(text) <= limit:
        return text
    # Upper bound on the marker: the omitted count can never need more digits than len(text).
    marker_len = len(f"\n[{len(text)} characters left out here. Their content is unknown.]\n")
    keep = limit - marker_len
    if keep <= 0:  # limit smaller than the marker itself: nothing but a hard cut is honest
        return text[:limit]
    gap = len(text) - keep
    marker = f"\n[{gap} characters left out here. Their content is unknown.]\n"
    head = int(keep * head_share)
    tail = keep - head
    return f"{text[:head]}{marker}{text[-tail:] if tail else ''}"[:limit]


def last_user_message(messages):
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return message_text(message.get("content"))
    return ""
