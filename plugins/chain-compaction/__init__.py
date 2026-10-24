"""chain-compaction: ordered cascade of compaction methods with a summary backstop.

Registers as the ContextEngine (replacing the built-in compressor for this
profile) and orchestrates pure stages from ``stages.py`` in configured order:
each stage gets the current candidate transcript; if the stage fails outright
(classifier down, nothing to do) or its contribution leaves the cumulative
reduction below ``target_reduction``, the next stage runs on top. The moment
the cumulative reduction is substantial enough, the cascade commits verbatim
output and stops; if the whole pipeline under-delivers, the call falls through
to the untouched built-in summary. The built-in remains the source of truth
for token bookkeeping, thresholds, cooldowns and telemetry — same overlay
discipline as fast-compaction.

Stages (default pipeline: prune,clip,drop_stale):
  prune       - the fast-compaction classifier keep/drop pruner (imported, not
                duplicated); most selective, needs the classifier service.
  clip        - head+tail truncation of oversized tool results; deterministic,
                catches the one case prune cannot (a single huge kept result).
  drop_stale  - positional removal of tool pairs beyond the keep window; blunt,
                runs last precisely because it is the least selective.

Settings live under ``plugins.entries.chain-compaction.settings``; the API key
is read from the environment at call time and never stored or logged.
"""
from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

from . import stages
from .stages import StageError, parse_pipeline

logger = logging.getLogger(__name__)

STATUS = {"attempts": 0, "committed": 0, "fallbacks": 0, "stage_misses": {},
          "last": None}

# Stages the engine owns (need I/O or cross-plugin imports) as opposed to the
# pure stages in stages.py; both are valid pipeline names.
ENGINE_STAGES = ("prune",)


def _load_fast_compaction():
    """Reuse the fast-compaction pruner/client by path — one classifier
    implementation in the repo, not two. Absent installation: prune is a miss."""
    fc_dir = Path(__file__).resolve().parent.parent / "fast-compaction"
    if not (fc_dir / "pruner.py").is_file():
        return None, None
    try:
        def load(name):
            spec = importlib.util.spec_from_file_location(
                f"chaincomp_{name}", fc_dir / f"{name}.py")
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load fast-compaction/{name}.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        return load("pruner"), load("client")
    except Exception as exc:
        logger.warning("chain-compaction: fast-compaction pruner unusable (%s)", exc)
        return None, None


