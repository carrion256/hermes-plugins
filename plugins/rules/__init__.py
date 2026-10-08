"""Rules — port of dsh-rules: Cursor/Windsurf-style rule files.

*.md files in rules_dir, optional YAML-ish front matter:
  applies: always        -> body injected every turn via pre_llm_call
  tools: [terminal, ...] -> body appended to matching tool args via tool_request

Skills natively cover static guidance; rules cover the dynamic pair: injected
constraints on every turn and mechanical transforms of tool calls.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

_FM_RX = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_rule(path: str) -> tuple[dict, str]:
    text = open(path, encoding="utf-8").read()
    m = _FM_RX.match(text)
    meta: dict = {}
    body = text
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
    return meta, body.strip()


def register(ctx) -> None:
    raw_dir = ctx.get_config("rules_dir", "~/.hermes/rules") or ""
    rules_dir = os.path.expanduser(raw_dir) if raw_dir else ""
    if not rules_dir or not os.path.isdir(rules_dir):
        logger.info("rules: no rules dir %s; plugin idle", rules_dir or "(unset)")
        return

    always_rules: list[str] = []
    tool_rules: list[tuple[set, str]] = []
    for fn in sorted(os.listdir(rules_dir)):
        if not fn.endswith(".md"):
            continue
        meta, body = _parse_rule(os.path.join(rules_dir, fn))
        if not body:
            continue
        applies = meta.get("applies", "").lower()
        tools = [t.strip() for t in meta.get("tools", "").strip("[]").split(",") if t.strip()]
        if applies == "always":
            always_rules.append(body)
        elif tools:
            tool_rules.append((set(tools), body))

    if always_rules:
        combined = "\n\n".join(always_rules)
        ctx.register_hook("pre_llm_call", lambda user_message="", **_kw: {"context": combined})
        logger.info("rules: %d always-apply rule(s)", len(always_rules))

    if tool_rules:
        def apply_tool_rule(tool_name: str = "", args=None, **_kw):
            if not isinstance(args, dict):
                return None
            for gate_tools, body in tool_rules:
                if tool_name in gate_tools:
                    # Append the rule to the most prose-like existing field.
                    key = next((k for k in ("prompt", "command", "code", "text", "content") if isinstance(args.get(k), str)), None)
                    if key:
                        return {"args": {**args, key: args[key] + "\n\n# rule:\n" + body},
                                "source": "rules", "reason": f"rule for {tool_name}"}
            return None

        ctx.register_middleware("tool_request", apply_tool_rule)
        logger.info("rules: %d tool-gated rule(s)", len(tool_rules))
