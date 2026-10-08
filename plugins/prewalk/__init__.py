"""Task prewalk — port of dsh-prewalk: strong model plans, cheap model executes.

One-way, one-shot per session: when prewalk arms, the planning model gets a
plan nudge via pre_llm_call; at the first successful edit/write mutation the
session switches to the target model — implemented as an llm_request middleware
that rewrites the effective kwargs from then on (verified 2026-10: the
middleware payload is read per API call, so a mid-session swap is honored by
downstream exactly; the target model must be hosted by the same endpoint).
Zero extra LLM calls.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

MUTATING_TOOLS = {"patch", "write_file"}
NUDGE = (
    "PREWALK: plan first. Lay out the full todo list and concrete steps before any "
    "mutation; the execution model will take over at your first edit/write."
)


def register(ctx) -> None:
    target_model = (ctx.get_config("target_model", "") or "").strip()
    do_nudge = bool(ctx.get_config("plan_nudge", True))
    # session_id -> True once handed off (one-shot, one-way)
    handed_off: set[str] = set()
    armed: dict[str, str] = {}

    def prewalk_arm(session_id: str = "", **_kw) -> str:
        if not target_model:
            return json.dumps({"success": False, "error": "prewalk target_model not configured"})
        if session_id in handed_off or session_id in armed:
            return json.dumps({"success": False, "error": "prewalk already armed/handed off for this session"})
        armed[session_id] = target_model
        logger.info("prewalk: armed session %s -> %s", session_id, target_model)
        return json.dumps({"success": True, "armed": True, "target": target_model})

    ctx.register_tool(
        name="prewalk_arm", toolset="prewalk",
        schema={"name": "prewalk_arm",
                "description": "Arm prewalk for this session: planning model steers, target model takes over at first edit/write.",
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: prewalk_arm(**kw),
        description="Arm the plan/exec model handoff for this session.",
    )

    if do_nudge:
        def nudge(user_message: str = "", session_id: str = "", **_kw):
            if session_id in armed and session_id not in handed_off:
                return {"context": NUDGE}
            return None

        ctx.register_hook("pre_llm_call", nudge)

    def watch_mutation(tool_name: str = "", session_id: str = "", result: str = "", **_kw):
        if session_id not in armed or session_id in handed_off:
            return
        if tool_name not in MUTATING_TOOLS:
            return
        try:
            ok = bool(json.loads(result).get("success")) if isinstance(result, str) else False
        except (ValueError, AttributeError):
            ok = False
        if ok:
            handed_off.add(session_id)
            armed.pop(session_id, None)
            logger.info("prewalk: handed off session %s -> %s at first successful %s",
                        session_id, target_model, tool_name)

    ctx.register_hook("post_tool_call", watch_mutation)

    def swap_model(request=None, session_id: str = "", **_kw):
        if session_id in handed_off and isinstance(request, dict) and target_model:
            if request.get("model") != target_model:
                request["model"] = target_model
                return {"request": request}
        return None

    ctx.register_middleware("llm_request", swap_model)
    logger.info("prewalk: active (target=%s)", target_model or "unset — arm refused until configured")
