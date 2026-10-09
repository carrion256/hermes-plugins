"""Endorse unanswered clarification defaults in their originating tool result.

Human answers are never changed. Automatic decisions are explicitly labelled,
question-bound derived suggestions, not synthetic user messages or approvals.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)
_UNSAFE = re.compile(
    r"\b(approv\w*|authoriz\w*|consent|permission|password|credential\w*|secret\w*|"
    r"delet\w*|destroy\w*|drop\w*|wipe\w*|public\w*|publish\w*|deploy\w*|"
    r"payment\w*|transfer\w*|purchase\w*|production|sign\w*)\b", re.IGNORECASE,
)


def register(ctx) -> None:
    endpoint = (ctx.get_config("endpoint", "http://192.168.50.149:8787") or "").rstrip("/")
    api_key_env = ctx.get_config("api_key_env", "CLASSIFIER_API_KEY")
    enabled = bool(ctx.get_config("enabled", True))
    dry_run = bool(ctx.get_config("dry_run", False))

    def classify(question: str, choices: list[str]) -> bool:
        key = os.environ.get(api_key_env or "", "")
        if not key or not endpoint:
            return False
        labels = {str(i): option for i, option in enumerate(choices)}
        schema = {
            "pick": {"labels": labels},
            "sound_default": ["yes", "no"],
        }
        text = f"Question: {question}\nOptions: {json.dumps(labels)}\nProposed default: 0"
        body = json.dumps({"text": text[:100000], "schema": schema}).encode()
        try:
            for _ in range(2):
                request = urllib.request.Request(
                    f"{endpoint}/classify", data=body,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    verdict = json.loads(response.read())
                if not isinstance(verdict, dict) or verdict.get("pick") != "0" or verdict.get("sound_default") != "yes":
                    return False
            return True
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            logger.warning("auto-answer: classifier unavailable; retaining unanswered clarification")
            return False

    def answer(tool_name: str = "", args=None, next_call=None, session_id: str = "", turn_id: str = "", **_kw):
        if not enabled or tool_name != "clarify" or not isinstance(args, dict):
            return next_call(args) if next_call else None
        from agent.subagent_lifecycle import get_active_subagent_parent
        from tools.clarify_tool import TIMEOUT_RESPONSE

        agent = get_active_subagent_parent()
        origin_turn = turn_id or getattr(agent, "_current_turn_id", "") or ""

        def still_owned():
            return (agent is not None and get_active_subagent_parent() is agent
                    and str(getattr(agent, "session_id", "") or "") == session_id
                    and (getattr(agent, "_current_turn_id", "") or "") == origin_turn
                    and not getattr(agent, "_persist_disabled", False)
                    and not getattr(agent, "_interrupt_requested", False))

        result = next_call(args) if next_call else None
        if not still_owned():
            return result
        try:
            payload = json.loads(result) if isinstance(result, str) else None
        except (ValueError, TypeError):
            return result
        if not isinstance(payload, dict):
            return result
        batch = isinstance(payload.get("responses"), list)
        if batch:
            if payload.get("timed_out") is not True:
                return result
            rows = payload["responses"]
            original = args.get("questions")
            if not isinstance(original, list) or len(original) != len(rows):
                return result
        else:
            if payload.get("user_response") != TIMEOUT_RESPONSE:
                return result
            rows, original = [payload], [args]
        # ponytail: Hermes's batch result cannot distinguish a skipped blank
        # answer from a timed-out blank. Multi-question defaults stay advisory
        # until the host exposes per-question timeout provenance.
        may_apply = not dry_run and (not batch or len(rows) == 1)
        decisions = []
        for index, (row, question_args) in enumerate(zip(rows, original)):
            if not isinstance(row, dict) or not isinstance(question_args, dict):
                continue
            unanswered = row.get("user_response") == ("" if batch else TIMEOUT_RESPONSE)
            question = row.get("question")
            choices = row.get("choices_offered")
            if (not unanswered or question_args.get("multi_select")
                    or not isinstance(question, str) or not question.strip()
                    or str(question_args.get("question") or "").strip() != question
                    or not isinstance(choices, list) or len(choices) < 2
                    or not all(isinstance(option, str) and option.strip() for option in choices)):
                continue
            if _UNSAFE.search(question + " " + " ".join(choices)):
                continue  # Risk/approval questions always remain human-owned.
            if classify(question, choices):
                decisions.append({
                    "qid": f"q{index}", "question": question, "selected": choices[0],
                    "decided_by": "classifier", "applied": may_apply,
                })
        if not decisions or not still_owned():
            return result
        # Preserve every authoritative response, including explicit human choices.
        return json.dumps({**payload, "automatic_answers": decisions,
                           "automatic_answer_note": "Derived defaults, not human approvals. Apply only entries with applied=true. Unapplied entries are suggestions; original human answers and explicit skips remain authoritative."}, ensure_ascii=False)

    ctx.register_middleware("tool_execution", answer)
