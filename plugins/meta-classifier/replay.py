"""Replay droid's captured classifier decisions against the configured service.

Python port of droid's `tooling/classifier-replay.mjs`, merged with this
plugin: the `text` of each payload is exactly what droid sent its own
classifier (captured fixtures); the `schema` is the plugin's runtime schema
for every decision the plugin implements (risk, shield, signals), so a replay
exercises the same label sets the hooks use. Jev payloads convert the captured
decision request itself; the router (not implemented by the plugin) is built
from its fixtures only.

    hermes meta-classifier-replay            # dry run: print every payload
    hermes meta-classifier-replay --run      # POST each payload, print replies
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import schemas

DEFAULT_CAPTURES = "/var/lib/hermes/projects/droid/notes/captured/classifiers"
_ROUTER_BAND = {
    "viable": "0.7–1.0: the model will likely or very likely complete the task on its first attempt.",
    "marginal": "0.4–0.6: uncertain; may succeed with errors or rework.",
    "reject": "0.0–0.3: will almost certainly fail, or is unsuitable for this task.",
}


def build_payloads(captures):
    root = Path(captures)
    read = lambda rel: (root / rel).read_text(encoding="utf-8")  # noqa: E731
    out = []
    # 1. Execute risk, Jev shadow classifier: droid's own decision request, converted.
    for name, expect in (("git-status", "low"), ("git-push", "high")):
        request = json.loads(read(f"risk/jev-request-{name}.json"))
        out.append({"id": f"risk-jev/{name}", "expect": {"riskLevel": expect},
                    **schemas.decision_to_classify(request)})
    # 2. Execute risk, luna (chat) shadow classifier: same envelope, plugin tier ladder.
    out.append({"id": "risk-luna/git-push", "expect": {"riskLevel": "high"},
                "text": read("risk/luna-user-envelope-git-push.json"),
                "schema": schemas.risk_schema("luna")})
    # 3. Droid Shield: one payload per captured request; plugin thresholded heads.
    for index, line in enumerate(read("shield/observed-shield-requests.jsonl").strip().splitlines()):
        body = json.loads(line)["body"]
        body = json.loads(body) if isinstance(body, str) else body
        kind = body["model"].removeprefix("shield-")
        out.append({"id": f"shield/{body['model']}-{index}", "text": body["messages"][-1]["content"],
                    "schema": schemas.shield_schema(kind)})
    # 4. Local Signals rubrics: exact rendered transcripts, plugin rubric heads.
    for rubric, schema in (("intent_completeness", schemas.INTENT_SCHEMA),
                           ("friction", schemas.FRICTION_SCHEMA),
                           ("expertise", schemas.EXPERTISE_SCHEMA)):
        out.append({"id": f"signals/{rubric}", "text": read(f"signals/observed/{rubric}-user-content.txt"),
                    "schema": schema})
    # 5. Factory Router: per-candidate score bands from the prompt's own scale (fixture-only).
    candidates = re.findall(r'<model id="([^"]+)">', read("router/system-prompt-1.txt"))
    out.append({"id": "router/session-1", "text": read("router/user-session-1.txt"),
                "schema": {c: {"labels": dict(_ROUTER_BAND)} for c in candidates}})
    return out


def verdict(expect, got):
    if not expect or not isinstance(got, dict):
        return ""
    return " PASS" if all(got.get(head) == label for head, label in expect.items()) else " CHECK"


def run(payloads, classifier, out):
    """POST every payload through the plugin client (30 s inference deadline).
    Returns per-head {agree, total} over payloads that carried an `expect`."""
    agreement = {}
    for payload in payloads:
        got = classifier.classify(payload["text"], payload["schema"], timeout=30)
        status = "ok" if got is not None else "unavailable"
        out(f"{payload['id']} {status}{verdict(payload.get('expect'), got)}\n  "
            f"{json.dumps(got, ensure_ascii=False)[:600]}")
        for head, label in (payload.get("expect") or {}).items():
            cell = agreement.setdefault(head, {"agree": 0, "total": 0})
            cell["total"] += 1
            cell["agree"] += int(isinstance(got, dict) and got.get(head) == label)
    for head, cell in sorted(agreement.items()):
        out(f"agreement {head}: {cell['agree']}/{cell['total']}")
    return agreement


def dry_run(payloads, out):
    for payload in payloads:
        out(json.dumps({"id": payload["id"], "expect": payload.get("expect"),
                        "textChars": len(payload["text"]), "schema": payload["schema"]},
                       ensure_ascii=False))
