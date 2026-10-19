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
    """Head+tail window with an explicit gap marker, mirroring droid's TooLarge handling."""
    if len(text) <= limit:
        return text
    head = int(limit * head_share)
    gap = len(text) - limit
    return f"{text[:head]}\n[{gap} characters left out here. Their content is unknown.]\n{text[-(limit - head):]}"


def last_user_message(messages):
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return message_text(message.get("content"))
    return ""
