"""fast-compaction: lossless-by-deletion compaction overlay for Hermes.

Ports tamaratran/fast-jev-compaction to the Hermes plugin surface: instead of asking
an LLM to summarize old turns, the self-hosted classifier service (GLiNER2.5-Decide,
fistandantilus) scores every unpinned tool call/result in one pass and stale pairs are
dropped or head-truncated. Everything kept stays verbatim; user and assistant text is
never rewritten.

It registers as a ContextEngine (replacing the built-in compressor for this profile)
but is deliberately an *overlay*: it subclasses the host ContextCompressor and only
intercepts ``compress()``. If the classifier is unreachable, answers malformedly, the
reduction is below ``min_reduction``, or the state cannot be fitted, the call falls
through to the untouched built-in summary path. The built-in remains the source of
truth for token bookkeeping, thresholds, cooldowns, telemetry and the summary fallback.

Settings live under ``plugins.entries.fast-compaction.settings``; the API key is read
from the environment at call time and never stored or logged.
"""
from __future__ import annotations

import json
import logging

from . import pruner
from .client import ClassifierClient
from .proactive import (MODES, PROACTIVE_STATS, ProactivePruneMixin,
                        ProactiveUnavailable)

logger = logging.getLogger(__name__)

STATUS = {"attempts": 0, "pruned": 0, "fallbacks": 0, "fallback_reasons": {},
          "last": None}


