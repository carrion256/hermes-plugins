"""Shared classifier client with a dead-endpoint cooldown.

One transport (`/v1/systemone`), one failure contract. `ask()` sends typed
questions and returns the raw envelope with probabilities; `classify()` keeps
the historical label-schema API by translating each head into one typed choice
question (criteria descriptions carry the old label descriptions verbatim),
scoring it in a single request, and back-mapping each answer to the original
label key. `multi_label` heads cannot be answered jointly by a logprob scorer:
they are asked as a choice among their labels plus an explicit none and
answered as a list of at most one label — the shape every caller tolerates.

The endpoint is a flare node (127.0.0.1:8102) which REQUIRES a top-level
`model` field; the Bearer key is still read from the environment at call time
and never stored, logged or echoed (nodes ignore it; the key gate keeps the
tools registered). Failure semantics unchanged: no key / no endpoint /
transport error / unusable body -> None, and the endpoint is blacked out for
`cooldown` seconds so a dead service does not tax every tool call.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)
MAX_TEXT = 100000  # service limit; longer texts must be clipped by the caller
DEFAULT_MODEL = "qwen3.8-flash-next"  # what the flare nodes serve

# Client-side wait budget. Measured on the deployed engine (5 samples per size, worst
# observed): 0.10 s at 200 chars, 0.24 s at 2k, 1.67 s at 10k, 5.64 s at 40k, 12.8 s at
# 90k -- about 0.17 ms/char plus fixed overhead, with a ~2x tail from queueing behind the
# service's serialised inference lane. A flat timeout big enough for a large payload would
# stall every ordinary tool call behind it, and the flat 2.5 s this replaced silently
# blacked out the shield above ~25k chars. So small calls keep the configured floor and
# large ones get a budget that survives the tail. An explicit timeout= overrides this.
CHAR_SECONDS = 0.00025   # worst observed ~0.17 ms/char, plus tail headroom
MAX_TIMEOUT = 30.0       # guard rail; MAX_TEXT caps the real budget near 25 s


class Classifier:
    def __init__(self, ctx, timeout=2.5, cooldown=45.0):
        self._ctx = ctx
        self.timeout = timeout
        self.cooldown = cooldown
        self._down = {}  # endpoint -> monotonic deadline

    def endpoint(self):
        return str(self._ctx.get_config("endpoint", "http://127.0.0.1:8102") or "").rstrip("/")

    def _model(self):
        return str(self._ctx.get_config("served_model", DEFAULT_MODEL) or DEFAULT_MODEL)

    def _key(self):
        return os.environ.get(str(self._ctx.get_config("api_key_env", "CLASSIFIER_API_KEY")), "").strip()

    def _budget(self, text):
        """Seconds to wait for this payload: the configured timeout is the floor, the
        text's measured inference cost is the budget, MAX_TIMEOUT is the ceiling."""
        return min(MAX_TIMEOUT, max(self.timeout, 0.5 + len(text) * CHAR_SECONDS))

    def classify(self, text, schema, timeout=None):
        """Translate a label schema to typed questions, score them in ONE
        /v1/systemone pass, back-map each answer to its original label key;
        returns the parsed label dict or None (never raises).

        Each head `{labels: {key: description}}` becomes one choice question
        whose criteria carry the descriptions verbatim; the answer key is
        validated against the offered set. `multi_label` heads also see a
        `none` option and answer as a list of at most one label. A head with
        no usable answer yields no key — the shape /classify produced when a
        head abstained.
        """
        if not isinstance(schema, dict) or not schema:
            return None
        questions, multiheads = {}, {}
        for head, spec in schema.items():
            if isinstance(spec, dict) and "labels" in spec:
                labels = spec["labels"]
                multi = bool(spec.get("multi_label"))
            elif isinstance(spec, list):
                labels, multi = {str(k): str(k) for k in spec}, False
            else:
                return None
            if isinstance(labels, list):  # bare label lists carry no descriptions
                labels = {str(k): str(k) for k in labels}
            if not isinstance(labels, dict) or not labels:
                return None
            criteria = {str(k): str(labels.get(k, k)) for k in labels}
            if multi:
                criteria["none"] = "none of the listed labels applies"
                multiheads[head] = True
            questions[head] = {"type": "choice", "criteria": criteria}
        envelope = self.ask(text, questions, timeout=timeout)
        answers = envelope.get("answers") if isinstance(envelope, dict) else None
        if not isinstance(answers, dict):
            return None
        labels_out = {}
        for head in questions:
            answer = answers.get(head)
            choice = answer.get("choice") if isinstance(answer, dict) else None
            key = str(choice) if choice is not None else None
            if key is None or key not in questions[head]["criteria"]:
                continue  # head unanswered or off-menu: absent key, never a guess
            if multiheads.get(head):
                labels_out[head] = [] if key == "none" else [key]
            else:
                labels_out[head] = key
        return labels_out or None

    def ask(self, state, questions, timeout=None):
        """POST {model, state, questions} to /v1/systemone; returns the parsed envelope
        (`{"answers": {...}}`) or None (never raises)."""
        return self._post("/v1/systemone", {"model": self._model(),
                                            "state": state, "questions": questions},
                         state, timeout)

    def _post(self, path, payload, text, timeout=None):
        endpoint, key = self.endpoint(), self._key()
        if not endpoint or not key or self._down.get(endpoint, 0.0) > time.monotonic():
            return None
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
            logger.warning("meta-classifier: text outside service limits (%s chars)",
                           len(text) if isinstance(text, str) else "n/a")
            return None
        request = urllib.request.Request(
            endpoint + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._budget(text)) as reply:
                data = json.loads(reply.read(65536))
            if isinstance(data, dict):
                return data
            logger.warning("meta-classifier: unusable classifier body type %s", type(data).__name__)
        except Exception as exc:
            logger.warning("meta-classifier: classifier unavailable: %s: %s", type(exc).__name__, exc)
        self._down[endpoint] = time.monotonic() + self.cooldown
        return None
