"""Subagent effort routing — port of dsh-subagent-effort.

subagent_start fires with parent_session_id/child_session_id/child_goal.
We capture the child's goal, classify it (simple | complex) against the
self-hosted classifier, and rewrite reasoning_effort on that child's LLM
requests via llm_request middleware (identity = session_id, verified in the
fire sites). Fail-open: classifier absent/unreachable/error keeps effort
untouched.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

VALID_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
SCHEMA = {"difficulty": ["simple", "complex"]}


def register(ctx) -> None:
    endpoint = (ctx.get_config("endpoint", "http://192.168.50.149:8787") or "").rstrip("/")
    api_key_env = ctx.get_config("api_key_env", "CLASSIFIER_API_KEY")
    low = ctx.get_config("low_effort", "low")
    high = ctx.get_config("high_effort", "high")
    if low not in VALID_EFFORTS or high not in VALID_EFFORTS:
        logger.warning("subagent-effort: invalid effort config (low=%r high=%r); plugin idle", low, high)
        return
    # child_session_id -> effort verdict
    verdicts: dict[str, str] = {}

    def classify(goal: str) -> str | None:
        key = os.environ.get(api_key_env or "", "")
        if not key or not endpoint or not goal:
            return None
        body = json.dumps({"text": goal[:100000], "schema": SCHEMA}).encode()
        req = urllib.request.Request(
            f"{endpoint}/classify", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                v = json.loads(resp.read()).get("difficulty")
            return v if v in ("simple", "complex") else None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            return None

    def on_subagent_start(child_session_id: str = "", child_goal: str = "", **_kw):
        if not child_session_id:
            return
        v = classify(child_goal or "")
        if v:
            verdicts[child_session_id] = high if v == "complex" else low
            logger.info("subagent-effort: child %s -> %s (%s)",
                        child_session_id, verdicts[child_session_id], v)

    def apply_effort(request=None, session_id: str = "", **_kw):
        verdict = verdicts.get(session_id)
        if verdict is None or not isinstance(request, dict):
            return None
        if request.get("reasoning_effort") != verdict:
            request["reasoning_effort"] = verdict
            return {"request": request}
        return None

    ctx.register_hook("subagent_start", on_subagent_start)
    ctx.register_middleware("llm_request", apply_effort)
    logger.info("subagent-effort: active (%s/%s)", low, high)
