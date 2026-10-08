"""Auto-answer classifier — port of dsh-auto-answer-classifier.

When a clarify question times out, the classifier (two independent passes,
must agree) judges whether the recommended option (first choice, Hermes
clarify convention) is a sound default. Only on unanimous "yes" do we inject
the pick as a user message; disagreement or any failure leaves the question
open (fail-open).

Seam note: expiring-question semantics are platform-side; the plugin acts on
the post_tool_call result (timed_out flag) for clarify calls.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

SCHEMA = {"sound_default": ["yes", "no"]}


def register(ctx) -> None:
    endpoint = (ctx.get_config("endpoint", "http://192.168.50.149:8787") or "").rstrip("/")
    api_key_env = ctx.get_config("api_key_env", "CLASSIFIER_API_KEY")

    def sound_default(question: str, option: str) -> bool | None:
        """One pass; True/False verdict or None on failure (fail-open = don't act)."""
        key = os.environ.get(api_key_env or "", "")
        if not key or not endpoint:
            return None
        text = f"Question: {question}\nProposed default answer: {option}\nIs this a sound default for an unattended agent?"
        body = json.dumps({"text": text[:100000], "schema": SCHEMA}).encode()
        req = urllib.request.Request(
            f"{endpoint}/classify", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                v = json.loads(resp.read()).get("sound_default")
            return v == "yes" if v in ("yes", "no") else None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            return None

    def on_clarify_result(tool_name: str = "", args=None, result=None,
                          session_id: str = "", **_kw):
        if tool_name != "clarify" or not isinstance(args, dict):
            return
        try:
            payload = json.loads(result) if isinstance(result, str) else (result or {})
        except (ValueError, TypeError):
            return
        if not payload.get("timed_out"):
            return
        for q in args.get("questions") or []:
            if not isinstance(q, dict):
                continue
            text = str(q.get("question") or "")
            choices = [c for c in (q.get("choices") or []) if isinstance(c, str)]
            if not text or not choices:
                continue
            pick = choices[0]  # Hermes clarify convention: recommended option first
            # Two independent passes must agree (Winnow-2 style).
            p1, p2 = sound_default(text, pick), sound_default(text, pick)
            if p1 is True and p2 is True:
                logger.info("auto-answer: classifier endorsed default for %r -> %r", text[:50], pick)
                try:
                    ctx.inject_message(
                        f"(auto-answer) The clarify question timed out; classifier endorsed default: {pick}. "
                        f"Proceeding with that unless told otherwise.",
                        session_key=session_id or None)
                except Exception:
                    logger.debug("auto-answer: inject failed", exc_info=True)
            else:
                logger.info("auto-answer: no unanimous endorsement for %r (passes %r/%r) — leaving open",
                            text[:50], p1, p2)

    ctx.register_hook("post_tool_call", on_clarify_result)
    logger.info("auto-answer-classifier: active")
