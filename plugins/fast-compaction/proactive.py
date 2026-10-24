"""Proactive mid-session tool-result eviction, mounted on the host's seam.

turn_preflight calls ``prune_tool_results_only(messages, current_tokens)`` on the
registered context engine after every tool round when the request exceeds
``compression.proactive_prune_tokens`` (below the compression threshold, so the
summary path is not involved). The base implementation is deterministic and
LLM-free; this mixin replaces only the *policy* inside it, keeping every gate
the host enforces: trigger, tail-size guard, regrowth runway, reclaim floor,
store-capability check, and the archive_and_compact DB commit that makes the
rewrite durable. Prompt-cache hygiene stays the host's domain — one commit per
regrowth runway, never per turn.

Modes (plugin setting ``proactive_prune_mode``):
  off         the host's deterministic passes (default; base method untouched).
  fifo        positional oldest-first eviction of unpinned tool pairs via the
              fast-compaction pair primitives — no classifier call at all. This
              is the "cheap enough to run constantly" policy.
  classifier  the proven keep/drop pass first; any PruneError or classifier
              fault degrades this turn to fifo (never a second network call).

Pair safety contract is the pruner's: a pair drops only when BOTH halves drop,
pinned rows (head, recent window, either-half-recent, orphaned calls) are never
candidates, user/assistant text is never rewritten.
"""
# Attributes below are supplied by the host ContextCompressor at MRO-merge time;
# the mixin is never instantiated standalone.
# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MODES = ("off", "fifo", "classifier")

# Shared telemetry for whichever engine is registered (one engine slot anyway).
PROACTIVE_STATS = {"classifier": 0, "fifo": 0, "degraded": 0, "noops": 0}


class ProactiveUnavailable(Exception):
    """The classifier pass cannot run this turn; use the FIFO floor."""


