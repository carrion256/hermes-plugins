"""Shared GLiNER2.5-Decide `/classify` client with a dead-endpoint cooldown.

The Bearer key is read from the environment at call time and never stored,
logged or echoed. Failure semantics: no key / no endpoint / transport error /
unusable body -> None, and the endpoint is blacked out for `cooldown` seconds
so a dead service does not tax every tool call.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)
MAX_TEXT = 100000  # service limit; longer texts must be clipped by the caller


class Classifier:
    def __init__(self, ctx, timeout=2.5, cooldown=45.0):
        self._ctx = ctx
        self.timeout = timeout
        self.cooldown = cooldown
        self._down = {}  # endpoint -> monotonic deadline

    def endpoint(self):
        return str(self._ctx.get_config("endpoint", "http://192.168.50.149:8787") or "").rstrip("/")

    def _key(self):
        return os.environ.get(str(self._ctx.get_config("api_key_env", "CLASSIFIER_API_KEY")), "").strip()

    def classify(self, text, schema, timeout=None):
        """POST {text, schema}; returns the parsed label dict or None (never raises)."""
        endpoint, key = self.endpoint(), self._key()
        if not endpoint or not key or self._down.get(endpoint, 0.0) > time.monotonic():
            return None
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
            logger.warning("meta-classifier: text outside service limits (%s chars)",
                           len(text) if isinstance(text, str) else "n/a")
            return None
        request = urllib.request.Request(
            endpoint + "/classify",
            data=json.dumps({"text": text, "schema": schema}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as reply:
                data = json.loads(reply.read(65536))
            if isinstance(data, dict):
                return data
            logger.warning("meta-classifier: unusable classifier body type %s", type(data).__name__)
        except Exception as exc:
            logger.warning("meta-classifier: classifier unavailable: %s: %s", type(exc).__name__, exc)
        self._down[endpoint] = time.monotonic() + self.cooldown
        return None