def _engine_class():
    from agent.context_compressor import ContextCompressor

    class FastCompactionEngine(ProactivePruneMixin, ContextCompressor):
        """Built-in compressor + a classifier-pruning attempt ahead of the summary,
        and (opt-in) classifier/FIFO eviction on the host's proactive seam."""

        def __init__(self, settings, client):
            self._fc = settings
            self._fc_client = client
            cfg = self._compression_config()
            super().__init__(
                model="", threshold_percent=cfg["threshold"],
                protect_first_n=cfg["protect_first_n"], protect_last_n=cfg["protect_last_n"],
                summary_target_ratio=cfg["target_ratio"], quiet_mode=False,
                base_url="", api_key="", provider="", api_mode="",
                abort_on_summary_failure=cfg["abort_on_summary_failure"],
                model_thresholds=cfg["model_thresholds"], threshold_tokens_cap=cfg["threshold_tokens"],
                proactive_prune_tokens=cfg["proactive_prune_tokens"],
                proactive_prune_min_result_chars=cfg["proactive_prune_min_result_chars"],
                proactive_prune_min_reclaim_tokens=cfg["proactive_prune_min_reclaim_tokens"],
                min_tail_user_messages=cfg["min_tail_user_messages"], tail_mode=cfg["tail_mode"],
            )

        @property
        def name(self) -> str:
            return "fast-compaction"

        @staticmethod
        def _compression_config():
            """Mirror the host's compression defaults so the fallback path matches the
            built-in policy the operator configured (the plugin path bypasses
            _parse_compression_config; the host still applies model_thresholds, micro
            settings and update_model() to us afterwards)."""
            with_suppress = {}
            try:
                from hermes_cli.config import load_config_readonly
                with_suppress = (load_config_readonly() or {}).get("compression") or {}
            except Exception:
                logger.debug("fast-compaction: compression config unreadable; host defaults")
            def _int(key, default):
                try:
                    return int(with_suppress.get(key, default))
                except (TypeError, ValueError):
                    return default
            def _float(key, default):
                try:
                    return float(with_suppress.get(key, default))
                except (TypeError, ValueError):
                    return default
            thresholds = with_suppress.get("model_thresholds")
            return {
                "threshold": _float("threshold", 0.50),
                "protect_first_n": max(0, _int("protect_first_n", 3)),
                "protect_last_n": _int("protect_last_n", 20),
                "target_ratio": _float("target_ratio", 0.20),
                "tail_mode": str(with_suppress.get("tail_mode", "lean")).strip().lower(),
                "min_tail_user_messages": max(1, _int("min_tail_user_messages", 1)),
                "abort_on_summary_failure": bool(with_suppress.get("abort_on_summary_failure", False)),
                "model_thresholds": {str(k): float(v) for k, v in thresholds.items()
                                     if isinstance(v, (int, float)) and not isinstance(v, bool)}
                                    if isinstance(thresholds, dict) else {},
                "threshold_tokens": with_suppress.get("threshold_tokens"),
                "proactive_prune_tokens": max(0, _int("proactive_prune_tokens", 0)),
                "proactive_prune_min_result_chars": _int("proactive_prune_min_result_chars", 8000),
                "proactive_prune_min_reclaim_tokens": _int("proactive_prune_min_reclaim_tokens", 4096),
            }

        # -- token estimation (host estimator; character fallback keeps the module honest)

        def _tokens(self, text):
            try:
                from agent.model_metadata import estimate_tokens_rough
                return estimate_tokens_rough(text)
            except Exception:
                return max(1, len(text) // 4)

        # -- the overlay

        def compress(self, messages, current_tokens=None, focus_topic=None, force=False,
                     memory_context="", bypass_cooldown=False):
            if not self._fc.get("enabled", True) or focus_topic:
                return super().compress(messages, current_tokens=current_tokens,
                                        focus_topic=focus_topic, force=force,
                                        memory_context=memory_context,
                                        bypass_cooldown=bypass_cooldown)
            STATUS["attempts"] += 1
            try:
                pruned, stats = self._prune_pass(messages)
            except pruner.PruneError as exc:
                return self._fallback(str(exc), messages, current_tokens, force,
                                      memory_context, bypass_cooldown)
            except Exception as exc:  # a pruning bug must never cost a compression
                logger.warning("fast-compaction: prune pass failed: %s: %s",
                               type(exc).__name__, exc)
                return self._fallback(f"prune_error:{type(exc).__name__}", messages,
                                      current_tokens, force, memory_context, bypass_cooldown)
            reduction = self._reduction(messages, pruned)
            if stats["dropped"] + stats["truncated"] == 0 or reduction < self._fc["min_reduction"]:
                return self._fallback("insufficient_reduction", messages, current_tokens, force,
                                      memory_context, bypass_cooldown)
            self._commit(pruned, messages, current_tokens, force, stats, reduction)
            STATUS["pruned"] += 1
            STATUS["last"] = {"decisions": stats, "reduction": round(reduction, 4),
                              "state_tokens": stats.get("state_tokens")}
            if not self.quiet_mode:
                logger.info(
                    "fast-compaction: kept verbatim, dropped %d result(s), truncated %d "
                    "(%.0f%% estimated reduction, ~%d chars); summary fallback untouched",
                    stats["dropped"], stats["truncated"], reduction * 100, stats["chars_saved"])
            return pruned

        def _prune_pass(self, messages):
            calls = pruner.collect_tool_calls(messages, self._fc["preserve_recent_messages"])
            eligible = pruner.eligible_calls(calls)
            if len(eligible) < 2:
                raise pruner.PruneError("nothing_eligible")
            goal = pruner.extract_goal(messages)
            state = pruner.fit_state(messages, calls, goal, self._fc["max_state_tokens"],
                                     self._fc["max_request_tokens"], self._tokens)
            state = self._redact(state)
            decisions = {}
            for batch in pruner.build_batches(eligible):
                text, schema = pruner.build_request(batch, state)
                data = self._fc_client.post(
                    self._fc["endpoint"], self._fc["api_key_env"], text, schema,
                    timeout=self._fc["request_timeout"])
                if data is None:
                    raise pruner.PruneError("classifier_unavailable")
                decisions.update(pruner.parse_response(data, batch))
            pruned, stats = pruner.apply_decisions(messages, calls, decisions,
                                                   self._fc["truncate_head_chars"])
            stats["state_tokens"] = self._tokens(state)
            return pruned, stats

        def _redact(self, state):
            """Same egress discipline as meta-classifier: never send what the native
            scrubber cannot vouch for; a failure routes to the summary path."""
            from agent.redact import REDACTION_UNAVAILABLE, redact_for_egress
            safe = redact_for_egress(state)
            if not isinstance(safe, str) or REDACTION_UNAVAILABLE in safe:
                raise pruner.PruneError("redaction_unavailable")
            if len(safe) > pruner.MAX_TEXT:
                raise pruner.PruneError("state_too_long")
            return safe

        # -- proactive seam contract (ProactivePruneMixin) --------------------

        def _proactive_config(self):
            return {"mode": self._fc.get("proactive_prune_mode", "off"),
                    "trigger_tokens": self._fc.get("proactive_trigger_tokens", 0),
                    "target_tokens": self._fc.get("proactive_target_tokens", 0),
                    "preserve_recent": self._fc["preserve_recent_messages"]}

        _proactive_pruner = pruner

        def _proactive_classifier_prune(self, messages):
            try:
                return self._prune_pass(messages)
            except pruner.PruneError as exc:
                raise ProactiveUnavailable(str(exc))

        def _reduction(self, messages, pruned):
            from agent.model_metadata import estimate_messages_tokens_rough
            pre = estimate_messages_tokens_rough(messages)
            if pre <= 0:
                return 0.0
            return (pre - estimate_messages_tokens_rough(pruned)) / pre

        def _fallback(self, reason, messages, current_tokens, force, memory_context,
                      bypass_cooldown):
            STATUS["fallbacks"] += 1
            reasons = STATUS["fallback_reasons"]
            reasons[reason] = reasons.get(reason, 0) + 1
            if not self.quiet_mode:
                logger.info("fast-compaction: falling back to built-in summary (%s)", reason)
            return super().compress(messages, current_tokens=current_tokens, focus_topic=None,
                                    force=force, memory_context=memory_context,
                                    bypass_cooldown=bypass_cooldown)

        def _commit(self, pruned, messages, current_tokens, force, stats, reduction):
            """Mirror the built-in's commit bookkeeping for a pruned transcript.

            No summary exists for this pass: the transcript itself is the record.
            """
            from agent.context_compressor import (_prune_stale_reasoning_replay,
                                                  _strip_historical_media,
                                                  _strip_persistence_markers)
            from agent.model_metadata import estimate_messages_tokens_rough
            telemetry = self._begin_compress_attempt(current_tokens, force)
            telemetry["chunk_count"] = 1
            telemetry["commit_status"] = "pruned_verbatim"
            telemetry["failure_class"] = None
            compressed = self._sanitize_tool_pairs(pruned)
            compressed = self._reappend_inflight_user_task(
                compressed, self._find_inflight_user_task(messages))
            self.compression_count += 1
            compressed = _strip_historical_media(compressed)
            pre = estimate_messages_tokens_rough(messages)
            self._last_compression_savings_pct = reduction * 100
            if not self.quiet_mode:
                logger.info("Compressed: %d -> %d messages (~%d tokens saved, %.0f%%)",
                            len(messages), len(compressed),
                            pre - estimate_messages_tokens_rough(compressed), reduction * 100)
                logger.info("Compression #%d complete", self.compression_count)
            _strip_persistence_markers(compressed)
            _prune_stale_reasoning_replay(compressed)
            self._last_compression_made_progress = True
            self._reset_micro_compact_cursor_state()
            self._reset_proactive_prune_rearm()

    return FastCompactionEngine


def register(ctx):
    settings = {}
    for key, default in pruner.DEFAULTS.items():
        value = ctx.get_config(key, default)
        settings[key] = default if value is None else value
    settings["preserve_recent_messages"] = min(64, max(1, int(settings["preserve_recent_messages"])))
    settings["truncate_head_chars"] = min(4000, max(0, int(settings["truncate_head_chars"])))
    settings["min_reduction"] = min(0.95, max(0.05, float(settings["min_reduction"])))
    mode = str(settings.get("proactive_prune_mode") or "off").strip().lower()
    if mode not in MODES:
        logger.error("fast-compaction: unknown proactive_prune_mode %r; using off", mode)
        mode = "off"
    settings["proactive_prune_mode"] = mode
    settings["proactive_trigger_tokens"] = max(0, int(settings["proactive_trigger_tokens"]))
    settings["proactive_target_tokens"] = max(0, int(settings["proactive_target_tokens"]))
    client = ClassifierClient(timeout=settings["request_timeout"])
    engine = _engine_class()(settings, client)
    handle = ctx.register_context_engine(engine)
    if handle is None:
        logger.warning("fast-compaction: no context-engine slot (another engine is "
                       "registered); the overlay is inactive")
        return
    ctx.register_tool(
        name="fast_compaction_status", toolset="fast-compaction",
        schema={"name": "fast_compaction_status",
                "description": ("Overlay compaction diagnostics: classifier-prune attempts, "
                                "pruned/fallback counts with per-reason tallies, and the last "
                                "pass statistics. Reads state only; changes nothing."),
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: json.dumps({
            "engine": "fast-compaction", "enabled": settings["enabled"],
            "endpoint_configured": bool(settings["endpoint"]),
            "min_reduction": settings["min_reduction"],
            "proactive_mode": settings["proactive_prune_mode"],
            "proactive": dict(PROACTIVE_STATS), **STATUS}),
        check_fn=lambda: True)
