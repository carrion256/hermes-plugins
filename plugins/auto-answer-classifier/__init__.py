"""Workspace conventions require an observed human choice AND native consent to remember it.

Only exact approved conventional questions bypass clarify. Explicit JSON evidence
is data, hash-bound, redacted and advisory until a genuine timeout. Native human
answers/skips always win; legacy ambiguous batch defaults are never applied.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import time
import urllib.request

_UNSAFE = re.compile(r"\b(approv\w*|authoriz\w*|permission|consent|secret\w*|credential\w*|"
                     r"delet\w*|destroy\w*|deploy\w*|production|publish\w*|payment|purchase|"
                     r"product|feature|customer|business|architecture|security)\b", re.I)
_CONVENTIONAL = re.compile(r"\b(indentation|indent|formatting|naming convention|line endings)\b", re.I)
_LIMIT = 64
_FILE_LIMIT = 16384
logger = logging.getLogger(__name__)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _workspace(task_id):
    """No process-CWD fallback: only explicit native task/session context."""
    try:
        from tools.terminal_tool import get_session_cwd, _get_env_config
        from agent.runtime_cwd import resolve_context_cwd
        if _get_env_config().get("env_type") != "local":
            return ""
        cwd = (get_session_cwd(task_id) if task_id else None) or resolve_context_cwd()
        return str(Path(cwd).resolve(strict=True)) if cwd and Path(cwd).is_dir() else ""
    except Exception:
        return ""


def _owner(owner_session=None, owner_turn=None):
    from agent.subagent_lifecycle import get_active_subagent_parent
    agent = get_active_subagent_parent()
    if (agent is None or getattr(agent, "_persist_disabled", False)
            or getattr(agent, "_interrupt_requested", False)):
        return None
    session = str(getattr(agent, "session_id", "") or "")
    turn = str(getattr(agent, "_current_turn_id", "") or "")
    if not session or (owner_session and owner_session != session) or (owner_turn and owner_turn != turn):
        return None
    return agent, session, turn



_CLASSIFY_TIMEOUT = 2.5
# Client-side wait budget, the same curve the sibling classifier clients use: the
# engine measured 0.10 s at 200 chars, 0.24 s at 2k, 1.67 s at 10k, 5.64 s at 40k,
# and the evidence proofs make up most of this payload. Small payloads keep the
# configured floor; _MAX_TIMEOUT is the guard rail.
_CHAR_SECONDS = 0.00025
_MAX_TIMEOUT = 30.0


def _budget(text, floor=_CLASSIFY_TIMEOUT):
    """Seconds to wait for this payload: the configured timeout is the floor, the
    text's measured inference cost is the budget, _MAX_TIMEOUT is the ceiling."""
    return min(_MAX_TIMEOUT, max(float(floor), 0.5 + len(text) * _CHAR_SECONDS))