class ProactivePruneMixin:
    """Requires the host engine as base and, from the host engine subclass:
    ``_proactive_config()`` -> {"mode", "target_tokens", "preserve_recent"},
    ``_proactive_pruner`` (module or None), ``_proactive_classifier_prune(messages)``
    -> (messages, stats) or raise ProactiveUnavailable, and ``_tokens(text)``.
    """

    def _proactive_config(self):  # supplied by the engine subclass
        raise NotImplementedError

    def _proactive_classifier_prune(self, messages) -> tuple:  # supplied by the engine
        raise ProactiveUnavailable("not_provided")

    # -- the override ------------------------------------------------------

    def prune_tool_results_only(self, messages, current_tokens=None):
        cfgp = {}
        try:
            cfgp = self._proactive_config() or {}
            mode = str(cfgp.get("mode") or "off").strip().lower()
        except Exception:
            mode = "off"
        if mode not in ("fifo", "classifier"):
            return super().prune_tool_results_only(messages, current_tokens=current_tokens)

        # -- host gates, reproduced from ContextCompressor.prune_tool_results_only.
        # Trigger: the host's proactive_prune_tokens wins; the plugin's own
        # proactive_trigger_tokens is the fallback so the feature is enableable
        # from plugin settings alone (the host default is 0 = feature off).
        trigger = int(self.proactive_prune_tokens or 0) or int(cfgp.get("trigger_tokens") or 0)
        if trigger <= 0 or (current_tokens is not None and current_tokens < trigger):
            return messages, 0
        if len(messages) <= self.protect_last_n + self._protect_head_size(messages) + 1:
            self._warn_reclamation_no_op("prune:tail_only", current_tokens)
            return messages, 0
        from agent.context_compressor import (PROACTIVE_PRUNE_REARM_MODEL_CONFIG_KEY,
                                              _estimate_msg_budget_tokens,
                                              stamp_db_persisted_markers)
        before = sum(_estimate_msg_budget_tokens(m) for m in messages)
        # Under-threshold runway skip is ordinary hysteresis (silent), same as base.
        if before < getattr(self, "_proactive_prune_rearm_tokens", 0) \
                and not self._billed_basis_over_threshold(current_tokens):
            return messages, 0
        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "_session_id", "")
        if session_db and session_id and not callable(getattr(session_db, "archive_and_compact", None)):
            self._warn_reclamation_no_op("prune:store_cannot_persist", current_tokens)
            return messages, 0

        # -- the policy, ours ------------------------------------------------
        pruned = None
        used = "fifo"
        if mode == "classifier":
            try:
                pruned, stats = self._proactive_classifier_prune(messages)
                used = "classifier"
            except ProactiveUnavailable:
                PROACTIVE_STATS["degraded"] += 1
                pruned = None
            except Exception as exc:  # a pass bug costs a prune, never a turn
                logger.warning("proactive prune: classifier pass failed (%s: %s); using fifo",
                               type(exc).__name__, exc)
                PROACTIVE_STATS["degraded"] += 1
                pruned = None
        if pruned is None:
            if self._proactive_pruner is None:
                # Nothing positional available either: let the host's own
                # deterministic passes have this turn.
                return super().prune_tool_results_only(messages, current_tokens=current_tokens)
            pruned, stats = self._proactive_fifo(messages, cfgp)

        pruned_count = stats.get("dropped", 0) + stats.get("truncated", 0)
        if not pruned_count or pruned is messages:
            self._warn_reclamation_no_op("prune:nothing_eligible", current_tokens)
            PROACTIVE_STATS["noops"] += 1
            return messages, 0

        # -- host commit bookkeeping, reproduced ------------------------------
        after = sum(_estimate_msg_budget_tokens(m) for m in pruned)
        reclaimed = max(0, before - after)
        if reclaimed < self.proactive_prune_min_reclaim_tokens:
            self._warn_reclamation_no_op("prune:reclaim_below_minimum", current_tokens, before=before)
            PROACTIVE_STATS["noops"] += 1
            return messages, 0
        runway = max(reclaimed, trigger, self.proactive_prune_min_reclaim_tokens)
        next_rearm_tokens = after + runway
        if session_db and session_id:
            try:
                session_db.archive_and_compact(
                    session_id, pruned,
                    model_config_patch={PROACTIVE_PRUNE_REARM_MODEL_CONFIG_KEY: next_rearm_tokens},
                )
            except Exception as exc:
                logger.warning("proactive prune: DB commit failed; keeping the original transcript: %s", exc)
                return messages, 0
            stamp_db_persisted_markers(pruned)
        self._proactive_prune_rearm_tokens = next_rearm_tokens
        self._last_reclaim_block_warn = None
        PROACTIVE_STATS[used] += 1
        if not self.quiet_mode:
            logger.info("proactive prune (%s): dropped %d tool pair(s), ~%d tokens reclaimed",
                        used, stats.get("dropped", 0), reclaimed)
        return pruned, pruned_count

    # -- FIFO floor ----------------------------------------------------------

    def _proactive_fifo(self, messages, cfgp):
        """Oldest-first greedy eviction: keep dropping eligible pairs (the
        pruner's pinned/recent contract holds) until the estimated reclaim
        reaches the target, then stop — the reclaim gate below is the authority."""
        pruner = self._proactive_pruner
        try:
            calls = pruner.collect_tool_calls(messages, int(cfgp.get("preserve_recent") or 6))
            eligible = sorted(pruner.eligible_calls(calls), key=lambda r: r["assistant_index"])
        except Exception as exc:
            logger.warning("proactive fifo: pair scan failed (%s: %s)", type(exc).__name__, exc)
            return messages, {"dropped": 0, "truncated": 0}
        if not eligible:
            return messages, {"dropped": 0, "truncated": 0}
        budget = max(0, int(cfgp.get("target_tokens") or 0))
        decisions, estimated = {}, 0
        for record in eligible:
            if budget and estimated >= budget:
                break
            decisions[record["call_id"]] = {"call": False, "result": False}
            estimated += self._tokens(record.get("result_text") or "") \
                + max(1, len(str(record.get("arguments") or "")) // 4)
        if not decisions:
            return messages, {"dropped": 0, "truncated": 0}
        return pruner.apply_decisions(messages, calls, decisions, 0)
