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

Explicit acceptance: the `acceptance` tool (declare/bind/status/disposition)
stores requirement declarations and evidence *bindings* in the same session
record — never copied verification rows. Binding validates the explicitly
selected native evidence ID against the host lookup/verification_status
(same session+workspace, canonical check identity, fresh). Terminal checks
observe declared source hashes around the actual check via pre/post_tool_call;
source mutation across or after the check prohibits binding. Blocked/
cancelled/unverified criteria stay visible; cancellation is never completion
proof. The reviewer sees the criteria/evidence/todo mapping and the canonical
verdict must carry an explicit coverage assessment before clean completion;
durable clean bindings invalidate on any requirement, evidence, source or
task-ledger change.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import weakref

logger = logging.getLogger(__name__)

_ACTIVE_TODOS = ("pending", "in_progress")
_RUNNING_DELEGATION = ("running", "finalizing")
_VERDICTS = ("clean", "findings", "unverified")
_STATE_PREFIX = "session:"
# agent/review_engine.py snapshot_recent_messages truncates every message
# at its private _MESSAGE_CHAR_CAP; a mapping above this limit would reach
# the reviewer silently truncated — dispatch is refused instead.
_REVIEW_MSG_CHAR_CAP = 12_000

_NOTE_MARK = "Completion unverified:"
# Bounded evidence-observation history (see register): at most this many
# CALL GROUPS live at once, each group holding one tool call's observations
# for one session (up to the native todo/criterion width). Module-level so
# tests can shrink the bounds; register re-reads them per plugin instance.
_MAX_GROUPS = 64
_MAX_OBS_PER_CALL = 256

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

_ACCEPTANCE_ACTIONS = ("declare", "bind", "status", "disposition")
_CHECK_KINDS = ("terminal", "scenario", "external")
_DISPOSITIONS = ("cancelled", "blocked", "clear")
_COVERAGE_VALUES = ("covered", "gap", "cancelled")