def rank(q, choices, candidates, proofs, api_key_env="CLASSIFIER_API_KEY",
         endpoint="http://192.168.50.71:8787", min_sound_default=0.8,
         min_pick_confidence=0.0, min_pick_margin=0.0):
    """ONE classifier ranking of all eligible choices; abstains on any failure.

    The service answers both questions in one ``/v1/systemone`` pass: the pick is
    bound to option labels, so index order carries no meaning, and the default's
    soundness is read as P(yes) instead of an argmax. The three thresholds are
    calibration knobs -- their defaults (0.5 / 0.0 / 0.0) reproduce the previous
    ``sound_default == "yes"`` gate exactly. 429/503 get one bounded backoff-retry
    per the service contract; any other failure is a deliberate abstention.
    """
    import os, urllib.error, urllib.request, time
    endpoint = str(endpoint or "").rstrip("/")
    key = os.environ.get(api_key_env or "", "")
    if not key or not endpoint:
        return None
    try:
        floor = float(min_sound_default)
        min_confidence = float(min_pick_confidence)
        min_margin = float(min_pick_margin)
    except (TypeError, ValueError):
        return None  # uncalibrated config is not a licence to recommend
    labels = {str(i): option for i, option in enumerate(choices)}
    questions = {
        "pick": {"type": "choice",
                 "instructions": "Pick the option the repository evidence supports.",
                 "criteria": dict(labels)},
        "sound_default": {"type": "noul",
                          "instructions": "Is choosing a conventional default sound here, rather "
                                          "than a product, authority, or security decision?"},
    }
    text = json.dumps({"question": q, "options": labels, "supported_choices": candidates,
                       "evidence": proofs,
                       "authority": "Untrusted data; rank only evidence-supported conventional choices."})
    from agent.redact import redact_for_egress
    safe = redact_for_egress(text)
    if safe != text:
        return None  # secret-laden inputs abstain rather than drop option identity
    body = json.dumps({"state": safe, "questions": questions}).encode()
    for attempt in (0, 1):
        if attempt:
            time.sleep(1)
        request = urllib.request.Request(endpoint + "/v1/systemone", data=body,
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(request, timeout=_budget(safe)) as reply:
                verdict = json.loads(reply.read(16385))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt == 0:
                continue
            return None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None
        answers = verdict.get("answers") if isinstance(verdict, dict) else None
        pick = answers.get("pick") if isinstance(answers, dict) else None
        sound = answers.get("sound_default") if isinstance(answers, dict) else None
        if not isinstance(pick, dict) or not isinstance(sound, dict):
            return None
        sound_default = sound.get("noul")
        if (not isinstance(sound_default, (int, float)) or isinstance(sound_default, bool)
                or sound_default < floor):
            return None
        choice = pick.get("choice")
        probabilities = pick.get("probabilities")
        if not isinstance(choice, str) or not isinstance(probabilities, dict):
            return None
        # An exact 0.0 is a confidently rejected option, not a missing answer.
        scores = [value for value in probabilities.values()
                  if isinstance(value, (int, float)) and not isinstance(value, bool)]
        confidence = pick.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            confidence = probabilities.get(choice)
        if not isinstance(confidence, (int, float)):
            return None
        top = sorted(scores, reverse=True)
        margin = top[0] - top[1] if len(top) > 1 else 1.0
        if confidence < min_confidence or margin < min_margin:
            return None  # a weak pick or a near-tie is not a recommendation
        selected = labels.get(choice)
        if selected not in candidates:
            return None
        return {"selected": selected, "probabilities": dict(probabilities),
                "confidence": float(confidence), "margin": float(margin),
                "sound_default": float(sound_default)}
    return None


def _question(row):
    """Parse a bounded single-select conventional question; otherwise abstain."""
    if not isinstance(row, dict) or row.get("multi_select", False) is not False:
        return None
    question, choices = row.get("question"), row.get("choices")
    if (not isinstance(question, str) or not question.strip() or len(question) > 1024
            or not isinstance(choices, list) or not 2 <= len(choices) <= 12
            or any(not isinstance(choice, str) or not choice.strip() or len(choice) > 256
                   for choice in choices)):
        return None
    if len({choice.strip().casefold() for choice in choices}) != len(choices):
        return None
    if not _CONVENTIONAL.search(question) or _UNSAFE.search(question + " " + " ".join(choices)):
        return None
    return question, choices


_EVIDENCE_KEYS = {
    # Actual .editorconfig domains. Style values are tab/space; the plural
    # display forms live on the CHOICE side, never the config side.
    "indent_style": {"tab", "space"},
    "indent_size": None,          # positive integer or ASCII decimal string
    "tab_width": None,
    "end_of_line": {"lf", "crlf", "cr"},
}
_DISPLAY_ALIASES = {  # choice-side canonicalization: display plural -> canonical value
    "tabs": "tab", "spaces": "space",
}
_EVIDENCE_LIMIT = 32
_RANK_PROOFS = 16


def _identity_token(text):
    return text.strip().strip("\"'").lower().replace("-", "").replace("_", "")


def _valid_domain_value(key, value):
    """Validate parsed values before any display-choice alias normalization."""
    allowed = _EVIDENCE_KEYS[key]
    if allowed is not None:
        return isinstance(value, str) and value.lower() in allowed
    if type(value) is int:  # bool is not an integer configuration value.
        return value > 0
    return (isinstance(value, str) and re.fullmatch(r"[0-9]+", value) is not None
            and any(digit != "0" for digit in value))


def _supported_values(document, text, name):
    """Extract unambiguous repo-wide conventional values; data only.

    Conservative by design: only repo-wide ([*]) .editorconfig keys and
    top-level TOML/JSON scalars qualify. Conditional sections, overrides,
    repeated keys, unknown values and nested tables are excluded — this is
    deliberately not a full parser.
    """
    values = []
    lowered = name.lower()
    if lowered == ".editorconfig":
        current = None
        seen = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                current = "repo-wide" if line.strip() == "[*]" else "conditional"
            elif current in ("repo-wide", "conditional") and "=" in line and "#" not in line:
                key, _, value = line.partition("=")
                key = key.strip().lower()
                if key in _EVIDENCE_KEYS:
                    if current != "repo-wide" or key in seen:
                        seen[key] = None  # conditional/override/duplicate: ambiguous
                    else:
                        value = value.strip()
                        seen[key] = value if _valid_domain_value(key, value) else None
        return [(key, value, "repo-wide") for key, value in list(seen.items())[:_EVIDENCE_LIMIT]
                if value is not None]
    if lowered.endswith(".toml"):
        try:
            import tomllib
            document = tomllib.loads(text)
        except Exception:
            return []
    if isinstance(document, dict):
        # Top-level scalars only: nested tables/objects are never evidence.
        for key, value in document.items():
            key = str(key).lower()
            if key in _EVIDENCE_KEYS and _valid_domain_value(key, value):
                values.append((key, value, "repo-wide"))
                if len(values) >= _EVIDENCE_LIMIT:
                    break
    return values
def _evidence(workspace, references, question, choices):
    """Read explicit hash-bound repository data; never discover files or execute prose.

    Supports ordinary .editorconfig/TOML/JSON configs and explicit
    clarification_defaults JSON. The reference hash is optional: when absent
    it is fixed at read time and re-verified afterwards (freshness), so
    existing repo files work without inventing sidecar hashes.

    Walk directory descriptors with NOFOLLOW so a concurrent symlink
    replacement cannot escape the workspace between containment checks and
    the bounded read.
    """
    from agent.file_safety import get_read_block_error
    from agent.redact import redact_for_egress

    if (not isinstance(workspace, str) or not Path(workspace).is_absolute()
            or not isinstance(references, list) or not 1 <= len(references) <= 4
            or _question({"question": question, "choices": choices}) is None):
        raise ValueError("bounded conventional question and explicit workspace evidence required")
    root = Path(workspace).resolve(strict=True)
    proofs, candidates, source = [], set(), []
    for reference in references:
        if not isinstance(reference, dict) or set(reference) - {"path", "sha256", "source"}:
            raise ValueError("evidence keys limited to path/sha256/source")
        name, expected = reference.get("path"), reference.get("sha256")
        if (not isinstance(name, str) or not name.strip() or len(name) > 1024 or "\0" in name
                or (expected is not None
                    and (not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)))):
            raise ValueError("invalid evidence reference")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("evidence path must remain relative to the workspace")
        target = root / relative
        target.resolve(strict=True).relative_to(root)
        if get_read_block_error(str(target)):
            raise ValueError("native secret-file guard refused evidence")
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in relative.parts[:-1]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(relative.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                                 dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > _FILE_LIMIT:
                    raise ValueError("evidence must be a bounded regular file")
                data = stream.read(_FILE_LIMIT + 1)
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError("evidence changed during read")
        finally:
            os.close(directory)
        actual = hashlib.sha256(data).hexdigest()
        if len(data) > _FILE_LIMIT or (expected is not None and actual != expected):
            raise ValueError("evidence oversized or stale")
        text = data.decode("utf-8")
        if redact_for_egress(text) != text:
            raise ValueError("secret-bearing evidence cannot establish a default")
        try:
            document = json.loads(text)
        except ValueError:
            document = None
        if isinstance(document, dict) and set(document) == {"clarification_defaults"}:
            rows = document["clarification_defaults"]
            if not isinstance(rows, list) or not 1 <= len(rows) <= _LIMIT:
                raise ValueError("evidence requires bounded convention rows")
            for row in rows:
                if (not isinstance(row, dict) or set(row) != {"question", "choice"}
                        or not isinstance(row["question"], str) or not 0 < len(row["question"]) <= 1024
                        or not isinstance(row["choice"], str) or not 0 < len(row["choice"]) <= 256):
                    raise ValueError("malformed convention evidence")
                if row["question"] == question and row["choice"] in choices:
                    candidates.add(row["choice"])
        else:
            lowered = question.lower()
            # Evidence key domain comes from the question, not any key value:
            # line-ending choices read only end_of_line; tab-width phrasing
            # reads only tab_width; numeric options read only indent_size;
            # everything conventional-and-unambiguous reads only indent_style.
            if ("newline" in lowered or "line ending" in lowered
                    or "end_of_line" in lowered):
                topic_keys = ("end_of_line",)
            elif "tab width" in lowered or "tab_width" in lowered:
                topic_keys = ("tab_width",)
            elif "indent_style" in lowered or "indent style" in lowered:
                topic_keys = ("indent_style",)
            elif ("indent" in lowered
                  and any(_valid_domain_value("indent_size", choice.strip()) for choice in choices)):
                topic_keys = ("indent_size",)
            elif "indent" in lowered or "formatting" in lowered:
                topic_keys = ("indent_style",)
            else:
                raise ValueError("question has no config-key mapping")
            for key, value, scope in _supported_values(document, text, relative.name)[:_LIMIT]:
                if key not in topic_keys:
                    continue
                normalized = str(value).lower()
                for choice in choices:
                    if key in ("indent_size", "tab_width"):
                        # Positive ASCII decimal choices only; preserve exact
                        # digits (04 is not 4), never erase signs or punctuation.
                        choice_token = choice.strip()
                        if not _valid_domain_value(key, choice_token):
                            continue
                    else:
                        choice_token = _identity_token(choice)
                        choice_token = _DISPLAY_ALIASES.get(choice_token, choice_token)
                    if choice_token == normalized:
                        candidates.add(choice)
                        source.append({"path": relative.as_posix(), "key": key,
                                       "value": value, "scope": scope})
        if expected is None:
            expected = actual
        proofs.append({"path": relative.as_posix(), "sha256": expected,
                       "source": source[:_RANK_PROOFS]})
    if not candidates:
        raise ValueError("no exact question/choice evidence")
    return proofs, sorted(candidates)


def register(ctx):
    def load(key):
        value = ctx.state.get(key)
        return value if isinstance(value, dict) else {}

    def save(key, rows):
        ctx.state.set(key, dict(list(rows.items())[-_LIMIT:]))

    def scope(owner, workspace, task_id):
        return {"session_id": owner[1], "turn_id": owner[2], "task_id": task_id, "workspace": workspace}

    def current(owner, workspace, task_id):
        return _owner(owner[1], owner[2]) == owner and _workspace(task_id) == workspace

    def observe(tool_name="", args=None, result=None, task_id="", tool_call_id="",
                session_id="", turn_id="", **_kw):
        if tool_name != "clarify" or not tool_call_id:
            return
        owner, workspace = _owner(session_id, turn_id), _workspace(task_id)
        if not owner or not workspace or not isinstance(args, dict):
            return
        try:
            payload = json.loads(result) if isinstance(result, str) else result
            if not isinstance(payload, dict):
                return
            batch = isinstance(payload.get("responses"), list)
            rows = payload["responses"] if batch else [payload]
            original = args.get("questions") if batch else [args]
            if not isinstance(original, list) or len(rows) != len(original) or len(rows) > 12:
                return
            from tools.clarify_tool import TIMEOUT_RESPONSE
            observations = load("observations_v2")
            for i, (row, request) in enumerate(zip(rows, original)):
                eligible = _question(request)
                if not eligible or not isinstance(row, dict):
                    continue
                q, choices = eligible
                answer = row.get("user_response")
                if (row.get("response_status") not in (None, "answered") or answer == TIMEOUT_RESPONSE
                        or not isinstance(answer, str) or answer not in choices or row.get("question") != q
                        or row.get("choices_offered") != choices):
                    continue
                proof = {**scope(owner, workspace, task_id), "tool_call_id": tool_call_id,
                         "qid": f"q{i}", "question": q, "choice": answer, "created_at": time.time()}
                observations[_digest([owner[1], owner[2], tool_call_id, f"q{i}"])] = proof
            if current(owner, workspace, task_id):
                save("observations_v2", observations)
        except (ValueError, TypeError, OSError):
            return

    def defaults(args, task_id="", **_kw):
        key_env = ctx.get_config("api_key_env", "CLASSIFIER_API_KEY")
        endpoint = ctx.get_config("endpoint", "http://192.168.50.71:8787")
        owner, workspace = _owner(), _workspace(task_id)
        if not isinstance(args, dict) or not owner or not workspace:
            return json.dumps({"status": "unknown", "reason": "native owner/workspace unavailable"})
        identity = scope(owner, workspace, task_id)
        def reply(**data):
            return json.dumps({**identity, **data}, ensure_ascii=False)
        try:
            action = args.get("action", "list")
            conventions = load("conventions_v2")
            if action == "list":
                observations = [v for v in load("observations_v2").values()
                                if all(v.get(k) == val for k, val in identity.items()) and 0 <= time.time() - v.get("created_at", 0) <= 900]
                return reply(status="ok", defaults=[v for v in conventions.values() if v.get("workspace") == workspace],
                             observations=observations)
            if action == "record":
                proof_id = _digest([owner[1], owner[2], args.get("tool_call_id"), args.get("qid")])
                proof = load("observations_v2").get(proof_id)
                if (not isinstance(proof, dict) or any(proof.get(k) != val for k, val in identity.items())
                        or not 0 <= time.time() - proof.get("created_at", 0) <= 900
                        or args.get("question") != proof["question"] or args.get("choice") != proof["choice"]):
                    return reply(status="refused", recorded=False, reason="missing, expired, or mismatched native observation")
                from tools.clarify_tool import clarify_tool
                callback = getattr(owner[0], "clarify_callback", None)
                if not callable(callback):
                    return reply(status="refused", recorded=False, reason="native human clarification channel unavailable")
                permission = "Remember this convention"
                prompt = f"Remember this exact workspace convention for future questions? Workspace: {workspace}\nQuestion: {proof['question']}\nChoice: {proof['choice']}"
                raw = clarify_tool(question=prompt, choices=["Do not remember", permission], callback=callback)
                consent = json.loads(raw)
                if (not current(owner, workspace, task_id) or consent.get("user_response") != permission
                        or consent.get("response_status") not in (None, "answered")):
                    return reply(status="refused", recorded=False, reason="no current native human consent")
                record = {"workspace": workspace, "question": proof["question"], "choice": proof["choice"],
                          "provenance": {**proof, "source": "native_human_remember_consent", "consent_at": time.time()}}
                conventions[_digest([workspace, proof["question"]])] = record
                save("conventions_v2", conventions)
                if not current(owner, workspace, task_id):
                    # A write may block; don't leave consent bound to a replaced owner.
                    conventions.pop(_digest([workspace, proof["question"]]), None)
                    save("conventions_v2", conventions)
                    return reply(status="refused", recorded=False, reason="owner changed during persistence")
                return reply(status="ok", recorded=True, convention=record)
            if action == "recommend":
                eligible = _question(args)
                if not eligible:
                    return reply(status="abstained", reason="not a bounded low-risk conventional question")
                q, choices = eligible
                proofs, candidates = _evidence(workspace, args.get("evidence"), q, choices)
                # Positional like key_env/endpoint: the thresholds are rank()'s next
                # parameters, and every rank() stand-in in the tests takes *args.
                observed = rank(q, choices, candidates, proofs, key_env, endpoint,
                                ctx.get_config("min_sound_default", 0.8),
                                ctx.get_config("min_pick_confidence", 0.0),
                                ctx.get_config("min_pick_margin", 0.0))
                _evidence(workspace, proofs, q, choices)  # freshness after the blocking ranking
                # The ranking is advisory: only evidence-supported choices may
                # ever be recommended, even if a ranker misbehaves.
                selected = observed.get("selected") if isinstance(observed, dict) else observed
                if selected not in candidates or not current(owner, workspace, task_id):
                    return reply(status="abstained", reason="unknown ranking or owner changed")
                recommendation = {**identity, "question": q, "choices": choices, "selected": selected,
                                  "evidence": proofs, "created_at": time.time()}
                # Recorded so the thresholds can be calibrated from real traffic.
                ranking = ({"probabilities": observed.get("probabilities"),
                            "confidence": observed.get("confidence"),
                            "margin": observed.get("margin"),
                            "sound_default": observed.get("sound_default")}
                           if isinstance(observed, dict) else None)
                if ranking:
                    recommendation["ranking"] = ranking
                pending = load("recommendations_v2")
                pending[_digest([identity, q])] = recommendation
                save("recommendations_v2", pending)
                return reply(status="recommended", selected=selected, option_id=_digest(selected),
                             evidence=proofs, applied=False,
                             source="repository_evidence_single_ranking",
                             **({"ranking": ranking} if ranking else {}))
            return reply(status="refused", reason="unknown action")
        except Exception:
            return reply(status="abstained", recorded=False, reason="evidence, redaction, classifier or state unavailable/stale")

    def answer(tool_name="", args=None, next_call=None, session_id="", turn_id="", task_id="", **_kw):
        if tool_name != "clarify" or not isinstance(args, dict) or not ctx.get_config("enabled", True):
            return next_call(args) if next_call else None
        owner, workspace = _owner(session_id, turn_id), _workspace(task_id)
        if not owner or not workspace:
            return next_call(args) if next_call else None
        batch = isinstance(args.get("questions"), list) and bool(args["questions"])
        questions = args["questions"] if batch else [args]
        if len(questions) > 12:
            return next_call(args) if next_call else None
        dry = bool(ctx.get_config("dry_run", False))
        def decision(i, q, selected, provenance, applied):
            return {"qid": f"q{i}", "question": q, "selected": selected, "option_id": _digest(selected),
                    "decided_by": "approved_convention" if provenance.get("source") == "native_human_remember_consent" else "classifier",
                    "provenance": provenance, "applied": applied}
        try:
            conventions = load("conventions_v2")
            upfront = []
            for i, request in enumerate(questions):
                eligible = _question(request)
                if not eligible:
                    break
                q, choices = eligible
                record = conventions.get(_digest([workspace, q]), {})
                if (record.get("workspace") != workspace or record.get("choice") not in choices
                        or record.get("provenance", {}).get("source") != "native_human_remember_consent"):
                    break
                upfront.append(decision(i, q, record["choice"], record["provenance"], not dry))
            if len(upfront) == len(questions) and not dry and current(owner, workspace, task_id):
                return json.dumps({"responses": [{"qid": f"q{i}", "question": r["question"], "choices_offered": r["choices"],
                                                 "response_status": "automatic_default"} for i, r in enumerate(questions)],
                                   "automatic_answers": upfront, "automatic_answer_note": "Previously human-approved conventions, not new human answers."})
        except Exception as exc:
            # Native clarify remains available when plugin state fails; the
            # failure must still be diagnosable.
            logger.warning("auto-answer convention replay failed: %s: %s",
                           type(exc).__name__, exc, exc_info=True)
        result = next_call(args) if next_call else None
        if not current(owner, workspace, task_id):
            return result
        try:
            payload = json.loads(result) if isinstance(result, str) else None
            if not isinstance(payload, dict):
                return result
            rows = payload.get("responses") if batch else [payload]
            if not isinstance(rows, list) or len(rows) != len(questions):
                return result
            pending = load("recommendations_v2")
            decisions = []
            from tools.clarify_tool import TIMEOUT_RESPONSE
            for i, (row, request) in enumerate(zip(rows, questions)):
                eligible = _question(request)
                if not eligible or not isinstance(row, dict):
                    continue
                q, choices = eligible
                status = row.get("response_status")
                if (row.get("question") != q or row.get("choices_offered") != choices
                        or status in ("answered", "skipped") or row.get("user_response") not in ("", TIMEOUT_RESPONSE)):
                    continue
                timeout = status == "timed_out" or (not batch and row.get("user_response") == TIMEOUT_RESPONSE)
                ambiguous = batch and status is None and payload.get("timed_out") is True
                if not timeout and not ambiguous:
                    continue
                recommendation = pending.get(_digest([scope(owner, workspace, task_id), q]))
                if not recommendation or recommendation.get("choices") != choices or not 0 <= time.time() - recommendation.get("created_at", 0) <= 900:
                    continue
                proofs, candidates = _evidence(workspace, recommendation["evidence"], q, choices)
                if recommendation["selected"] not in candidates:
                    continue
                decisions.append(decision(i, q, recommendation["selected"], {"source": "repository_evidence_single_ranking", "evidence": proofs}, timeout and not dry))
            if decisions and current(owner, workspace, task_id):
                return json.dumps({**payload, "automatic_answers": decisions,
                                   "automatic_answer_note": "Derived defaults only; original human answers/skips remain authoritative."})
        except Exception as exc:
            # Never apply derived defaults from broken state; say why not.
            logger.warning("auto-answer timeout default skipped: %s: %s",
                           type(exc).__name__, exc, exc_info=True)
        return result

    ctx.register_hook("post_tool_call", observe)
    ctx.register_middleware("tool_execution", answer)
    ctx.register_tool(name="clarification_defaults", toolset="auto-answer-classifier",
                      schema={"name": "clarification_defaults", "description": "List native observations/conventions, request human consent to remember an observed answer, or recommend from explicit hash-bound JSON evidence.",
                              "parameters": {"type": "object", "properties": {
                                  "action": {"enum": ["list", "record", "recommend"]},
                                  "tool_call_id": {"type": "string"}, "qid": {"type": "string"},
                                  "question": {"type": "string"}, "choice": {"type": "string"},
                                  "choices": {"type": "array", "items": {"type": "string"}},
                                  "evidence": {"type": "array", "items": {"type": "object", "properties": {
                                      "path": {"type": "string"},
                                      "sha256": {"type": "string",
                                                 "description": "Optional: when absent, fixed at read time and re-verified."}},
                                      "required": ["path"]}}},
                                  "required": ["action"], "additionalProperties": False}}, handler=defaults)
