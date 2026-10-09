"""Evidence-based completion — pre_verify stop-gate consumer + outcome labeler.

Replaces the old regex/empty-steer continuation (native fallbacks for
trailing-intent and empty responses live in agent/turn_final_response.py;
this plugin must not duplicate them). When the agent stops after code
edits, ``pre_verify`` holds the turn until the completion contract holds:

1. remaining-task criteria: the native todo ledger (``agent._todo_store``)
   has no pending/in_progress items; an unreadable ledger is an explicit
   unverified blocker, never treated as finished;
2. fresh native verification: the verification snapshots
   (``agent.verification_evidence.verification_status``) for the agent's
   actual workspace (resolved via the native
   ``tools.coding_context.resolve_context_cwd``) are ``passed`` — ledger
   disabled, unavailable, unverified, stale, failed, or a lookup
   exception are explicit unverified blockers, never inferred clean;
3. an independent native /review (``agent.review_engine.start_review``)
   completed with a canonical JSON verdict strictly parsed from the
   native result summary (identical shape on the async delegation rail
   and the synchronous channel), bound to the exact reviewed evidence
   snapshot. Missing/malformed/contradictory verdicts are unverified;
   findings travel verbatim and persist until the reviewed state
   changes.

Every hold records ``blockers`` in session state. The turn is only
released silently on verified finish; budget exhaustion or dispatch
failure release with a blocked/unverified label appended to the final
response via ``transform_llm_output``. Pending-review bindings survive
run boundaries (on_session_end) so the next turn picks the delegation
up; session reset/finalize and user stop clear state. Directives are
same-turn native hook directives — no ``ctx.inject_message`` and no
child/gateway routing.

Native agent access: ``agent.subagent_lifecycle.get_active_subagent_parent``
(contextvar bound around run_conversation by turn_facade; plugin hook
dispatch preserves context via copy_context). Detached forks and
platform=subagent contexts are refused; the agent's session_id must
equal the hook's session_id.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ACTIVE_TODOS = ("pending", "in_progress")
_RUNNING_DELEGATION = ("running", "finalizing")
_VERDICTS = ("clean", "findings", "unverified")
_STATE_PREFIX = "session:"
_NOTE_MARK = "Completion unverified:"

_PENDING_MARK = "independent completion verification still pending (unverified)"

_REVIEW_FOCUS = (
    "Independent completion review of the referenced change. Your final "
    "message must end with exactly one canonical JSON object and nothing "
    'after it: {"review_verdict": "clean", "findings": []}. Permitted '
    'verdicts: "clean", "findings", or "unverified". The findings field '
    "is REQUIRED: clean verdicts carry an empty findings list; findings "
    "verdicts carry >=1 non-empty string findings; unverified carries an "
    "empty or string findings list. No trailing text after that object; "
    "a missing, malformed, or incomplete canonical object is treated as "
    "unverified."
)


def _identity_token(task_id: str, turn_id: str, user_message: str) -> str:
    """Genuine task identity. pre_llm_call fires once per real user turn, so
    turn_id is the primary identity (terminal task_id is often 'default'
    across every turn); the user-message digest is the fallback. Internal
    pre_verify rounds never invoke pre_llm_call and never change identity."""
    turn_id = str(turn_id or "").strip()
    if turn_id:
        return "turn:" + turn_id
    message = str(user_message or "").strip()
    if message:
        return "msg:" + hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
    return "task:" + str(task_id or "")


def register(ctx) -> None:
    max_review_rounds = max(1, int(ctx.get_config("max_review_rounds", 2) or 2))
    _session_lock = threading.Lock()
    _owners: Dict[str, str] = {}
    _memory_blockers: Dict[str, Any] = {}

    def _session_key(session_id: str) -> str:
        return _STATE_PREFIX + session_id

    def _session_record(session_id: str) -> Dict[str, Any]:
        try:
            record = ctx.state.get(_session_key(session_id))
        except Exception:
            # A failing durable store is reported through the in-memory
            # marker; reads degrade to empty but never crash the hook.
            logger.debug("continuation: session record read failed", exc_info=True)
            return {}
        return record if isinstance(record, dict) else {}

    def _save(session_id: str, record: Dict[str, Any], owner: str = "") -> None:
        # Ownership check, durable write and in-memory marker mirror are ONE
        # transaction against capture/_clear ownership transitions: a genuine
        # new turn either fences this writer outright or cleanly overwrites
        # it — no stale record, marker or blocker can interleave.
        blockers = record.get("blockers") or []
        with _session_lock:
            if owner and _owners.get(session_id) != owner:
                logger.info("continuation: stale callback state write ignored for session %s",
                            session_id)
                raise ValueError("stale callback: a newer turn owns the session gate")
            try:
                ctx.state.set(_session_key(session_id), record)
            finally:
                # The marker mirrors the gate outcome in memory: if the durable
                # store fails (quota/read-only/corrupt), transform_llm_output
                # still refuses to present an unlabelled completion.
                if blockers:
                    _memory_blockers[session_id] = (owner, _blockers_text(blockers))
                else:
                    _memory_blockers.pop(session_id, None)

    def _clear_session(**kwargs) -> None:
        session_id = str(kwargs.get("session_id") or kwargs.get("session_key") or "")
        if session_id:
            with _session_lock:
                _owners.pop(session_id, None)
                _memory_blockers.pop(session_id, None)
            try:
                ctx.state.set(_session_key(session_id), None)
            except Exception:
                logger.debug("continuation: session state clear failed", exc_info=True)

    def _active_agent(session_id: str):
        try:
            from agent.subagent_lifecycle import get_active_subagent_parent

            agent = get_active_subagent_parent()
        except Exception:
            logger.debug("continuation: parent-agent lookup failed", exc_info=True)
            return None
        if agent is None:
            return None
        # Detached forks and subagent contexts must never route the gate.
        if getattr(agent, "_persist_disabled", False) or str(getattr(agent, "platform", "") or "") == "subagent":
            return None
        if str(getattr(agent, "session_id", "") or "") != session_id:
            return None
        return agent

    def _blockers_text(blockers: List[str]) -> str:
        return "; ".join(str(b) for b in blockers[:4])

    def _todos_state(agent) -> tuple[Optional[list], Optional[str]]:
        """(pending, blocker). pending=None means no usable ledger; blocker
        set when the native todo ledger is unavailable (unverified, not
        finished)."""
        store = getattr(agent, "_todo_store", None)
        if store is None or not hasattr(store, "read"):
            return None, "todo ledger unavailable (unverified)"
        try:
            items = store.read()
        except Exception:
            logger.debug("continuation: todo read failed", exc_info=True)
            return None, "todo ledger unreadable (unverified)"
        pending = [i for i in items if str(i.get("status", "")) in _ACTIVE_TODOS]
        return pending or None, None

    def _snapshots(agent, changed_paths: list, session_id: str) -> Dict[str, Dict[str, Any]]:
        """Native verification snapshot for the actual workspace. Relative
        changed paths must never resolve against the plugin process cwd:
        candidate cwds come from the native resolve_context_cwd, then from
        absolute host-supplied changed paths; anything else is unavailable."""
        cwds: List[str] = []
        try:
            from agent.runtime_cwd import resolve_context_cwd

            configured = resolve_context_cwd()
        except Exception:
            logger.debug("continuation: workspace resolution failed", exc_info=True)
            configured = None
        if configured:
            cwds.append(str(configured))
        cwds.extend(dict.fromkeys(str(Path(p).parent) for p in changed_paths
                                  if p and os.path.isabs(str(p))))
        cwds = list(dict.fromkeys(cwds))
        if not cwds:
            return {"workspace": {"status": "unverified", "error": "workspace_unavailable"}}
        try:
            from agent.verification_evidence import verification_status
        except Exception:
            return {cwd: {"status": "ledger_unavailable"} for cwd in cwds}
        snaps: Dict[str, Dict[str, Any]] = {}
        for cwd in cwds:
            try:
                result = verification_status(session_id=session_id, cwd=cwd)
            except Exception:
                logger.debug("continuation: verification snapshot failed for %s", cwd, exc_info=True)
                snaps[cwd] = {"status": "unverified", "error": "lookup_failed"}
                continue
            evidence = result.get("evidence") or {}
            snaps[cwd] = {
                "status": str(result.get("status") or ""),
                "evidence": ({k: evidence.get(k) for k in
                              ("id", "created_at", "canonical_command", "kind", "status", "exit_code")}
                             if evidence else None),
                "changed_paths": sorted(str(p) for p in (result.get("changed_paths") or [])),
            }
        return snaps

    def _gaps(snaps: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
        return {cwd: snap.get("status", "") for cwd, snap in snaps.items()
                if snap.get("status") != "passed"}

    def _parse_verdict(text: str) -> Optional[Dict[str, Any]]:
        """Strict canonical-contract parse. The canonical object must be the
        FINAL content of the summary (raw_decode consumes to end): a
        nested/quoted mention or trailing prose cannot inject a clean
        verdict. clean requires an empty findings list; findings requires
        >=1 actionable item; anything else -> None (unverified)."""
        if not isinstance(text, str) or not text.strip():
            return None
        decoder = json.JSONDecoder()
        obj = None
        idx = text.find("{")
        while idx >= 0 and obj is None:
            try:
                candidate, end = decoder.raw_decode(text[idx:])
            except ValueError:
                idx = text.find("{", idx + 1)
                continue
            if not text[idx + end:].strip():
                obj = candidate  # object decodes to the exact end of the summary
                break
            idx = text.find("{", idx + 1)
        if obj is None:
            return None  # no complete object anchored at the summary end
        if not isinstance(obj, dict) or obj.get("review_verdict") not in _VERDICTS:
            return None
        if "findings" not in obj:
            return None  # the canonical object must carry the findings field
        raw_findings = obj["findings"]
        if not isinstance(raw_findings, list):
            return None
        for entry in raw_findings:
            if not isinstance(entry, str) or not entry.strip():
                return None  # strict: all findings entries are non-empty strings
        findings = [entry.strip() for entry in raw_findings]
        verdict = obj["review_verdict"]
        if verdict == "clean" and findings:
            return None  # clean carries an empty findings list, never items
        if verdict == "findings" and not findings:
            return None
        return {"verdict": str(verdict), "findings": findings[:20]}

    def _review_summary(result: Dict[str, Any]) -> str:
        """Native delegate result -> reviewer summary text, identical shape
        for the sync channel and the async delegation record."""
        results = result.get("results")
        if isinstance(results, list) and results and isinstance(results[0], dict):
            return str(results[0].get("summary") or "")
        return str(result.get("summary") or "")

    def _dispatch_review(agent, changed_paths: list, session_id: str, record: Dict[str, Any],
                         snaps: Dict[str, Dict[str, Any]], final_response: str = "",
                         owner: str = ""):
        """Dispatch native /review on the actual captured task context
        (pre_llm_call excerpt + candidate response + native ledgers), bound
        to the exact verification snapshot and task identity. Returns a
        directive dict, or None only for a synchronous clean verdict."""
        task = record.get("task") or {}
        messages: List[Dict[str, str]] = []
        for msg in task.get("excerpt") or []:
            if isinstance(msg, dict) and msg.get("role") in ("user", "assistant") and msg.get("text"):
                messages.append({"role": str(msg["role"]), "content": str(msg["text"])})
        ledger = "; ".join(
            f"{cwd}: evidence={(snap.get('evidence') or {}).get('canonical_command') or 'none'}"
            f" status={snap.get('status')}" for cwd, snap in snaps.items())
        messages.append({"role": "user", "content": (
            "[continuation-gate partial capture] The live transcript is not replayable from the agent; "
            "this is the actual user task, the pre-LLM conversation, the current candidate response and "
            "the current native ledgers provided by the completion gate, not a full message history. "
            f"NATIVE LEDGERS: changed={', '.join(changed_paths[:8])}; {ledger}."
        )})
        messages.append({"role": "assistant",
                         "content": "[CANDIDATE RESPONSE]\n" + str(final_response or "").strip()})
        # Pending state is established BEFORE native dispatch: start_review may
        # block synchronously (pool full / async unsupported) and the host bounds
        # plugin callbacks with a timeout; an abandoned callback's later writes
        # are fenced by the owner token, and a timeout leaves this blocker.
        record = dict(record, rounds=record.get("rounds", 0) + 1)
        record["review"] = {"snapshot": snaps,
                            "identity": str(task.get("identity") or ""),
                            "paths": list(changed_paths)}
        record["blockers"] = list(record.get("findings") or []) + [
            f"independent review dispatching (round {record['rounds']}/{max_review_rounds}); "
            + _PENDING_MARK]
        _save(session_id, record, owner)
        try:
            from agent.review_engine import start_review

            focus = _REVIEW_FOCUS + " Changed: " + ", ".join(changed_paths[:8])
            result = start_review(agent, messages, focus)
        except Exception as exc:
            logger.info("continuation: review dispatch refused/failed: %s", exc)
            record["blockers"] = list(record.get("findings") or []) + [
                f"independent review dispatch failed (unverified): {exc}"]
            record.pop("review", None)
            _save(session_id, record, owner)
            return {"action": "continue",
                    "message": ("Independent review could not be dispatched: " + _blockers_text(record["blockers"])
                                + ". Completion cannot be certified; resolve and retry.")}
        delegation_id = str(result.get("delegation_id") or "")
        if delegation_id:
            record["review"]["delegation_id"] = delegation_id
            record["blockers"] = list(record.get("findings") or []) + [
                f"independent review dispatched (round {record['rounds']}/{max_review_rounds}); verdict pending"]
            _save(session_id, record, owner)
            logger.info("continuation: review round %d dispatched for session %s",
                        record["rounds"], session_id)
            directive = {"action": "continue",
                         "message": (f"Independent review dispatched (round {record['rounds']}/{max_review_rounds}). "
                                     "Its structured verdict gates completion: address any findings, keep "
                                     "verification evidence fresh, then finish.")}
            carried = record.get("findings") or []
            if carried:
                directive["message"] += (" Still outstanding: " + _blockers_text(carried)
                                         + ".")
            return directive
        # Synchronous channel: same native result structure as the async record.
        verdict = _parse_verdict(_review_summary(result))
        if verdict and verdict["verdict"] == "clean":
            record["review"]["verdict"] = "clean"
            record.pop("blockers", None)
            record.pop("findings", None)  # clean renewal releases carried findings
            _save(session_id, record, owner)  # durable clean binding; clears markers
            logger.info("continuation: synchronous clean review; session %s verified", session_id)
            return None
        if verdict and verdict["verdict"] == "findings":
            record["findings"] = blockers = verdict["findings"]
        else:
            blockers = ["independent review returned no canonical clean verdict (unverified)"]
        record["blockers"] = blockers
        record.pop("review", None)
        _save(session_id, record, owner)
        return {"action": "continue",
                "message": ("Independent review verdict is not clean: " + _blockers_text(blockers)
                            + ". Address these, re-verify, and finish only after a clean review "
                              "binds the fresh evidence.")}

    def pre_verify(session_id: str = "", coding: bool = False, attempt: int = 0,
                   changed_paths=None, final_response: str = "", **_rest):
        if not coding or not session_id:
            return None  # non-coding / unknown-session turns pass through
        owner = ""
        try:
            agent = _active_agent(session_id)
            if agent is None:
                return None
            # Turn ownership: each callback invocation claims the session gate;
            # a timed-out (abandoned) callback's writes are fenced out. The
            # provisional marker is planted BEFORE any I/O so host timeout or
            # a failing durable store cannot yield an unlabeled completion.
            owner = uuid.uuid4().hex
            with _session_lock:
                _owners[session_id] = owner
                _memory_blockers[session_id] = (owner, _PENDING_MARK)
            record = _session_record(session_id)
            paths = sorted({str(p) for p in (changed_paths or []) if p})

            # 1. remaining-task criteria (native todo ledger).
            pending, todo_blocker = _todos_state(agent)
            if todo_blocker:
                record["blockers"] = list(record.get("findings") or []) + [todo_blocker]
                _save(session_id, record, owner)
                return {"action": "continue",
                        "message": (todo_blocker + ". Task criteria cannot be confirmed; "
                                    "completion cannot be certified.")}
            if pending:
                ids = ", ".join(f"{i.get('id', '?')}({str(i.get('content', ''))[:60]})"
                                for i in pending[:8])
                record["blockers"] = list(record.get("findings") or []) + [f"unfinished tasks: {ids}"]
                _save(session_id, record, owner)
                return {"action": "continue",
                        "message": (f"Task list still has {len(pending)} unfinished item(s): {ids}. "
                                    "Finish them (or cancel explicitly) before ending the turn.")}

            # 2. fresh native verification for the agent's workspace.
            snaps = _snapshots(agent, paths, session_id)
            gaps = _gaps(snaps)
            if gaps:
                blockers = list(record.get("findings") or []) + [
                    f"{cwd}: verification {status}" for cwd, status in gaps.items()]
                record["blockers"] = blockers
                _save(session_id, record, owner)
                return {"action": "continue",
                        "message": ("Code edits lack fresh verification (" + _blockers_text(blockers)
                                    + "). Run the project's verification/tests now; completion cannot "
                                      "be claimed without a passing, fresh evidence ledger result.")}

            # 3. independent native /review with structured verdict binding.
            task = record.get("task") or {}
            identity = str(task.get("identity") or _identity_token(
                str(task.get("task_id") or ""), str(task.get("turn_id") or ""),
                str(task.get("user_message") or "")))
            if not task.get("excerpt") and not task.get("user_message"):
                record["blockers"] = list(record.get("findings") or []) + [
                    "actual task capture unavailable (unverified)"]
                _save(session_id, record, owner)
                return {"action": "continue",
                        "message": ("No captured user task for this session; the agent's live transcript "
                                    "cannot be replayed, and an independent review cannot run on real "
                                    "context. Completion cannot be certified.")}
            review = record.get("review") or {}
            delegation_id = str(review.get("delegation_id") or "")
            if delegation_id:
                try:
                    from tools.async_delegation import get_durable_delegation

                    delegation = get_durable_delegation(delegation_id)
                except Exception:
                    logger.debug("continuation: delegation lookup failed", exc_info=True)
                    record["blockers"] = list(record.get("findings") or []) + [
                        "review record lookup failed (unverified)"]
                    _save(session_id, record, owner)
                    return {"action": "continue",
                            "message": "Review record lookup failed; completion cannot be certified. Retry."}
                if delegation is not None and str(delegation.get("state") or "") in _RUNNING_DELEGATION:
                    record["blockers"] = list(record.get("findings") or []) + [
                        "independent review still running (pending)"]
                    _save(session_id, record, owner)
                    return {"action": "continue",
                            "message": ("Independent review of this change is still running; its findings "
                                        "arrive in this conversation. Wait for the structured verdict "
                                        "before finishing.")}
                if delegation is None or str(delegation.get("state") or "") != "completed":
                    record["review"] = {}  # lost/failed: not a clean review; cycle continues
                else:
                    if (review.get("snapshot") != snaps
                            or review.get("identity") != identity
                            or not set(paths) <= set(review.get("paths") or [])):
                        # Edits (even same-path) moved the evidence, the genuine user turn
                        # changed, or files outside the reviewed set were touched: reviewed
                        # state invalidated.
                        record["review"] = {}
                    else:
                        verdict = _parse_verdict(_review_summary(delegation.get("result") or {}))
                        if verdict is None:
                            record["review"] = {}
                            record["blockers"] = list(record.get("findings") or []) + [
                                "review verdict missing or malformed (unverified)"]
                            _save(session_id, record, owner)
                            return {"action": "continue",
                                    "message": ("The independent review returned no parseable canonical "
                                                "verdict; completion stays unverified. Address the reviewed "
                                                "change, re-verify, and finish after a clean review.")}
                        if verdict["verdict"] == "clean":
                            record.pop("blockers", None)
                            record.pop("findings", None)  # clean renewal on renewed evidence
                            _save(session_id, record, owner)
                            return None  # verified finish on THIS evidence snapshot
                        # findings / unverified: carried verbatim until the reviewed state changes
                        carried = (verdict["findings"] if verdict["verdict"] == "findings"
                                   else ["reviewer reported the change unverified"])
                        record["findings"] = carried  # persists until a clean renewed review
                        record["blockers"] = carried
                        _save(session_id, record, owner)
                        return {"action": "continue",
                                "message": ("Independent review returned actionable findings: "
                                            + _blockers_text(carried)
                                            + ". Fix them and finish only after re-verification and a "
                                              "clean review on the new evidence.")}

            if (review.get("verdict") == "clean" and review.get("snapshot") == snaps
                    and review.get("identity") == identity
                    and set(paths) <= set(review.get("paths") or [])):
                record.pop("blockers", None)  # synchronous clean binding still current
                record.pop("findings", None)
                _save(session_id, record, owner)
                return None

            rounds = int(record.get("rounds", 0))
            if rounds >= max_review_rounds:
                # Fresh derivation only: carried findings + honest cap outcome.
                # Verification gaps were already reported by their own branch;
                # never relabel fresh state with stale blockers.
                record["blockers"] = list(record.get("findings") or []) + [
                    f"review/fix budget exhausted after {rounds} round(s); "
                    "no completed clean review on the current evidence (unverified)"]
                _save(session_id, record, owner)
                logger.info("continuation: review budget exhausted for session %s; releasing labeled",
                            session_id)
                return None  # release; transform_llm_output labels the outcome blocked/unverified

            return _dispatch_review(agent, paths, session_id, record, snaps,
                                    str(final_response or ""), owner)
        except Exception as exc:
            logger.debug("continuation: pre_verify failed", exc_info=True)
            try:
                record = _session_record(session_id)
                record["blockers"] = [f"completion gate failed open: {type(exc).__name__} (unverified)"]
                _save(session_id, record, owner)
            except Exception:
                logger.debug("continuation: blocker label persistence failed", exc_info=True)
            return None  # host budget decides; label marks the outcome unverified

    def transform_llm_output(response_text: str = "", session_id: str = "", **_rest):
        """Label the outcome honestly: unresolved blockers are appended to the
        final response so a capped, timed-out, or failed release is never
        silent. The session+turn-owned in-memory marker is consulted BEFORE
        the durable store, and lock acquisition is NON-BLOCKING: a stalled
        store holding the _save transaction must never delay or silence this
        label — lock contention itself is reported as pending/unverified
        immediately, never awaited and never routed through durable state."""
        if not session_id or not str(response_text or "").strip():
            return None
        if not _session_lock.acquire(blocking=False):
            marker = _memory_blockers.get(session_id)
            blockers_text = marker[1] if marker else _PENDING_MARK
        else:
            try:
                marker = _memory_blockers.get(session_id)
                current_owner = _owners.get(session_id)
            finally:
                _session_lock.release()
            blockers_text = ""
            if marker:
                if marker[0] == current_owner:
                    blockers_text = marker[1]
            if not blockers_text:
                try:
                    blockers_text = _blockers_text(
                        _session_record(session_id).get("blockers") or [])
                except Exception:
                    logger.debug("continuation: blocker lookup failed; leaving unlabeled",
                                 exc_info=True)
                    return None
        if not blockers_text:
            return None
        note = f"\n\n{_NOTE_MARK} {blockers_text}. This outcome is blocked/unverified, not complete."
        return str(response_text) + note

    def pre_llm_call(session_id: str = "", task_id: str = "", turn_id: str = "",
                     user_message="", conversation_history=None, is_first_turn: bool = False,
                     **_rest):
        """Observer for the actual pre_llm_call producer. Captures the
        authoritative task identity and pre-LLM conversation: agent message
        state is empty during the run, so this hook is the transcript source.
        Returns None: injects nothing into the user message."""
        if not session_id:
            return None
        if _active_agent(session_id) is None:
            return None
        try:
            record = _session_record(session_id)
            prev_identity = str((record.get("task") or {}).get("identity") or "")
            owner = uuid.uuid4().hex
            with _session_lock:
                _owners[session_id] = owner
                # Genuine new user turn: the previous turn's provisional marker
                # must never label this one's output.
                _memory_blockers.pop(session_id, None)
            excerpt: List[Dict[str, str]] = []
            try:
                from agent.review_engine import snapshot_recent_messages

                excerpt = snapshot_recent_messages(list(conversation_history or []))
            except Exception:
                logger.debug("continuation: review excerpt snapshot failed", exc_info=True)
            identity = _identity_token(task_id, turn_id, user_message)
            genuine_turn = bool(prev_identity) and prev_identity != identity
            record["task"] = {
                "task_id": str(task_id or ""),
                "turn_id": str(turn_id or ""),
                "identity": identity,
                "user_message": str(user_message or "")[:4000],
                "excerpt": excerpt,
                "is_first_turn": bool(is_first_turn),
            }
            if genuine_turn or str((record.get("review") or {}).get("identity") or "") != identity:
                # New genuine user turn (producer fires once per user turn;
                # internal pre_verify rounds never re-capture): the previous
                # review/findings never carry over.
                record["review"] = {}
                record.pop("findings", None)
                record.pop("blockers", None)
                if genuine_turn:
                    # Review budget is per genuine user turn.
                    record["rounds"] = 0
            _save(session_id, record, owner)
        except Exception:
            logger.debug("continuation: pre_llm_call capture failed", exc_info=True)
        return None

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("pre_verify", pre_verify)
    ctx.register_hook("transform_llm_output", transform_llm_output)
    # on_session_end deliberately NOT registered: a reviewer still pending at
    # run boundary must stay bound; reset/finalize/stop bound the NEW identity.
    ctx.register_hook("on_session_reset", _clear_session)
    ctx.register_hook("on_session_finalize", _clear_session)
    ctx.register_hook("agent_loop_stopped", _clear_session)
    logger.info("continuation: evidence-gated completion active (max %d review rounds/session)",
                max_review_rounds)