def _engine_class():
    from agent.context_compressor import ContextCompressor

    class ChainCompactionEngine(ContextCompressor):
        """Built-in compressor + an ordered stage cascade ahead of the summary."""

        def __init__(self, settings, pruner, client):
            self._cc = settings
            self._pruner = pruner
            self._cc_client = client
            self._pipeline = parse_pipeline(settings["pipeline"], extra=ENGINE_STAGES)
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
            return "chain-compaction"

        @staticmethod
        def _compression_config():
            """Mirror the host's compression defaults so the built-in fallback path
            matches the operator's configured policy (same contract as
            fast-compaction; the host still applies model_thresholds and
            update_model() to us afterwards)."""
            with_suppress = {}
            try:
                from hermes_cli.config import load_config_readonly
                with_suppress = (load_config_readonly() or {}).get("compression") or {}
            except Exception:
                logger.debug("chain-compaction: compression config unreadable; host defaults")
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

        def _tokens(self, text):
            try:
                from agent.model_metadata import estimate_tokens_rough
                return estimate_tokens_rough(text)
            except Exception:
                return max(1, len(text) // 4)

        def _base_reduction(self, baseline_tokens, candidate):
            from agent.model_metadata import estimate_messages_tokens_rough
            if baseline_tokens <= 0:
                return 0.0
            return (baseline_tokens - estimate_messages_tokens_rough(candidate)) / baseline_tokens

        # -- the cascade ------------------------------------------------------------

        def compress(self, messages, current_tokens=None, focus_topic=None, force=False,
                     memory_context="", bypass_cooldown=False):
            if not self._cc["enabled"] or focus_topic:
                return super().compress(messages, current_tokens=current_tokens,
                                        focus_topic=focus_topic, force=force,
                                        memory_context=memory_context,
                                        bypass_cooldown=bypass_cooldown)
            STATUS["attempts"] += 1
            try:
                from agent.model_metadata import estimate_messages_tokens_rough
                baseline = estimate_messages_tokens_rough(messages)
            except Exception as exc:
                logger.warning("chain-compaction: baseline estimate failed: %s", exc)
                return self._fallback("baseline_unavailable", messages, current_tokens,
                                      force, memory_context, bypass_cooldown)
            candidate, trace = messages, []
            for name in self._pipeline:
                try:
                    outcome, stats = self._run_stage(name, candidate)
                except StageError as exc:
                    trace.append({"stage": name, "miss": str(exc)})
                    self._miss(f"{name}:{exc}")
                    continue
                except Exception as exc:  # a stage bug is a miss, never a lost compression
                    logger.warning("chain-compaction: stage %s crashed: %s: %s",
                                   name, type(exc).__name__, exc)
                    trace.append({"stage": name, "miss": f"crash:{type(exc).__name__}"})
                    self._miss(f"{name}:crash")
                    continue
                reduction = self._base_reduction(baseline, outcome)
                own = self._own_delta(candidate, outcome)
                trace.append({"stage": name, "reduction": round(reduction, 4), **stats})
                if own < self._cc["stage_min_reduction"]:
                    # contributed nothing meaningful: keep the candidate (no bytes lost)
                    # but continue to the next stage — "not substantial enough" is the
                    # documented continue trigger, not a failure.
                    self._miss(f"{name}:below_stage_min")
                    continue
                candidate = outcome
                if reduction >= self._cc["target_reduction"]:
                    self._commit(candidate, messages, current_tokens, force, trace, reduction)
                    STATUS["committed"] += 1
                    STATUS["last"] = {"trace": trace, "reduction": round(reduction, 4)}
                    if not self.quiet_mode:
                        logger.info("chain-compaction: committed after %d stage(s) "
                                    "(%.0f%% estimated reduction)",
                                    len([t for t in trace if "reduction" in t]), reduction * 100)
                    return candidate
            return self._fallback("cascade_insufficient", messages, current_tokens, force,
                                  memory_context, bypass_cooldown)

        def _own_delta(self, before, after):
            from agent.model_metadata import estimate_messages_tokens_rough
            pre = estimate_messages_tokens_rough(before)
            if pre <= 0:
                return 0.0
            return (pre - estimate_messages_tokens_rough(after)) / pre

        def _run_stage(self, name, messages):
            cfg = self._cc
            if name == "prune":
                return self._stage_prune(messages, cfg)
            return stages.run_stage(name, messages, cfg, self._tokens)

        def _stage_prune(self, messages, cfg):
            """fast-compaction's proven prune pass, as one cascade stage."""
            pruner, client = self._pruner, self._cc_client
            if pruner is None or client is None:
                raise StageError("prune_unavailable:not_installed")
            key_env = str(cfg.get("api_key_env") or "CLASSIFIER_API_KEY")
            import os
            if not os.environ.get(key_env, "").strip():
                raise StageError("prune_unavailable:no_key")
            calls = pruner.collect_tool_calls(messages, cfg["preserve_recent_messages"])
            eligible = pruner.eligible_calls(calls)
            if len(eligible) < 2:
                raise StageError("prune_nothing_eligible")
            goal = pruner.extract_goal(messages)
            prune_cfg = dict(pruner.DEFAULTS)
            prune_cfg.update(cfg.get("prune_defaults") or {})
            state = pruner.fit_state(messages, calls, goal, prune_cfg["max_state_tokens"],
                                     prune_cfg["max_request_tokens"], self._tokens)
            state = self._redact(state)
            decisions = {}
            for batch in pruner.build_batches(eligible):
                text, schema = pruner.build_request(batch, state)
                data = client.post(cfg["endpoint"], key_env, text, schema,
                                   timeout=float(cfg["request_timeout"]))
                if data is None:
                    raise StageError("prune_classifier_unavailable")
                decisions.update(pruner.parse_response(data, batch))
            pruned, stats = pruner.apply_decisions(messages, calls, decisions,
                                                   prune_cfg["truncate_head_chars"])
            stats["stage"] = "prune"
            return pruned, stats

        def _redact(self, state):
            from agent.redact import REDACTION_UNAVAILABLE, redact_for_egress
            safe = redact_for_egress(state)
            if not isinstance(safe, str) or REDACTION_UNAVAILABLE in safe:
                raise StageError("prune_redaction_unavailable")
            if len(safe) > pruner_max_text(self._pruner):
                raise StageError("prune_state_too_long")
            return safe

        def _miss(self, reason):
            misses = STATUS["stage_misses"]
            misses[reason] = misses.get(reason, 0) + 1

        def _fallback(self, reason, messages, current_tokens, force, memory_context,
                      bypass_cooldown):
            STATUS["fallbacks"] += 1
            self._miss(f"fallback:{reason}")
            if not self.quiet_mode:
                logger.info("chain-compaction: falling back to built-in summary (%s)", reason)
            return super().compress(messages, current_tokens=current_tokens, focus_topic=None,
                                    force=force, memory_context=memory_context,
                                    bypass_cooldown=bypass_cooldown)

        def _commit(self, candidate, messages, current_tokens, force, trace, reduction):
            """Mirror the built-in's commit bookkeeping for a cascade result.
            No summary exists for this pass: the transcript itself is the record."""
            from agent.context_compressor import (_prune_stale_reasoning_replay,
                                                  _strip_historical_media,
                                                  _strip_persistence_markers)
            telemetry = self._begin_compress_attempt(current_tokens, force)
            telemetry["chunk_count"] = 1
            telemetry["commit_status"] = "cascade_verbatim"
            telemetry["failure_class"] = None
            compressed = self._sanitize_tool_pairs(candidate)
            compressed = self._reappend_inflight_user_task(
                compressed, self._find_inflight_user_task(messages))
            self.compression_count += 1
            compressed = _strip_historical_media(compressed)
            self._last_compression_savings_pct = reduction * 100
            if not self.quiet_mode:
                logger.info("chain-compaction: Compression #%d complete (%d stages)",
                            self.compression_count, len(trace))
            _strip_persistence_markers(compressed)
            _prune_stale_reasoning_replay(compressed)
            self._last_compression_made_progress = True
            self._reset_micro_compact_cursor_state()
            self._reset_proactive_prune_rearm()

    return ChainCompactionEngine


def pruner_max_text(pruner):
    return getattr(pruner, "MAX_TEXT", 100_000) if pruner is not None else 100_000


def register(ctx):
    settings = {}
    for key, default in stages.DEFAULTS.items():
        value = ctx.get_config(key, default)
        settings[key] = default if value is None else value
    settings["target_reduction"] = min(0.95, max(0.05, float(settings["target_reduction"])))
    settings["stage_min_reduction"] = min(0.9, max(0.0, float(settings["stage_min_reduction"])))
    try:
        parse_pipeline(settings["pipeline"], extra=ENGINE_STAGES)
    except ValueError as exc:
        logger.error("chain-compaction: bad pipeline config (%s); using default order", exc)
        settings["pipeline"] = "prune,clip,drop_stale"
    pruner, client_mod = _load_fast_compaction()
    client = client_mod.ClassifierClient(timeout=settings["request_timeout"]) if client_mod else None
    engine = _engine_class()(settings, pruner, client)
    handle = ctx.register_context_engine(engine)
    if handle is None:
        logger.warning("chain-compaction: no context-engine slot (another engine is "
                       "registered); the cascade is inactive")
        return
    ctx.register_tool(
        name="chain_compaction_status", toolset="chain-compaction",
        schema={"name": "chain_compaction_status",
                "description": ("Cascade compaction diagnostics: configured stage order, "
                                "thresholds, attempts/commits/fallbacks, per-stage miss "
                                "tallies, and the last trace. Reads state only; changes nothing."),
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: json.dumps({
            "engine": "chain-compaction", "enabled": settings["enabled"],
            "pipeline": engine._pipeline,
            "target_reduction": settings["target_reduction"],
            "stage_min_reduction": settings["stage_min_reduction"],
            "prune_available": pruner is not None, **STATUS}),
        check_fn=lambda: True)