_COVERAGE_FOCUS = (
    " ACCEPTANCE COVERAGE IS PART OF THE CONTRACT: your canonical verdict "
    "object MUST also carry a \"coverage\" field mapping EVERY listed id to "
    "\"covered\", \"gap\", or \"cancelled\". Judge relevance and evidence "
    "per id: an id whose declared evidence is missing, stale, from another "
    "check, or irrelevant to the observable condition is \"gap\", not "
    "\"covered\". Clean verdicts are only valid when every required id is "
    "\"covered\" (or \"cancelled\" where the ledger shows an explicit "
    "cancellation)."
    " Reviewers cannot manufacture proof: an id whose native evidence row is "
    "missing, stale, or irrelevant must be reported as a gap; no verdict text "
    "can substitute for the evidence itself."
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

def _now_iso() -> str:
    """Same clock/string semantics as the native evidence ledger
    (agent/verification_evidence._utc_now): ISO UTC, lexicographically
    comparable — the same comparison the host itself uses for staleness."""
    return datetime.now(timezone.utc).isoformat()


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
            with _tool_obs_lock:
                for gkey in [k for k in _tool_obs_groups if k.startswith(f"{session_id}|")]:
                    _tool_obs_groups.pop(gkey, None)
                for key in [k for k in _evidence_obs if str(k).startswith(f"{session_id}|")]:
                    _evidence_obs.pop(key, None)
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

    # Compaction rotation (agent/conversation_compression.py) reassigns
    # agent.session_id on the SAME object. Track the live agent object ->
    # its last-seen session id, and follow the rotation instead of orphaning
    # the contract on the dead key.
    _agent_session = weakref.WeakKeyDictionary()

    def _migrate_session(session_id: str, agent) -> None:
        """Proven same-agent session rotation: carry declarations and
        dispositions forward, but invalidate evidence and review bindings —
        they must be re-observed / re-run under the current session."""
        if agent is None:
            return
        try:
            origin = _agent_session.get(agent) or session_id
            _agent_session[agent] = session_id
        except TypeError:  # unhashable/unweakrefable agent: no tracking
            return
        if origin == session_id:
            return
        record = _session_record(origin)
        if not record:
            return
        for crit in (record.get("acceptance") or {}).values():
            crit.pop("evidence", None)  # evidence must be re-observed
        record["review"] = {}
        record["blockers"] = [str(b) + " (unverified: observation invalidated by session rotation)"
                              for b in (record.get("blockers") or [])]
        with _tool_obs_lock:
            for gkey in [k for k in _tool_obs_groups if k.startswith(f"{origin}|")]:
                _tool_obs_groups[session_id + gkey[len(origin):]] = _tool_obs_groups.pop(gkey)
            for key in [k for k in _evidence_obs if str(k).startswith(f"{origin}|")]:
                _evidence_obs[session_id + key[len(origin):]] = _evidence_obs.pop(key)
        with _session_lock:
            if origin in _owners:
                _owners[session_id] = _owners.pop(origin)
            if origin in _memory_blockers:
                _memory_blockers[session_id] = _memory_blockers.pop(origin)
        ctx.state.set(_session_key(session_id), record)
        ctx.state.set(_session_key(origin), None)
        logger.info("continuation: session %s -> %s (agent rotation); declarations carried, "
                    "evidence bindings invalidated", origin, session_id)

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

    def _snapshots(agent, changed_paths: list, session_id: str,
                   task_id: str = "") -> Dict[str, Dict[str, Any]]:
        """Native verification snapshot for the actual workspace. Relative
        changed paths must never resolve against the plugin process cwd:
        candidate cwds come from the native TASK cwd record (the terminal
        registry keys on the effective task id), then resolve_context_cwd,
        then absolute host-supplied changed paths; anything else is
        unavailable."""
        cwds: List[str] = []
        task_cwd = ""
        if task_id:
            try:
                from tools.terminal_tool import get_session_cwd
                recorded = get_session_cwd(str(task_id))
                if recorded:
                    task_cwd = str(recorded)
            except Exception:
                logger.debug("continuation: task cwd lookup failed", exc_info=True)
        if task_cwd:
            # The captured task workspace is authoritative: a worktree/cwd
            # override means the configured launch directory is NOT a second
            # workspace to verify — completion would otherwise be blocked by
            # the launch tree's unrelated unverified/stale ledger.
            cwds.append(task_cwd)
        else:
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


    # ------------------------------------------------------------------
    # Explicit acceptance ledger (declare/bind/status/disposition). Stores
    # requirement declarations and evidence BINDINGS only — never copied
    # verification rows and never a second evidence DB.
    # ------------------------------------------------------------------
    # ponytail: terminal-check observations are per-process. A restart
    # simply observes no pre/post pair: the bind then FAILS and the declared
    # check must be rerun — never an implicit current-hash fallback.
    # Bounded history per CALL GROUP, never per row: one call may
    # legitimately carry the full native todo/criterion width (256), so a
    # group holds one call's observations for one session. Eviction removes
    # WHOLE completed groups only — a pending observation of another (or
    # the same) session's in-flight call is never touched, and when all
    # groups are active the new call is explicitly refused (block
    # directive) instead of silently invalidating a peer's running check.
    # The admission bookkeeping runs under _tool_obs_lock; the native
    # pre_tool_call dispatch is already fail-closed on hook timeouts, so a
    # refused call can never execute an unobserved check.
    _tool_obs_lock = threading.RLock()
    # group key -> {"done": bool, "n": int}; n is the live row count.
    _tool_obs_groups: Dict[str, Dict[str, Any]] = {}
    _evidence_obs: Dict[str, Dict[str, Any]] = {}

    def _workspace_root(session_id: str = "", task_id: str = "") -> Optional[str]:
        """Real workspace identity: the native terminal cwd registry is keyed
        by the effective task id (session keys route there), so check the
        native TASK identity first, then the session key (workspace-override /
        Covey-bound values), then the configured override / agent launch cwd.
        Backends whose cwd is not a local path fail closed at path
        validation — criteria can never rebind."""
        try:
            from tools.terminal_tool import get_session_cwd
        except Exception:
            logger.debug("continuation: terminal cwd lookup unavailable", exc_info=True)
            return None
        for identity in (str(task_id or ""), str(session_id or "")):
            if not identity:
                continue
            try:
                recorded = get_session_cwd(identity)
                if recorded:
                    return str(recorded)
            except Exception:
                logger.debug("continuation: session cwd lookup failed for %s",
                             identity, exc_info=True)
        try:
            from agent.runtime_cwd import resolve_agent_cwd, resolve_context_cwd
            configured = resolve_context_cwd()
            if configured:
                return str(configured)
            agent_cwd = resolve_agent_cwd()
            return str(agent_cwd) if agent_cwd else None
        except Exception:
            logger.debug("continuation: workspace resolution failed", exc_info=True)
            return None

    def _rel_source_path(raw: Any) -> Optional[str]:
        s = str(raw or "").strip()
        if not s or os.path.isabs(s) or s.startswith("~") or ".." in Path(s).parts:
            return None
        return s

    def _source_hashes(workspace: str, paths: Any) -> tuple:
        """(hashes, certifiable). Workspace-confinement checked on the
        RESOLVED path (symlinks cannot hop outside), files hashed in streamed
        chunks. Missing/unreadable/escaping sources: certifiable=False — such
        a source can never certify anything."""
        out: Dict[str, Optional[str]] = {}
        certifiable = True
        try:
            root = Path(workspace).resolve()
        except OSError:
            return out, False
        for rel in sorted({str(p) for p in (paths or []) if str(p).strip()}):
            digest = None
            try:
                full = (root / rel).resolve()
                if not full.is_relative_to(root) or not full.is_file():
                    certifiable = False
                else:
                    h = hashlib.sha256()
                    with open(full, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 16), b""):
                            h.update(chunk)
                    digest = h.hexdigest()
            except OSError:
                certifiable = False
            out[rel] = digest
        return out, certifiable

    def _canonical_source_revision(hashes: Dict[str, Optional[str]]) -> str:
        """Canonical source-set binding: sorted workspace-relative path +
        per-file streamed sha256, joined deterministically. Byte-exact
        contract with the external producers (browser-bladebro); any other
        encoding simply fails closed."""
        canon = "".join(f"{rel}\0{hashes[rel] or ''}\n" for rel in sorted(hashes))
        return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


    def _identity_matches(row: Dict[str, Any], identity: str) -> bool:
        """The native canonical name is a classifier label, not the executed
        check: 'pytest', 'pytest tests/test_b.py' and even 'pytest -k smoke'
        (which native may record as full-scope) all canonicalize to
        'pytest' — so a canonical label alone can let a passing selected
        subset certify a declared whole-suite requirement. Terminal rows
        therefore only certify the EXACT raw executed command; only
        external/scenario rows certify their canonical
        'external:{source}:{check_id}' identity."""
        raw = str(row.get("command") or "")
        if raw == identity:
            return True
        return (str(row.get("kind") or "") == "external"
                and str(row.get("canonical_command") or "") == identity)

    def _lookup_evidence(evidence_id: int, session_id: str, workspace: str) -> Dict[str, Any]:
        # The explicit selected native evidence ID is mandatory: hosts
        # without the host-scoped ID lookup yield unbound/unverified —
        # NEVER a latest-evidence fallback.
        try:
            from agent.verification_evidence import verification_evidence_by_id
        except ImportError:
            return {"status": "lookup_unavailable", "evidence": None}
        try:
            return verification_evidence_by_id(
                int(evidence_id), session_id=session_id, cwd=workspace)
        except Exception:
            logger.debug("continuation: evidence lookup failed", exc_info=True)
            return {"status": "lookup_failed", "evidence": None}

    def _terminal_observation(session_id: str, cid: str, evidence_id: int,
                              generation: int = 0) -> Optional[Dict[str, Any]]:
        """The genuine pre/post pair for this evidence: same session, exact
        criterion, the evidence id produced by that very tool call, the
        observation's declaration generation still current (re-declaring the
        criterion invalidates earlier observations), and both snapshots
        certifiable (confined, readable sources). None means the check was
        never observed here — the bind must then fail and the declared check
        be rerun; no current-hash fallback exists."""
        for key, obs in _evidence_obs.items():
            if (str(key).startswith(f"{session_id}|")
                    and obs.get("criterion") == cid
                    and str(obs.get("tool") or "terminal") == "terminal"
                    and obs.get("evidence_id") == evidence_id
                    and int(obs.get("generation") or 0) == int(generation or 0)
                    and obs.get("pre_certifiable") and obs.get("post_certifiable")
                    and obs.get("pre") == obs.get("post")):
                return obs
        return None

    def _evidence_id_from_result(result: Any) -> Optional[int]:
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                return None
        if not isinstance(result, dict):
            return None
        ev = result.get("verification_evidence") or {}
        raw = ev.get("id") if isinstance(ev, dict) else None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _todo_items(agent) -> List[Dict[str, Any]]:
        """Full native todo items ({id, content, status, parent?}) — content
        and structure are part of the reviewable outcome, never just id/status
        tuples: a content-only mutation must invalidate the durable review."""
        store = getattr(agent, "_todo_store", None)
        if store is None or not hasattr(store, "read"):
            return []
        try:
            items = store.read()
        except Exception:
            logger.debug("continuation: todo read failed for ledger view", exc_info=True)
            return []
        return [i for i in items if isinstance(i, dict) and i.get("id") is not None]

    def _todo_rows(agent) -> List[tuple]:
        return [(str(i.get("id")), str(i.get("status"))) for i in _todo_items(agent)]

    def _acceptance_state(record, agent):
        """(criteria, required_ids, cancellable_ids, blocked_lines).
        Required ids derive from EXPLICIT declarations only — a native todo
        alone is never a declared acceptance criterion; its content is still
        passed to the reviewer via the mapping."""
        criteria = record.get("acceptance") or {}
        rows = _todo_rows(agent)
        required = set(criteria)
        cancellable = {cid for cid, c in criteria.items()
                       if str((c.get("disposition") or {}).get("status")) == "cancelled"}
        cancellable |= {tid for tid, st in rows if st == "cancelled"}
        blocked = [f"acceptance criterion {cid} disposition blocked: "
                   f"{(c.get('disposition') or {}).get('reason', '')}"
                   for cid, c in criteria.items()
                   if str((c.get("disposition") or {}).get("status")) == "blocked"]
        return criteria, required, cancellable, blocked

    def _acceptance_digest(record, agent) -> str:
        # Full todo items (id/content/status/parent) — a content-only
        # mutation is a different outcome and must invalidate the review.
        items = sorted((_todo_items(agent)), key=lambda i: str(i.get("id")))
        payload = json.dumps({"criteria": record.get("acceptance") or {},
                              "todos": items},
                             sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _acceptance_mapping_text(criteria, agent, session_id: str = "",
                                 workspace: Optional[str] = None) -> str:
        items = {str(i.get("id")): i for i in _todo_items(agent)}
        parts = []
        for cid, crit in criteria.items():
            ev = crit.get("evidence") or {}
            disp = str((crit.get("disposition") or {}).get("status") or "")
            if disp == "cancelled":
                evidence = "evidence_id=none (cancelled)"
            elif not ev:
                evidence = "evidence_id=none (UNVERIFIED)"
            else:
                current, reason = _binding_current(cid, crit, session_id, workspace)
                evidence = (f"evidence_id={ev.get('evidence_id')}({ev.get('identity')}) VALID"
                            if current else
                            f"evidence_id={ev.get('evidence_id')} INVALID ({reason})")
            parts.append(f"{cid} observable={crit['observable']}; "
                         f"check={crit['check_kind']}:{crit['check_identity']}; "
                         f"{evidence}"
                         + (f"; disposition={disp}" if disp else ""))
        # The native ledger is the authoritative statement of what was asked:
        # every item is passed through, FULL content + structure, for matched
        # ids too — a criterion can describe an easier outcome under the same
        # id, and the reviewer must see the actual native task it covers.
        for tid, item in items.items():
            parent = str(item.get("parent") or "")
            parts.append(f"{tid} NATIVE_TODO outcome={item.get('status')} "
                         f"content={str(item.get('content', ''))!r}"
                         + (f" parent={parent}" if parent else "")
                         + ("" if tid in criteria else " (no declared criterion)"))
        return " | ".join(parts)

    def _binding_current(cid: str, crit: Dict[str, Any], session_id: str,
                         workspace: Optional[str]) -> tuple:
        """(True, '') only while the binding still certifies the criterion:
        workspace unchanged, the native row itself still fresh (re-looked-up
        every certification — never a hash-only shortcut), identity intact,
        and the declared sources unchanged since binding."""
        ev = crit.get("evidence") or {}
        if workspace is None:
            return False, "workspace unavailable (cannot verify)"
        if not ev:
            return False, "no evidence bound"
        if ev.get("workspace") != workspace:
            return False, "workspace identity changed"
        view = _lookup_evidence(ev["evidence_id"], session_id, workspace)
        if view.get("status") != "passed":
            return False, f"native evidence row is {view.get('status')}"
        row = view.get("evidence") or {}
        if str(row.get("canonical_command") or row.get("command") or "") != ev.get("identity"):
            return False, "native evidence row identity changed"
        current, certifiable = _source_hashes(workspace, crit.get("paths"))
        if not certifiable:
            return False, "declared source cannot be hashed (missing/unreadable/escaping)"
        if current != ev.get("source_hashes"):
            return False, "declared source drifted"
        return True, ""

    def _bound_source_drift(record, session_id: str,
                            workspace: Optional[str]) -> List[tuple]:
        """(cid, reason) for every non-cancelled bound criterion whose
        evidence can no longer attest to the current state — including the
        NATIVE ROW going stale (workspace edited after the check) and the
        workspace itself being unavailable. Fail closed; never clean."""
        out = []
        for cid, crit in (record.get("acceptance") or {}).items():
            if str((crit.get("disposition") or {}).get("status")) == "cancelled":
                continue
            if not crit.get("evidence"):
                continue
            ok, reason = _binding_current(cid, crit, session_id, workspace)
            if not ok:
                out.append((cid, reason))
        return out

    def _err(message: str) -> str:
        return json.dumps({"success": False, "error": message})

    def acceptance(args=None, session_id: str = "", **_rest) -> str:
        args = args if isinstance(args, dict) else {}
        action = str(args.get("action") or "").strip().lower()
        if not session_id or action not in _ACCEPTANCE_ACTIONS:
            return _err(f"action must be one of {'|'.join(_ACCEPTANCE_ACTIONS)}")
        _migrate_session(session_id, _active_agent(session_id))
        record = _session_record(session_id)
        criteria = record.get("acceptance") or {}
        workspace = _workspace_root(session_id, str(
            _rest.get("task_id")
            or (record.get("task") or {}).get("task_id") or ""))
        if action == "declare":
            if workspace is None:
                return _err("workspace unavailable; cannot pin the declaration")
            cid = str(args.get("id") or "").strip()
            observable = str(args.get("observable") or "").strip()
            kind = str(args.get("check_kind") or "").strip().lower()
            identity = str(args.get("check_identity") or "").strip()
            if not cid or not observable:
                return _err("id (native todo id where applicable) and observable are required")
            if kind not in _CHECK_KINDS:
                return _err(f"check_kind must be one of {'|'.join(_CHECK_KINDS)}")
            if not identity:
                return _err("check_identity (exact command or scenario) is required")
            paths_raw = args.get("paths")
            if not isinstance(paths_raw, list) or not paths_raw:
                return _err("paths: explicit workspace-relative source paths are required")
            paths, bad = [], []
            for raw in paths_raw:
                rel = _rel_source_path(raw)
                (paths if rel else bad).append(rel or str(raw))
            if bad:
                return _err(f"paths must be workspace-relative, no absolute or .. entries: {bad}")
            if cid in criteria and criteria[cid].get("evidence"):
                return _err(f"criterion {cid} already has evidence bound; cancel it explicitly first")
            previous = criteria.get(cid) or {}
            # Redefining a criterion is a NEW requirement: prior
            # evidence/disposition never carries over, earlier observations
            # for this id are purged, and the generation bumps so no
            # observation from a previous declaration can bind.
            with _tool_obs_lock:
                # The stored criterion field is authoritative: ids may
                # contain '|', and a composite id's delimiter suffix is
                # NOT its id — comparing the suffix would let declaring
                # "case" purge "suite|case"'s genuine evidence.
                for key, obs in list(_evidence_obs.items()):
                    if str(key).startswith(f"{session_id}|") \
                            and obs.get("criterion") == cid:
                        _evidence_obs.pop(key, None)
                # Completed groups with zero remaining rows release their
                # slot; live (pending) groups stay whatever the purge left.
                for gkey in [k for k, g in _tool_obs_groups.items()
                             if g["done"] and k.startswith(f"{session_id}|")]:
                    if not any(x.startswith(gkey + "|") for x in _evidence_obs):
                        _tool_obs_groups.pop(gkey, None)
            criteria[cid] = {"observable": observable, "check_kind": kind,
                             "check_identity": identity, "paths": sorted(set(paths)),
                             "refs": [str(r) for r in (args.get("refs") or [])],
                             # workspace identity pinned at declaration: a
                             # changed cwd can never rebind the criterion.
                             "workspace": workspace,
                             "generation": int(previous.get("generation") or 0) + 1,
                             "declared_at": _now_iso()}
            record["acceptance"] = criteria
            _save(session_id, record)
            return json.dumps({"success": True, "id": cid})
        if action == "bind":
            cid = str(args.get("id") or "").strip()
            crit = criteria.get(cid)
            if crit is None:
                return _err(f"criterion {cid} is not declared")
            if str((crit.get("disposition") or {}).get("status")) == "cancelled":
                return _err(f"criterion {cid} is cancelled; cancellation is not completion")
            try:
                evidence_id = int(args.get("evidence_id"))
            except (TypeError, ValueError):
                return _err("bind requires an explicit native evidence_id (integer)")
            if workspace is None:
                return _err("workspace unavailable; evidence binding cannot be verified")
            # The criterion is pinned to its declaring workspace: a native
            # task-cwd change can never rebind evidence to the new tree —
            # identical relative paths in another workspace cannot certify
            # the original requirement; redeclaration in the current tree is
            # required.
            pinned = str(crit.get("workspace") or "")
            if pinned:
                try:
                    same_ws = Path(pinned).resolve() == Path(workspace).resolve()
                except OSError:
                    same_ws = pinned == workspace
                if not same_ws:
                    return _err(f"criterion {cid} is pinned to workspace {pinned}, "
                                f"but the current workspace is {workspace}; the cwd "
                                "moved — redeclare the criterion in the current "
                                "workspace and rerun the check there (binding rejected)")
            view = _lookup_evidence(evidence_id, session_id, workspace)
            if view.get("status") != "passed":
                return _err(f"evidence {evidence_id} is "
                            f"{view.get('status')} in this session/workspace (binding rejected)")
            row = view.get("evidence") or {}
            if str(row.get("session_id") or "") != session_id:
                return _err("evidence belongs to another session")
            row_cwd = str(row.get("cwd") or "")
            if row_cwd and Path(row_cwd).resolve() != Path(workspace).resolve():
                return _err(f"evidence {evidence_id} was executed in {row_cwd}, "
                            f"not the criterion workspace {workspace}; the root "
                            "may match in a monorepo but the row attests to a "
                            "different directory (binding rejected)")
            if not _identity_matches(row, crit["check_identity"]):
                return _err(f"evidence {evidence_id} check identity "
                            f"{row.get('canonical_command') or row.get('command') or '?'} "
                            f"does not match declared check {crit['check_identity']}")
            if crit.get("check_kind") == "terminal":
                obs = _terminal_observation(session_id, cid, evidence_id,
                                            crit.get("generation") or 0)
                if obs is None:
                    return _err(f"evidence {evidence_id} has no genuine observed check "
                                "execution (confined pre/post source snapshots) in this "
                                "session for this criterion; rerun the declared check and "
                                "bind the evidence id that check produced")
                exec_cwd = str(obs.get("exec_cwd") or "")
                if exec_cwd and Path(exec_cwd).resolve() != Path(workspace).resolve():
                    return _err(f"the observed check executed in {exec_cwd}, not the "
                                f"criterion workspace {workspace}; rerun the check "
                                "in the declared workspace")
                obs_identity = str(obs.get("identity") or "")
                current_identity = str((record.get("task") or {}).get("identity") or "")
                if obs_identity and current_identity and obs_identity != current_identity:
                    return _err(f"evidence {evidence_id} was observed under a different "
                                "task identity; rerun under the current task")
                snapshot = obs["post"]
            else:
                # scenario/external: browser_capture is the check. The bind
                # REQUIRES a current, complete, owned capture observation —
                # timestamps alone cannot establish that the row came from
                # the current task/call, so an absent or stale observation is
                # denial, never permission.
                obs = None
                for k, o in _evidence_obs.items():
                    if (str(k).startswith(f"{session_id}|")
                            and o.get("criterion") == cid
                            and o.get("evidence_id") == evidence_id
                            and str(o.get("tool") or "") == "browser_capture"
                            and int(o.get("generation") or 0)
                            == int(crit.get("generation") or 0)):
                        obs = o
                if obs is None:
                    return _err(f"evidence {evidence_id} has no current observed "
                                "browser_capture for this criterion; run the "
                                "scenario and bind the evidence id it records")
                if not (obs.get("pre_certifiable") and obs.get("post_certifiable")
                        and obs.get("pre") == obs.get("post")):
                    return _err(f"evidence {evidence_id}: the observed capture did "
                                "not certify stable confined sources across the check")
                obs_identity = str(obs.get("identity") or "")
                current_identity = str((record.get("task") or {}).get("identity") or "")
                if obs_identity and current_identity and obs_identity != current_identity:
                    return _err(f"evidence {evidence_id} was captured under a "
                                "different task identity; rerun under the current task")
                row_ts = str(row.get("created_at") or "")
                declared_at = str(crit.get("declared_at") or "")
                turn_started = str((record.get("task") or {}).get("started_at") or "")
                if (declared_at and row_ts < declared_at) or \
                        (turn_started and row_ts < turn_started):
                    return _err(f"evidence {evidence_id} predates the criterion "
                                "declaration or the current turn; rerun the check "
                                "now and bind the evidence id it produces")
                # its source_revision must equal the canonical hash of
                # the CURRENT declared source set (producer-encoded form).
                # Never accept an arbitrary digest merely because a row holds it.
                current, certifiable = _source_hashes(workspace, crit.get("paths"))
                if not certifiable:
                    return _err("declared source is missing/unreadable/escaping; "
                                "external evidence cannot certify it")
                canonical_rev = _canonical_source_revision(current)
                row_rev = str(row.get("source_revision") or "")
                if row_rev != canonical_rev:
                    return _err(f"evidence {evidence_id} source_revision "
                                f"{row_rev or '(none)'} does not match the current "
                                f"canonical source-set hash {canonical_rev}")
                snapshot = obs["post"] if obs.get("post") else current
            crit["evidence"] = {
                "evidence_id": evidence_id,
                "identity": str(row.get("canonical_command") or row.get("command") or ""),
                "observed_at": row.get("created_at"), "session_id": session_id,
                "workspace": workspace, "source_hashes": snapshot,
                "source_revision": str(row.get("source_revision") or ""),
                "artifact_digest": str(row.get("artifact_digest") or ""),
            }
            record["acceptance"] = criteria
            _save(session_id, record)
            return json.dumps({"success": True, "id": cid, "evidence_id": evidence_id})
        if action == "disposition":
            cid = str(args.get("id") or "").strip()
            disp = str(args.get("disposition") or "").strip().lower()
            if cid not in criteria:
                return _err(f"criterion {cid} is not declared")
            if disp not in _DISPOSITIONS:
                return _err(f"disposition must be one of {'|'.join(_DISPOSITIONS)}")
            if disp == "clear":
                criteria[cid].pop("disposition", None)
            else:
                if disp == "cancelled":
                    criteria[cid].pop("evidence", None)  # cancellation is not completion
                criteria[cid]["disposition"] = {
                    "status": disp, "reason": str(args.get("reason") or "")}
            record["acceptance"] = criteria
            _save(session_id, record)
            return json.dumps({"success": True, "id": cid, "disposition": disp})
        # action == "status"
        agent = _active_agent(session_id)
        criteria, required, _cancellable, blocked = _acceptance_state(record, agent)
        report = []
        for cid, crit in criteria.items():
            ev = crit.get("evidence") or {}
            disp = str((crit.get("disposition") or {}).get("status") or "")
            if disp == "cancelled":
                status = "cancelled"
            elif disp == "blocked":
                status = "blocked"
            elif ev:
                if workspace is None:
                    status = "unverified"
                else:
                    ok, _reason = _binding_current(cid, crit, session_id, workspace)
                    status = "verified" if ok else "stale_evidence"
            else:
                status = "unverified"
            report.append({"id": cid, "observable": crit.get("observable"),
                           "check_kind": crit.get("check_kind"),
                           "check_identity": crit.get("check_identity"),
                           "paths": crit.get("paths"), "refs": crit.get("refs") or [],
                           "disposition": disp or None, "status": status,
                           "evidence_id": ev.get("evidence_id")})
        # multiplexed-gateway privacy: only the requesting session's own
        # observations; other sessions' criterion ids / evidence ids are
        # never disclosed, regardless of the display window.
        observations = [{"evidence_id": o.get("evidence_id"),
                         "criterion": o.get("criterion"),
                         "source_stable": (o.get("pre") == o.get("post"))}
                        for k, o in _evidence_obs.items()
                        if str(k).startswith(session_id + "|")][-10:]
        return json.dumps({"success": True, "criteria": report,
                           "native_todos": [{"id": t, "status": s}
                                            for t, s in _todo_rows(agent)],
                           "uncovered_todo_outcomes": sorted(required - set(criteria)),
                           "blocked_criteria": blocked,
                           "evidence_observations": observations},
                          default=str)

    ctx.register_tool(
        name="acceptance", toolset="continuation",
        schema={"name": "acceptance",
                "description": "Explicit acceptance ledger: declare criteria (id, observable, check identity, source paths), bind explicitly selected native evidence IDs, report status, or set visible cancelled/blocked dispositions.",
                "parameters": {"type": "object", "properties": {
                    "action": {"type": "string", "enum": list(_ACCEPTANCE_ACTIONS)},
                    "id": {"type": "string", "description": "Stable criterion id (native todo id where applicable)."},
                    "observable": {"type": "string", "description": "Observable condition of the criterion."},
                    "check_kind": {"type": "string", "enum": list(_CHECK_KINDS)},
                    "check_identity": {"type": "string", "description": "Exact command or scenario identity the evidence must carry."},
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "Workspace-relative source paths the criterion is about."},
                    "refs": {"type": "array", "items": {"type": "string"}, "description": "Optional source/rule references."},
                    "evidence_id": {"type": "integer", "description": "bind: explicitly selected real native verification event id; validated against the host lookup for this session/workspace (unavailable host = unverified, never auto-bound)."},
                    "disposition": {"type": "string", "enum": ["cancelled", "blocked", "clear"]},
                    "reason": {"type": "string"}},
                    "required": ["action"]}},
        handler=lambda args, **kw: acceptance(args, **kw),
        description="Evidence-based acceptance declarations and bindings.",
    )

    def on_pre_tool_call(tool_name: str = "", session_id: str = "",
                         tool_call_id: str = "", args=None, turn_id: str = "",
                         **_rest):
        """Check-run source snapshot for declared criteria. Recording an
        observation is NOT binding; the bind tool remains the only binder.
        terminal criteria observe terminal checks; scenario/external
        criteria observe browser_capture (its recorded native row is the
        check). The workspace follows the native task identity, not a
        session guess."""
        if tool_name not in ("terminal", "browser_capture") or not session_id:
            return None
        # Same-agent rotation can land mid-turn with no pre_llm_call or tool
        # call before the declared check: migrate BEFORE reading criteria, or
        # the post-compaction check would record nothing and the following
        # bind would reject the genuine execution as unobserved.
        _migrate_session(session_id, _active_agent(session_id))
        record = _session_record(session_id)
        criteria = record.get("acceptance") or {}
        task_id = str(_rest.get("task_id")
                      or (record.get("task") or {}).get("task_id") or "")
        workspace = _workspace_root(session_id, task_id)
        if workspace is None:
            return None
        def _eligible(crit: Dict[str, Any]) -> bool:
            # Observation only certifies the DECLARED workspace: once
            # the native task cwd moves, the check can no longer attest
            # to the pinned criterion — redeclaration (in the current
            # tree) is required before observations are recorded again.
            pinned = str(crit.get("workspace") or "")
            if pinned:
                try:
                    same_ws = Path(pinned).resolve() == Path(workspace).resolve()
                except OSError:
                    same_ws = pinned == workspace
                if not same_ws:
                    return False
            kind = str(crit.get("check_kind") or "")
            if tool_name == "terminal":
                return kind == "terminal"
            return kind in ("scenario", "external")

        eligible = {k: v for k, v in criteria.items() if _eligible(v)}
        n = len(eligible)
        gkey = f"{session_id}|{tool_call_id}"
        created = False
        with _tool_obs_lock:
            # Group admission BEFORE any observation or dispatch: a check
            # is observed only if its whole-call group fits the bound.
            group = _tool_obs_groups.get(gkey)
            if group is None:
                if not n:
                    return None
                max_obs = globals()["_MAX_OBS_PER_CALL"]  # runtime read: tests shrink it
                if n > max_obs:
                    # An oversized declaration is refused outright (fail
                    # closed): the check runs unobserved, so its evidence
                    # can never be bound — never half-trimmed.
                    return {"action": "block",
                            "message": (f"acceptance: {n} eligible criteria exceed the "
                                        f"{max_obs}-criterion bound for one tool call; "
                                        "split the declared check — its evidence cannot bind unobserved")}
                max_groups = globals()["_MAX_GROUPS"]  # runtime read: tests shrink it
                if len(_tool_obs_groups) >= max_groups:
                    # Evict WHOLE completed groups only — pending observations
                    # of any peer call are never touched, partially or wholly.
                    # Other sessions' completed groups first, then this
                    # session's completed history; dict order preserves
                    # recency within each tier.
                    for k in sorted((k for k, g in _tool_obs_groups.items() if g["done"]),
                                    key=lambda k: (k.split("|", 1)[0] == session_id,)):
                        if len(_tool_obs_groups) < max_groups:
                            break
                        _tool_obs_groups.pop(k, None)
                        for key in [x for x in _evidence_obs if x.startswith(k + "|")]:
                            _evidence_obs.pop(key, None)
                if len(_tool_obs_groups) >= max_groups:
                    return {"action": "block",
                            "message": (f"acceptance: all {max_groups} evidence-observation call "
                                        "groups are active; wait for an in-flight check to finish "
                                        "(or re-declare) before recording a new one")}
                group = {"done": False, "n": 0}
                _tool_obs_groups[gkey] = group
                created = True
        # Pre-snapshot hashing on the observer thread, holding no lock —
        # mirroring on_post_tool_call: a bounded pre-observer abandoned
        # mid-hash must never hold _tool_obs_lock while the guaranteed
        # settlement seam runs on the dispatching thread. The reservation
        # booked above is what settlement releases if this observer is
        # abandoned here.
        rows = {}
        for cid, crit in eligible.items():
            pre, pre_cert = _source_hashes(workspace, crit.get("paths"))
            rows[cid] = {
                "criterion": cid, "tool": tool_name,
                "command": str((args or {}).get("command") or ""),
                # the ACTUAL execution directory of this call (a
                # per-command workdir is transient and never hijacks
                # the session cwd); a check executed outside the
                # declared workspace cannot certify the declared sources.
                "exec_cwd": str((args or {}).get("workdir") or workspace),
                "identity": ("turn:" + str(turn_id)) if turn_id else "",
                "generation": int(crit.get("generation") or 0),
                "pre": pre, "pre_certifiable": pre_cert,
                "post": None, "post_certifiable": False,
                "evidence_id": None}
        fresh = _session_record(session_id).get("acceptance") or {}
        with _tool_obs_lock:
            if _tool_obs_groups.get(gkey) is group:
                # Commit re-checks identity and generation so a late or
                # superseded observer can neither resurrect rows nor
                # bless a re-declared criterion: an evicted/renamed
                # group commits nothing, and a declaration that changed
                # while this observer hashed owns the rows instead.
                m = 0
                for cid, row in rows.items():
                    cur = fresh.get(cid)
                    if cur is None or int(cur.get("generation") or 0) \
                            != int(row.get("generation") or 0):
                        continue
                    _evidence_obs[f"{gkey}|{cid}"] = row
                    m += 1
                # re-observation of an in-flight call keeps the higher
                # live row count; capacity was already held at admission
                group["n"] = m if created else max(group["n"], m)
        return None

    def on_post_tool_call(tool_name: str = "", session_id: str = "",
                          tool_call_id: str = "", result=None, **_rest):
        """Post-check source snapshot + the evidence id from the real native
        result payload (terminal: verification_evidence.id; browser_capture:
        flat native_recorded + integer evidence_id). Source mutation across
        the check is recorded here and prohibits binding at bind time.
        All source IO runs OUTSIDE _tool_obs_lock: a bounded observer
        suppressed by overlap/timeout must never hold the lock across
        hashing while the guaranteed settlement seam runs on the dispatch
        thread. Commit re-checks identity so late delivery can neither
        resurrect rows/groups nor bless stale declarations."""
        if tool_name not in ("terminal", "browser_capture") or not session_id:
            return None
        _migrate_session(session_id, _active_agent(session_id))
        record = _session_record(session_id)
        criteria = record.get("acceptance") or {}
        task_id = str(_rest.get("task_id")
                      or (record.get("task") or {}).get("task_id") or "")
        workspace = _workspace_root(session_id, task_id)
        gkey = f"{session_id}|{tool_call_id}"
        with _tool_obs_lock:
            group = _tool_obs_groups.get(gkey)
            pending = []
            for key in [k for k in _evidence_obs
                        if str(k).startswith(f"{gkey}|")]:
                obs = _evidence_obs.pop(key, None)
                if obs is None:
                    continue
                crit = criteria.get(obs.get("criterion"))
                if crit is None:
                    continue
                pending.append((key, obs, crit))
        for key, obs, crit in pending:
            if tool_name == "terminal":
                obs["evidence_id"] = _evidence_id_from_result(result)
            else:
                # browser_capture flat result: only a genuinely recorded
                # row with an integer evidence id is bindable;
                # native_recorded false or null id leaves the
                # observation unbindable.
                payload = result if isinstance(result, dict) else {}
                if isinstance(result, str):
                    try:
                        payload = json.loads(result)
                    except ValueError:
                        payload = {}
                if payload.get("native_recorded") is True and \
                        isinstance(payload.get("evidence_id"), int):
                    obs["evidence_id"] = payload["evidence_id"]
            if workspace is not None:
                post, post_cert = _source_hashes(workspace, crit.get("paths"))
                obs["post"], obs["post_certifiable"] = post, post_cert
        fresh = _session_record(session_id).get("acceptance") or {}
        with _tool_obs_lock:
            if group is not None and _tool_obs_groups.get(gkey) is group:
                for key, obs, crit in pending:
                    cur = fresh.get(obs.get("criterion"))
                    if cur is None or int(cur.get("generation") or 0) \
                            != int(obs.get("generation") or 0):
                        # re-declared while this bounded observer was hashing:
                        # the declaration purge owns the rows, never bless it
                        continue
                    _evidence_obs[key] = obs
                # Group is whole completed history, evictable as a unit. If
                # it was evicted (or replaced) while this bounded observer
                # hashed sources, nothing is re-inserted and no done flag is
                # stamped on a foreign object — no resurrection, ever.
                group["done"] = True
        return None

    def on_post_tool_call_settled(tool_name: str = "", session_id: str = "",
                                  tool_call_id: str = "", **_rest):
        """Guaranteed terminal bookkeeping (host-opt-in post_tool_call_settled):
        the tool call is over regardless of whether the bounded
        post_tool_call observer was delivered (_HOOK_SKIPPED overlap/
        timeout has NO replay). Marking the group done keeps its slot
        evictable, so suppressed observations can never wedge the group
        budget for future terminal/browser checks. Dropped observations
        stay unbindable (post-hash is None) — dropped evidence never
        becomes passing evidence; if the late observer still commits,
        its genuine evidence remains bindable."""
        if tool_name not in ("terminal", "browser_capture") or not session_id:
            return None
        gkey = f"{session_id}|{tool_call_id}"
        with _tool_obs_lock:
            group = _tool_obs_groups.get(gkey)
            if group is not None:
                group["done"] = True
        return None


    def _parse_verdict(text: str, required: Optional[set] = None,
                       cancellable: Optional[set] = None) -> Optional[Dict[str, Any]]:
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
        required = required or set()
        if required:
            # Coverage is a REQUIRED verdict field once acceptance criteria
            # or native todo outcomes exist: "clean" without an explicit
            # per-id assessment is not clean.
            coverage = obj.get("coverage")
            if not isinstance(coverage, dict) or not required <= set(coverage):
                return None
            cancellable = cancellable or set()
            if not set(coverage.values()) <= set(_COVERAGE_VALUES):
                return None
            if any(str(v) == "cancelled" and str(cid) not in cancellable
                   for cid, v in coverage.items()):
                return None
            if verdict == "clean" and any(
                    str(coverage.get(cid)) !=
                    ("cancelled" if str(cid) in cancellable else "covered")
                    for cid in required):
                # cancelled criteria must map to "cancelled" (never laundered
                # into "covered"); everything else must be genuinely covered.
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
                         owner: str = "", workspace: Optional[str] = None):
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
        _acriteria, required, cancellable, _ablocked = _acceptance_state(record, agent)
        mapping = _acceptance_mapping_text(_acriteria, agent, session_id, workspace)
        if mapping:
            mapping_text = "[continuation-acceptance mapping] " + mapping
            if len(mapping_text) > _REVIEW_MSG_CHAR_CAP:
                # native snapshot_recent_messages truncates every message at
                # its _MESSAGE_CHAR_CAP — the reviewer would silently lose
                # whole criterion/NATIVE_TODO rows despite the required ids
                # in focus. Fail closed with an explicit unverified reason;
                # never deliver a truncated mapping.
                record["rounds"] = int(record.get("rounds") or 0) + 1
                record["blockers"] = list(record.get("findings") or []) + [
                    f"acceptance mapping exceeds the native review message "
                    f"cap ({len(mapping_text)} > {_REVIEW_MSG_CHAR_CAP} chars; unverified)"]
                _save(session_id, record, owner)
                logger.info("continuation: acceptance mapping overflow (%d chars) "
                            "refused review dispatch for session %s",
                            len(mapping_text), session_id)
                return {"action": "continue",
                        "message": ("Independent review cannot run: the acceptance mapping "
                                    f"({len(mapping_text)} chars) exceeds the native "
                                    f"{_REVIEW_MSG_CHAR_CAP}-char message cap and the reviewer "
                                    "would receive it truncated. Completion cannot be certified. "
                                    "Split acceptance across turns or cancel criteria so the "
                                    "mapping fits.")}
            messages.append({"role": "user", "content": mapping_text})
        # Pending state is established BEFORE native dispatch: start_review may
        # block synchronously (pool full / async unsupported) and the host bounds
        # plugin callbacks with a timeout; an abandoned callback's later writes
        # are fenced by the owner token, and a timeout leaves this blocker.
        record = dict(record, rounds=record.get("rounds", 0) + 1)
        record["review"] = {"snapshot": snaps,
                            "identity": str(task.get("identity") or ""),
                            "paths": list(changed_paths),
                            "required_coverage": sorted(required),
                            "cancellable": sorted(cancellable),
                            "acceptance_digest": _acceptance_digest(record, agent)}
        record["blockers"] = list(record.get("findings") or []) + [
            f"independent review dispatching (round {record['rounds']}/{max_review_rounds}); "
            + _PENDING_MARK]
        _save(session_id, record, owner)
        try:
            from agent.review_engine import start_review

            focus = _REVIEW_FOCUS
            if required:
                focus += _COVERAGE_FOCUS + " Required ids: " + ", ".join(sorted(required)) + "."
            focus += " Changed: " + ", ".join(changed_paths[:8])
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
        verdict = _parse_verdict(_review_summary(result), required, cancellable)
        if verdict and verdict["verdict"] == "clean":
            task_id_now = str((record.get("task") or {}).get("task_id") or "")
            snaps_now = _snapshots(agent, changed_paths, session_id, task_id_now)
            workspace_now = _workspace_root(session_id, task_id_now)
            drift_now = _bound_source_drift(record, session_id, workspace_now)
            digest_now = _acceptance_digest(record, agent)
            if _gaps(snaps_now) or drift_now or (
                    snaps_now != record["review"].get("snapshot")) or (
                    digest_now != record["review"].get("acceptance_digest")):
                # Native reviewers run with tools, and a concurrent worker
                # can also touch the tree: the clean verdict is valid for the
                # state that was reviewed, not the state after it moved. The
                # same current-state comparison the async/cached routes
                # re-enter applies to the immediate synchronous return.
                record["review"] = {}
                record["blockers"] = list(record.get("findings") or []) + [
                    "state changed while the synchronous review ran (unverified)"]
                _save(session_id, record, owner)
                return {"action": "continue",
                        "message": ("The reviewed state moved during the synchronous review; "
                                    "that clean verdict cannot certify the new state. "
                                    "Re-verify and finish only after a fresh clean review.")}
            record["review"]["verdict"] = "clean"
            cancelled = _acceptance_state(record, agent)[2]
            if cancelled:
                # Agent-authored cancellations are bounded UNVERIFIED
                # releases on the synchronous channel too — never clean
                # completion, identical labeling to the async path.
                record["blockers"] = [
                    "cancelled acceptance items remain unverified: "
                    + ", ".join(sorted(cancelled))
                    + " (cancellation is not completion proof)"]
            else:
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
            _migrate_session(session_id, agent)
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

            # Acceptance ledger: blocked dispositions are hard, visible
            # blockers; evidence whose declared source drifted no longer
            # attests to the current state (unverified, not stale-clean).
            # The native pre_verify producer emits NO task_id: the captured
            # pre_llm_call record is the authoritative source for the
            # effective task identity here, and the task cwd is checked
            # FIRST (terminal registries key on the effective task id).
            task_id = str((record.get("task") or {}).get("task_id") or "")
            workspace = _workspace_root(session_id, task_id)
            _acriteria, _required, _cancellable, ablocked = _acceptance_state(record, agent)
            if ablocked:
                record["blockers"] = list(record.get("findings") or []) + ablocked
                _save(session_id, record, owner)
                return {"action": "continue",
                        "message": ("Acceptance dispositions block completion: "
                                    + _blockers_text(ablocked))}
            # Required outcomes WITHOUT current valid evidence can never be
            # certified — and a reviewer's word cannot supply the missing
            # proof. Hard blocker BEFORE any review dispatch, per outcome.
            unbound = [cid for cid, crit in _acriteria.items()
                       if str((crit.get("disposition") or {}).get("status"))
                       not in ("cancelled", "blocked") and not crit.get("evidence")]
            unbound += [tid for tid in sorted(_required - set(_acriteria))
                        if tid not in _cancellable]
            if unbound:
                blockers = [f"required outcome {cid} has no valid evidence binding "
                            "(declare the criterion and bind native evidence; unverified)"
                            for cid in sorted(set(unbound))]
                record["blockers"] = list(record.get("findings") or []) + blockers
                _save(session_id, record, owner)
                return {"action": "continue",
                        "message": ("Acceptance outcomes are unverified: "
                                    + _blockers_text(blockers)
                                    + ". A reviewer cannot substitute for native evidence; "
                                      "run the declared checks and bind their evidence ids.")}
            drift = _bound_source_drift(record, session_id, workspace)
            if drift:
                blockers = [f"acceptance criterion {cid} evidence no longer certifies "
                            f"the current state ({reason}; unverified)"
                            for cid, reason in drift]
                record["blockers"] = list(record.get("findings") or []) + blockers
                _save(session_id, record, owner)
                return {"action": "continue",
                        "message": ("Acceptance evidence is no longer current: "
                                    + _blockers_text(blockers)
                                    + ". Re-run the declared checks and re-bind current evidence.")}

            # 2. fresh native verification for the agent's workspace.
            snaps = _snapshots(agent, paths, session_id, task_id)
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
                    # Reviewer still running: release the turn instead of holding
                    # every internal round. The binding stays (released) so the
                    # next genuine turn picks the delegation up; release is silent.
                    record["review"] = dict(review, released=True)
                    record.pop("blockers", None)
                    _save(session_id, record, owner)
                    with _session_lock:
                        # Release is deliberate and silent: the provisional
                        # pending marker must not label the released output.
                        if _owners.get(session_id) == owner:
                            _memory_blockers.pop(session_id, None)
                    return None
                if delegation is None or str(delegation.get("state") or "") != "completed":
                    record["review"] = {}  # lost/failed: not a clean review; cycle continues
                else:
                    if (review.get("snapshot") != snaps
                            or review.get("identity") != identity
                            or not set(paths) <= set(review.get("paths") or [])
                            or review.get("acceptance_digest")
                            != _acceptance_digest(record, agent)):
                        # Edits (even same-path) moved the evidence, the genuine user turn
                        # changed, or files outside the reviewed set were touched: reviewed
                        # state invalidated.
                        record["review"] = {}
                    else:
                        verdict = _parse_verdict(
                            _review_summary(delegation.get("result") or {}),
                            set(review.get("required_coverage") or ()),
                            set(review.get("cancellable") or ()))
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
                            record.pop("findings", None)  # clean renewal on renewed evidence
                            cancelled = _acceptance_state(record, agent)[2]
                            if cancelled:
                                # Cancelled work stays explicitly unverified:
                                # an agent-authored disposition (plus a reviewer
                                # "cancelled" mapping) is not proof of human
                                # scope change. Release is bounded and labeled.
                                record["blockers"] = [
                                    "cancelled acceptance items remain unverified: "
                                    + ", ".join(sorted(cancelled))
                                    + " (cancellation is not completion proof)"]
                            else:
                                record.pop("blockers", None)
                            _save(session_id, record, owner)
                            return None  # release on THIS snapshot; cancelled labeled unverified
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
                    and set(paths) <= set(review.get("paths") or [])
                    and review.get("acceptance_digest") == _acceptance_digest(record, agent)):
                record.pop("findings", None)
                cancelled = _acceptance_state(record, agent)[2]
                if cancelled:  # same bounded unverified release
                    record["blockers"] = [
                        "cancelled acceptance items remain unverified: "
                        + ", ".join(sorted(cancelled))
                        + " (cancellation is not completion proof)"]
                else:
                    record.pop("blockers", None)  # synchronous clean binding still current
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
                                    str(final_response or ""), owner, workspace)
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
        agent = _active_agent(session_id)
        if agent is None:
            return None
        _migrate_session(session_id, agent)
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
                "started_at": _now_iso(),
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
    # Check-run source observation: terminal checks (terminal-kind criteria)
    # and scenario/external checks (browser_capture rows) snapshot sources
    # pre/post and carry the authoritative native evidence id. Observing is
    # recording only; the acceptance tool's bind is the only binder.
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    # Guaranteed terminal bookkeeping: bounded post_tool_call delivery can
    # be suppressed (_HOOK_SKIPPED, no replay), so group settlement rides
    # the host's caller-thread post_tool_call_settled seam instead.
    ctx.register_hook("post_tool_call_settled", on_post_tool_call_settled)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    # on_session_end deliberately NOT registered: a reviewer still pending at
    # run boundary must stay bound; reset/finalize/stop bound the NEW identity.
    ctx.register_hook("on_session_reset", _clear_session)
    ctx.register_hook("on_session_finalize", _clear_session)
    ctx.register_hook("agent_loop_stopped", _clear_session)
    logger.info("continuation: evidence-gated completion active (max %d review rounds/session)",
                max_review_rounds)
