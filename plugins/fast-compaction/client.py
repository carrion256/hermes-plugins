"""GLiNER2.5-Decide /classify transport for fast-compaction.

Mirrors the meta-classifier client contract: Bearer key from the environment at call
time, never stored or logged; a dead endpoint is blacked out for ``cooldown`` seconds so
a down service never taxes every compaction. Returns the parsed label dict or None.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)
MAX_TEXT = 100_000  # service limit on the text field


class ClassifierClient:
    def __init__(self, timeout=20.0, cooldown=45.0):
        self.timeout = float(timeout)
        self.cooldown = float(cooldown)
        self._down = {}  # endpoint -> monotonic deadline; set only on failures

    def post(self, endpoint, api_key_env, text, schema, timeout=None):
        """POST {text, schema} to <endpoint>/classify; returns dict or None (never raises)."""
        endpoint = str(endpoint or "").rstrip("/")
        key = os.environ.get(str(api_key_env or "CLASSIFIER_API_KEY"), "").strip()
        if not endpoint or not key or self._down.get(endpoint, 0.0) > time.monotonic():
            return None
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
            logger.warning("fast-compaction: state outside service limits (%s chars)",
                           len(text) if isinstance(text, str) else "n/a")
            return None
        request = urllib.request.Request(
            endpoint + "/classify",
            data=json.dumps({"text": text, "schema": schema}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as reply:
                data = json.loads(reply.read(262144))
            if isinstance(data, dict):
                return data
            logger.warning("fast-compaction: unusable classifier body type %s", type(data).__name__)
        except Exception as exc:
            logger.warning("fast-compaction: classifier unavailable: %s: %s", type(exc).__name__, exc)
        self._down[endpoint] = time.monotonic() + self.cooldown
        return None
