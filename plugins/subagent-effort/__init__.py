"""Subagent effort routing — port of dsh-subagent-effort (PURE SHADOW).

subagent_start fires with parent_session_id/child_session_id/child_goal.
We capture the child's goal and start classification (simple | complex)
against the self-hosted classifier in a BOUNDED background worker — the
child launch critical path performs no classifier I/O and never waits.
The plugin RECORDS only: the verdict, the suggested effort, and the
child's actual runtime origin (reasoning_config/model/provider/api_mode)
for evaluation. The live agent is NEVER touched — there is no apply path
by design.

Row semantics: every started child keeps a row keyed by child_session_id
with classification_state pending | classified | unavailable (and
classification_reason for unavailable). verdict/suggested_effort are null
until classification completes; unproven is never presented as a label.
Native evidence (origin, outcome, usage, cost, verification) is captured
independently of classification timing. A late classification result may
enrich a RETAINED row after subagent_stop, but never resurrects live
usage/session bookkeeping, evicted rows, or unloaded-plugin state.
subagent_stop ALWAYS clears the child's live usage/session state.

The recommendation is normalized through the native capability machinery:
agent.reasoning_effort.clamp_effort clamps the suggestion onto the route's
supported efforts. When the provider/model capability table is absent, the
supported set is recorded as explicitly unknown — we never invent a lower
effort level, and every explicit operator/model/tool choice stays untouched.

Evidence is observed, not inferred: subagent_stop carries child_status,
duration_ms and a metadata-only tool_call_history (repeated_mutation_targets
is a rework PROXY, never presented as proven rework or correctness); token
usage is aggregated from post_api_request's response.usage summary (native
normalize_usage buckets); per-child cost is captured on the child's own
post_llm_call from the NATIVE accounting accessors
(session_estimated_cost_usd/session_cost_status — same attributes
delegate_tool_child_run.py reads) while the child agent is still alive, since
the parent subagent_stop payload carries none. Amount is None only when
genuinely unproven (missing/non-finite/negative/unknown status); partial or
estimated statuses pass through native verbatim, never presented as actual
billing. Verification evidence is read from Hermes' own verification ledger
(captured child-side on post_llm_call, exact session). Completed is NEVER
reported as correct; summary rows carry outcome_observed for calibration.
Durable: verdicts/evidence persist as bounded rows in the plugin's own
profile-scoped ctx.state so summaries survive gateway restarts; a row still
pending after a restart is honestly marked unavailable/interrupted — its
classifier died with the previous process.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
import time
from contextvars import copy_context

logger = logging.getLogger(__name__)

SCHEMA = {"difficulty": ["simple", "complex"]}
_MAX_ROWS = 50
_BOOKKEEPING_INTERVAL = 30
# Classifier workers are daemon threads capped by a bounded semaphore: no
# unbounded thread/queue growth per child, and the child-launch hook path
# NEVER blocks on classifier I/O. Overflow fails open (reason overloaded).
_MAX_CLASSIFIERS = 4


def register(ctx) -> None:
    endpoint = (ctx.get_config("endpoint", "http://192.168.50.149:8787") or "").rstrip("/")
    api_key_env = ctx.get_config("api_key_env", "CLASSIFIER_API_KEY")
    low = ctx.get_config("low_effort", "low")
    high = ctx.get_config("high_effort", "high")

    from hermes_constants import parse_reasoning_effort


    config_error = None
    if parse_reasoning_effort(low) is None or parse_reasoning_effort(high) is None:
        # Invalid config: stay IDLE (classify nothing) but keep the /command
        # status inspectable with the config error — genuine operational
        # features must be diagnosable, never silently dead.
        config_error = f"invalid effort config (low={low!r} high={high!r})"
        logger.warning("subagent-effort: %s; plugin idle", config_error)
    # Durable bounded rows: survive gateway restarts; newest wins over
    # anything loaded at register time.
    verdicts: dict[str, dict] = {}
    try:
        persisted = ctx.state.get("shadow")
        if isinstance(persisted, dict):
            verdicts.update(persisted)
            for old in verdicts.values():
                if old.get("classification_state") == "pending":
                    old["classification_state"] = "unavailable"
                    old["classification_reason"] = "interrupted"
    except Exception:
        logger.warning("subagent-effort: durable state unreadable; continuing in-memory", exc_info=True)

    # One small lock for ALL mutable plugin state (verdicts/jobs/
    # child_sessions/usage_totals + the state-JSON write). Hooks fire on
    # child threads and the parent thread concurrently; classify()'s HTTP
    # call happens on its own daemon worker thread, never under this lock.
    _lock = threading.Lock()

    def _enforce_rows_locked() -> None:
        if len(verdicts) > _MAX_ROWS:
            for stale in sorted(verdicts)[: len(verdicts) - _MAX_ROWS]:
                verdicts.pop(stale, None)

    def _persist() -> None:
        # Serialize + durable write INSIDE the lock: callers hold no lock,
        # so an older snapshot can never overwrite newer rows and nested rec
        # dicts cannot mutate while the state store serializes them.
        with _lock:
            _enforce_rows_locked()
            try:
                ctx.state.set("shadow", verdicts)
            except Exception:
                logger.warning("subagent-effort: durable state write failed (quota?)",
                               exc_info=True)


    _last_bookkeeping = [-_BOOKKEEPING_INTERVAL]  # first write always passes

    def _persist_bookkeeping() -> None:
        # Rate-limited write for pure bookkeeping (pending row, captured
        # origin). Verdicts are written as a full-dict snapshot, so any
        # later durable write carries these fields; a dropped throttle
        # window loses nothing durable and never rewrites after unload.
        now = time.monotonic()
        if now - _last_bookkeeping[0] < _BOOKKEEPING_INTERVAL:
            return
        _last_bookkeeping[0] = now
        _persist()

    # child_session_id -> observed usage, aggregated from post_api_request.
    usage_totals: dict[str, dict] = {}
    child_sessions: set[str] = set()
    # child_session_id -> pending classifier attempt (threading.Event
    # identity token). The Event's .value carries the result for test
    # synchronization: unset = classification in flight, set = settled
    # (classified or failed). Cleared atomically with its row.
    jobs: dict[str, threading.Event] = {}
    # Bounded concurrency, fail-open: overflow is recorded, never queued
    # onto an unbounded backlog.
    _slots = threading.BoundedSemaphore(_MAX_CLASSIFIERS)
    # True once the native loader disposes this plugin: late workers and
    # hooks become inert; durable rows are never rewritten after unload.
    closed = False

    def classify(goal: str) -> str | None:
        key = os.environ.get(api_key_env or "", "")
        if config_error or not key or not endpoint or not goal:
            return None
        body = json.dumps({"text": goal[:100000], "schema": SCHEMA}).encode()
        req = urllib.request.Request(
            f"{endpoint}/classify", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                v = json.loads(resp.read()).get("difficulty")
            return v if v in ("simple", "complex") else None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            return None

    def _supported_efforts(provider: str, model: str):
        """Native capability tri-state for the child's actual route (None = unknown)."""
        try:
            from hermes_cli.models_reasoning_caps import (
                nous_model_reasoning_capabilities, openrouter_model_reasoning_capabilities)
            p = (provider or "").lower()
            if p == "openrouter":
                caps = openrouter_model_reasoning_capabilities(model, allow_fetch=False)
            elif p == "nous":
                caps = nous_model_reasoning_capabilities(model, allow_fetch=False)
            else:
                return None
            if caps and caps.get("supports_reasoning") is True:
                return caps.get("supported_efforts")
            return None
        except Exception:
            return None

    def _apply_verdict(rec: dict, value, reason) -> None:
        from agent.reasoning_effort import clamp_effort
        # caller holds _lock. Honest, bounded enrichment of a RETAINED row:
        # never resurrects live usage/session bookkeeping.
        rec["classification_reason"] = reason
        if value not in ("simple", "complex"):
            rec["classification_state"] = "unavailable"
            return
        rec["verdict"] = value
        rec["suggested_effort"] = high if value == "complex" else low
        rec["classification_state"] = "classified"
        # verdict after origin capture: complete the clamping then —
        # independent of the first-turn ordering.
        child = rec.get("child")
        if child:
            supported = rec["supported_efforts"]
            clamped = (clamp_effort(rec["suggested_effort"], supported)
                       if supported != "unknown" else None)
            rec["clamped_effort"] = clamped if clamped is not None else "unknown"

    def _finish_classification(child_session_id: str, job: threading.Event,
                               value, reason: str) -> None:
        # late completion guard: a settled result may only enrich the row
        # the classifier was started for while the plugin is loaded —
        # never after unload, for a re-registered attempt, or for an
        # evicted row.
        persisted = False
        with _lock:
            if closed or jobs.get(child_session_id) is not job:
                job.set()
                return
            del jobs[child_session_id]
            job.value = value if value is not None else reason
            job.set()
            rec = verdicts.get(child_session_id)
            if rec is not None:
                _apply_verdict(rec, value, reason)
                persisted = True
        if persisted:
            _persist()
        logger.info("subagent-effort: child %s classification %s (shadow)",
                    child_session_id, job.value)

    def _classifier_worker(child_session_id: str, job: threading.Event,
                           goal: str) -> None:
        # Bounded daemon worker: the child launch path already returned.
        # Scoped context (profile scoping, cwd contextvars) was copied at
        # submit time; the API key is read here, at call time, and never
        # stored in rows, logs or results.
        try:
            if not endpoint or not goal:
                value, reason = None, ("not_configured" if not endpoint else "empty_goal")
            else:
                try:
                    value = classify(goal)
                except Exception:
                    logger.warning("subagent-effort: classifier worker for %s failed",
                                   child_session_id, exc_info=True)
                    value, reason = None, "error"
                reason = None if value else "error"
                if value is None and not os.environ.get(api_key_env or "", ""):
                    reason = "not_configured"
            _finish_classification(child_session_id, job, value, reason)
        finally:
            _slots.release()

    def _spawn_classifier(child_session_id: str, rec: dict, goal: str) -> None:
        if not _slots.acquire(blocking=False):
            # fail-open: cap reached -> honest unavailable row, no backlog
            with _lock:
                if not closed:
                    _apply_verdict(rec, None, "overloaded")
            _persist()
            return
        job = threading.Event()
        with _lock:
            jobs[child_session_id] = job
        try:
            # Capture the registering thread's scoped context (profile scope,
            # runtime cwd contextvars) and run the worker inside it. Starting
            # the thread under copy_context().run would copy nothing: new
            # threads do not inherit contextvars.
            context = copy_context()
            threading.Thread(
                target=context.run,
                args=(_classifier_worker, child_session_id, job, goal),
                daemon=True,
                name=f"subagent-effort-classifier-{child_session_id}",
            ).start()
        except Exception:
            _slots.release()
            logger.warning("subagent-effort: classifier spawn failed for %s",
                           child_session_id, exc_info=True)
            with _lock:
                if not closed:
                    _apply_verdict(rec, None, "error")
            _persist()

    def on_subagent_start(child_session_id: str = "", child_goal: str = "", **_kw):
        # Launch-critical hook: NO classifier I/O here. The row exists the
        # moment the child starts; classification completes on a bounded
        # daemon worker and may enrich the (possibly stopped) row later.
        if not child_session_id:
            return
        goal = child_goal or ""
        rec = {
            "verdict": None,
            "suggested_effort": None,
            "supported_efforts": "unknown",
            "clamped_effort": "unknown",
            "classification_state": "pending",
            "classification_reason": None,
            "child": {},
            "goal_chars": len(goal),
        }
        with _lock:
            if closed:
                return
            child_sessions.add(child_session_id)
            verdicts[child_session_id] = rec
            _enforce_rows_locked()
            if config_error:
                # config-invalid: plugin stays IDLE (no classifier call, no
                # row mutation beyond the honest unavailable state)
                _apply_verdict(rec, None, "not_configured")
        _persist_bookkeeping()
        if config_error:
            return
        _spawn_classifier(child_session_id, rec, goal)
        logger.info("subagent-effort: child %s started (classification pending; shadow)",
                    child_session_id)

    def on_child_first_turn(session_id: str = "", **_kw):
        """Record the child's actual runtime origin (read-only — pure shadow).

        The child binds itself as the active parent for its own conversation
        (bind_subagent_parent), so this is the first point with a live agent
        handle. We only OBSERVE: reasoning_config, model, provider, api_mode
        are recorded for evaluation. Nothing is modified — every explicit
        operator/model/tool effort choice stays untouched by this plugin.
        """
        with _lock:
            rec = verdicts.get(session_id)
        if not rec or rec.get("child"):
            return
        try:
            from agent.subagent_lifecycle import get_active_subagent_parent
            agent = get_active_subagent_parent()
        except Exception:
            return
        if agent is None or getattr(agent, "session_id", None) != session_id:
            return
        # record runtime origin; derived facts computed outside the lock
        reasoning = getattr(agent, "reasoning_config", None)
        from agent.reasoning_effort import clamp_effort
        supported = _supported_efforts(getattr(agent, "provider", None),
                                       getattr(agent, "model", None))
        # origin capture is INDEPENDENT of classification timing: with no
        # verdict yet (or none ever) the suggestion stays unknown and the
        # clamp completes later in _apply_verdict, never fabricating a level
        suggested = rec.get("suggested_effort")
        clamped = (clamp_effort(suggested, supported)
                   if supported and suggested else None)
        with _lock:
            rec["child"] = {
                "reasoning_config": getattr(agent, "reasoning_config", None),
                "model": getattr(agent, "model", None),
                "provider": getattr(agent, "provider", None),
                "api_mode": getattr(agent, "api_mode", None),
            }
            # Explicitly unknown when the capability table is absent — the raw
            # suggestion stays as classified, clamped_effort stays unknown.
            rec["supported_efforts"] = supported or "unknown"
            rec["clamped_effort"] = clamped if clamped is not None else "unknown"
        _persist_bookkeeping()
        logger.info("subagent-effort: child %s origin recorded (reasoning=%s, clamped=%s)",
                    session_id, reasoning, clamped if clamped is not None else "unknown")

    def on_post_api_request(session_id: str = "", usage: dict | None = None, **_kw):
        # usage is the native normalize_usage() summary of response.usage
        # (api_request_hooks._usage_summary_for_api_request_hook).
        if isinstance(usage, dict):
            with _lock:
                if session_id in child_sessions:
                    agg = usage_totals.setdefault(session_id, {"prompt_tokens": 0,
                                                               "completion_tokens": 0,
                                                               "total_tokens": 0,
                                                               "requests": 0})
                    prompt = int(usage.get("prompt_tokens") or 0)
                    completion = int(usage.get("output_tokens") or 0)
                    agg["prompt_tokens"] += prompt
                    agg["completion_tokens"] += completion
                    # normalize_usage computes total = prompt + output;
                    # mirror that when the payload omits the explicit total
                    agg["total_tokens"] += int(usage.get("total_tokens") or (prompt + completion))
                    agg["requests"] += 1

    def on_post_llm_call(session_id: str = "", **_kw):
        """Capture the child's native cost + verification evidence while the
        child agent is still alive (post_llm_call fires on the child, bound to
        itself; the parent subagent_stop payload carries no per-child cost)."""
        with _lock:
            if session_id not in child_sessions:
                return
            rec = verdicts.get(session_id)
        if rec is None:
            return
        try:
            from agent.subagent_lifecycle import get_active_subagent_parent
            agent = get_active_subagent_parent()
        except Exception:
            return
        if agent is None or getattr(agent, "session_id", None) != session_id:
            return
        amount, status = _native_cost(agent)
        with _lock:
            observed = rec.setdefault("observed", {})
            if amount is not None or status != "unknown":
                observed["cost_usd"] = amount
                observed["cost_status"] = status
            observed["verification"] = _verification_evidence(session_id, live=True)

    def _native_cost(agent):
        # Same accessors _run_single_child uses (tools/delegate_tool_child_run
        # .py): session_estimated_cost_usd + session_cost_status. None amount
        # only for genuinely unproven values; native partial/estimated status
        # passes through verbatim — never presented as actual billing.
        raw = getattr(agent, "session_estimated_cost_usd", None)
        status = getattr(agent, "session_cost_status", None)
        status = status if isinstance(status, str) and status.strip() else "unknown"
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None, "unknown"  # non-numeric: unproven, not a native status
        if value != value or value in (float("inf"), float("-inf")) or value < 0:
            return None, "unknown"
        if status == "unknown":
            return None, "unknown"  # native default: unproven provenance
        return round(value, 6), status

    def on_subagent_stop(child_session_id: str = "", child_status: str | None = None,
                        duration_ms: int | None = None, tool_call_history=None, **_kw):
        # Observable outcome only. "completed" is a lifecycle fact, never a
        # correctness claim. Cost is kept independently of token usage: a
        # child whose native accounting holds a proven estimate but emitted
        # no observed post_api_request usage keeps its cost, with the token
        # fields unknown (None) — unknown, never fabricated 0.
        # Verification: child-side capture only (native ledger under the
        # child's live workspace context); at parent stop the ledger would
        # resolve the PARENT workspace, so missing capture stays unknown —
        # never the parent's evidence, never inferred.
        repeated = _repeated_mutation_targets(tool_call_history)
        usage = {
            "prompt_tokens": None, "completion_tokens": None,
            "total_tokens": None, "requests": None,
            "cost_usd": None, "cost_status": "unknown",
            "cost_source": "child agent native accounting (session_estimated_cost_usd/"
                           "session_cost_status, captured on post_llm_call)",
        }
        # stop ALWAYS clears live bookkeeping for this child (usage
        # totals + child_sessions) before anything else — even when the
        # row was evicted, never recorded, or classification never landed.
        # The row itself is RETAINED (pending/unavailable verdicts and all
        # observed evidence); late classification may enrich it later but
        # never re-arms child_sessions.
        with _lock:
            child_sessions.discard(child_session_id)
            session_usage = usage_totals.pop(child_session_id, None) or {}
            rec = verdicts.get(child_session_id)
            captured = rec.pop("observed", None) if rec is not None else None
        if rec is None:
            return
        captured = captured or {}
        usage.update({"prompt_tokens": session_usage.get("prompt_tokens"),
                      "completion_tokens": session_usage.get("completion_tokens"),
                      "total_tokens": session_usage.get("total_tokens"),
                      "requests": session_usage.get("requests"),
                      "cost_usd": captured.get("cost_usd"),
                      "cost_status": captured.get("cost_status", "unknown")})
        outcome = {"status": child_status, "latency_ms": duration_ms,
                   "usage": usage, "verification": captured.get("verification"),
                   # calibration: outcome evidence observed
                   "observed": True}
        if repeated:
            outcome["repeated_mutation_targets"] = repeated
        with _lock:
            if verdicts.get(child_session_id) is rec:
                rec["outcome"] = outcome
        _persist()

    _MUTATING_TOOLS = frozenset({"patch", "write_file"})

    def _repeated_mutation_targets(tool_call_history) -> list:
        # PROXY metric only: same side-effect target mutated >1 time.
        # Counts only confirmed native mutation events (status "ok" on a
        # side-effect tool) with stable target values — native history
        # carries string targets, possibly inside a bounded list (urls/
        # path lists from _sanitize_tool_target); non-string values (lists,
        # None) are iterated for their string items, never used as a key
        # directly (unhashable). Reads never count as rework.
        if not isinstance(tool_call_history, list):
            return []
        seen: dict = {}
        for entry in tool_call_history:
            if not isinstance(entry, dict):
                continue
            if entry.get("tool_name") not in _MUTATING_TOOLS:
                continue
            if str(entry.get("status") or "").lower() != "ok":
                continue
            targets = (entry.get("tool_input") or {}).get("targets") or {}
            for value in targets.values():
                # native _sanitize_tool_target can return bounded LISTS for
                # list-valued target keys (urls); iterate lists for their
                # string items — a list itself must never be used as a key
                # (unhashable TypeError kills the whole stop record)
                items = value if isinstance(value, list) else [value]
                for item in items:
                    if isinstance(item, str):
                        seen[item] = seen.get(item, 0) + 1
        return sorted(t for t, n in seen.items() if n > 1)

    def _verification_evidence(child_session_id: str, *, live: bool = False):
        # live=True: called from the child's own post_llm_call, where
        # verification_status resolves workspace facts under the child's live
        # context. From anywhere else (parent threads, stop handling) the
        # ledger resolves the PARENT workspace — that evidence must not be
        # attributed to the child, so callers get an explicit unknown.
        if not live:
            return None
        try:
            from agent.verification_evidence import verification_status
            # native session-cwd resolver, bound to the child's live
            # context (contextvar-backed) — never the gateway/parent cwd
            from agent.runtime_cwd import resolve_agent_cwd
            return verification_status(session_id=child_session_id,
                                       cwd=resolve_agent_cwd())
        except ImportError:
            # ledger module absent — unknown, not "passed". Genuine runtime
            # errors propagate: silently swallowing them hides real defects.
            return None

    def _summary_handler(_args=None):
        rows = []
        with _lock:
            for sid, rec in verdicts.items():
                outcome = rec.get("outcome") or {}
                usage = outcome.get("usage") or {}
                row = {
                    "child_session_id": sid,
                    "verdict": rec.get("verdict"),
                    "suggested_effort": rec.get("suggested_effort"),
                    "supported_efforts": rec.get("supported_efforts", "unknown"),
                    "clamped_effort": rec.get("clamped_effort", "unknown"),
                    # pending: classifier still in flight; unavailable:
                    # never available (reason says why). Rows survive all
                    # three states — native evidence is real regardless.
                    "classification_state": rec.get("classification_state", "unknown"),
                    "classification_reason": rec.get("classification_reason"),
                    "child_reasoning_config": (rec.get("child") or {}).get("reasoning_config"),
                    "child_model": (rec.get("child") or {}).get("model"),
                    "child_provider": (rec.get("child") or {}).get("provider"),
                    "outcome_status": outcome.get("status", "unknown"),
                    "latency_ms": outcome.get("latency_ms"),
                    "repeated_mutation_targets": outcome.get("repeated_mutation_targets"),
                    "usage": outcome.get("usage", "unknown"),
                    # verification captured child-side only; uncaptured (or
                    # child never observed an outcome) is explicit unknown
                    "verification": outcome.get("verification") or "unknown",
                    # calibration: child reported a lifecycle outcome vs never
                    # observed (shadow verdicts calibrate only on observed rows)
                    "outcome_observed": bool(outcome.get("observed")),
                }
                if usage:
                    # raw native accounting fields for policy evaluation
                    for raw_key in ("prompt_tokens", "completion_tokens", "total_tokens",
                                    "requests", "cost_usd", "cost_status", "cost_source"):
                        row[raw_key] = usage.get(raw_key)
                rows.append(row)
        payload = {"mode": "shadow", "children": rows,
                   "note": "pure shadow: the plugin never applies effort; "
                           "status is a lifecycle fact, not correctness; "
                           "repeated_mutation_targets is a proxy metric; "
                           "usage/verification 'unknown' means not observed; "
                           "classification_state 'pending' means the "
                           "classifier may still enrich this row; "
                           "cost partial/estimated statuses are native "
                           "accounting provenance, not actual billing"}
        if config_error:
            payload["config_error"] = config_error
        return json.dumps(payload, default=str)

    # Register commands AFTER handler definition (forward reference = NameError).
    ctx.register_command(
        "subagent_effort", _summary_handler,
        description="Inspect subagent effort shadow decisions and observed outcomes.",
        argument_mode="none",
    )

    ctx.register_hook("subagent_start", on_subagent_start)
    ctx.register_hook("pre_llm_call", on_child_first_turn)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("subagent_stop", on_subagent_stop)
    # Deliberately NO on_session_end cleanup: it fires on conversation
    # boundaries and would erase child usage before subagent_stop records it.

    def _unload():
        # Native unload/disposal cleanup (ctx.on_unload; covey-tasks
        # pattern): late classifier workers and hooks become inert. Live
        # usage/session bookkeeping is released here; RETAINED durable
        # rows are untouched (ctx.state is the only survivor, and a row
        # left pending is honestly marked interrupted on the next load).
        nonlocal closed
        with _lock:
            closed = True
            jobs.clear()
            child_sessions.clear()
            usage_totals.clear()

    ctx.on_unload(_unload)

    logger.info("subagent-effort: active in pure shadow mode (%s/%s)", low, high)
