"""Optional classifier tie-break for ambiguous keyword hits.

The only semantic seam in keyword-steering: asked solely when a hit is a common
word or negated. The Bearer key is read from the environment per call and never
stored or logged. Any failure returns None (the caller declines the hit) and
blacks the endpoint out for `cooldown` seconds.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)

LABELS = ("directive", "mention", "negated")

STEERING_SCHEMA = {"steering": {"labels": {
    "directive": "The keyword is the user's instruction to the assistant for this turn: they want the named mode applied now.",
    "mention": "The keyword is used as an ordinary word, a name, or in reported/past speech (e.g. 'the CI workflow is failing', 'last time I said ultrathink'); it is not an instruction for this turn.",
    "negated": "The user explicitly says NOT to apply the keyword/mode this turn (e.g. \"don't ultrathink this\", 'no need to orchestrate').",
}}}


class Tiebreak:
    def __init__(self, ctx):
        cfg = ctx.get_config("classifier")
        if not isinstance(cfg, dict):
            cfg = {}
        enabled = cfg.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("keyword-steering: classifier.enabled must be a boolean")
        self.enabled = enabled
        self._endpoint = str(cfg.get("endpoint") or "http://192.168.50.71:8787").rstrip("/")
        self._key_env = str(cfg.get("api_key_env") or "CLASSIFIER_API_KEY")
        self._timeout = float(cfg.get("timeout", 1.5))
        self._cooldown = float(cfg.get("cooldown", 45.0))
        self._down_until = 0.0
        self._last_error: str | None = None

    def _fail(self, message: str) -> None:
        self._last_error = message
        self._down_until = time.monotonic() + self._cooldown

    def judge(self, trigger: str, prose: str) -> str | None:
        try:
            if not self.enabled:
                return None
            key = os.environ.get(self._key_env, "").strip()
            if not key or self._down_until > time.monotonic():
                return None
            try:
                from agent.redact import REDACTION_UNAVAILABLE, redact_for_egress

                redacted = redact_for_egress(prose)
                if not isinstance(redacted, str) or REDACTION_UNAVAILABLE in redacted:
                    redacted = None
            except Exception:
                redacted = None
            if redacted is None:
                self._last_error = "redaction unavailable"
                return None
            request = urllib.request.Request(
                self._endpoint + "/classify",
                data=json.dumps({
                    "text": f"Keyword: {trigger}\nMessage: {redacted}",
                    "schema": STEERING_SCHEMA,
                }).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as reply:
                    body = json.loads(reply.read(65536))
                label = body.get("steering") if isinstance(body, dict) else None
                if label not in LABELS:
                    raise ValueError("unusable classifier body")
            except Exception as exc:
                self._fail(f"{type(exc).__name__}: {str(exc).replace(key, '***')}")
                return None
            self._last_error = None
            return label
        except Exception as exc:  # judge must never raise
            self._last_error = type(exc).__name__
            return None

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "endpoint": self._endpoint if self.enabled else None,
            "last_error": self._last_error,
        }
