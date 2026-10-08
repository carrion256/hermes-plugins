"""Auto-review classifier gate — port of dsh-auto-review-classifier.

Registers ``tool_execution`` middleware: every tool call is screened through
the self-hosted classifier (POST /classify) BEFORE downstream execution.
A risky verdict short-circuits the call (deny) or falls through to the normal
approval flow (ask). Classifier errors fail OPEN — an unreachable endpoint
never wedges the agent.

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
               "skills_list", "ha_get_state", "ha_list_entities", "ha_list_services",
               "process_manage"}

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

    def gate(tool_name: str = "", args: dict | None = None, next_call=None, **_kw):
        if tool_name in _SKIP_TOOLS or not next_call:
            return next_call(args or {}) if next_call else None
        summary = json.dumps({tool_name: args or {}}, default=str)[:4000]
        verdict = classify(summary)
        if verdict == "risky":
            if policy == "deny":
                logger.info("auto-review: denied %s (classifier risky)", tool_name)
                return json.dumps({"success": False, "blocked": True,
                                   "reason": "auto-review-classifier: verdict risky"})
            # policy == "ask": run the call anyway but stamp a review note —
            # real approval flow is core's; this seam short-circuits only on deny.
            logger.info("auto-review: %s flagged risky (policy=ask, proceeding)", tool_name)
        return next_call(args or {})

    ctx.register_middleware("tool_execution", gate)
    logger.info("auto-review-classifier: active (policy=%s, endpoint=%s)", policy, endpoint or "none")
