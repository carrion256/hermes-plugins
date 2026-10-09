"""Auto-review classifier gate — port of dsh-auto-review-classifier.

Registers a native ``pre_tool_call`` risk gate. Risky calls are either blocked
or escalated to Hermes's human-approval directive BEFORE execution.
Classifier errors retain the documented fail-open behavior; this is an
advisory screen, not an authorization boundary.

Read-only tool calls skip screening (fast path).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Tool calls that are safe to skip: read-only, no side effects on external state.
_SKIP_TOOLS = {"read_file", "search_files", "web_search", "web_extract", "skill_view",
               "skills_list", "ha_get_state", "ha_list_entities", "ha_list_services"}
_READ_ONLY_PROCESS_ACTIONS = {"list", "poll", "log", "wait"}

# One-shot risk screen; extend with more heads as needed.
SCHEMA = {"risk": ["safe", "risky"]}


def register(ctx) -> None:
    endpoint = (ctx.get_config("endpoint", "http://192.168.50.149:8787") or "").rstrip("/")
    api_key_env = ctx.get_config("api_key_env", "CLASSIFIER_API_KEY")
    policy = (ctx.get_config("policy", "ask") or "ask").lower()
    if policy not in ("ask", "deny"):
        policy = "ask"

    def classify(text: str) -> str | None:
        """Return 'safe'/'risky' or None on any failure (fail-open)."""
        key = os.environ.get(api_key_env or "", "")
        if not key or not endpoint:
            return None
        body = json.dumps({"text": text[:100000], "schema": SCHEMA}).encode()
        req = urllib.request.Request(
            f"{endpoint}/classify", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                verdict = json.loads(resp.read()).get("risk")
            return verdict if verdict in ("safe", "risky") else None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            return None

    def gate(tool_name: str = "", args: dict | None = None, **_kw):
        if tool_name in _SKIP_TOOLS:
            return None
        if tool_name == "process_manage" and isinstance(args, dict) and args.get("action") in _READ_ONLY_PROCESS_ACTIONS:
            return None
        summary = json.dumps({tool_name: args or {}}, default=str)[:4000]
        if classify(summary) != "risky":
            return None
        logger.info("auto-review: %s flagged risky (policy=%s)", tool_name, policy)
        return {
            "action": "block" if policy == "deny" else "approve",
            "message": f"auto-review-classifier: {tool_name} was classified risky; human review required.",
            "rule_key": f"auto-review:{tool_name}",
        }

    ctx.register_hook("pre_tool_call", gate)
    logger.info("auto-review-classifier: active (policy=%s)", policy)
