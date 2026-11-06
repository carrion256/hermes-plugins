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

Stages (default pipeline: drop_reasoning,prune,clip,drop_stale):
  drop_reasoning - deletes chain-of-thought (reasoning/reasoning_content/reasoning_details
                   and thinking blocks) from assistant turns older than the protected
                   window; pure deletion, never rewrites visible text or tool calls.
  prune       - the fast-compaction classifier keep/drop pruner (imported, not
                duplicated); most selective, needs the classifier service.
  clip        - head+tail truncation of oversized tool results; deterministic,
                catches the one case prune cannot (a single huge kept result).
  drop_stale  - positional removal of tool pairs beyond the keep window; blunt,
                runs last precisely because it is the least selective.
  summary     - the normal compaction engine itself, as a stage: the earlier stages
                reduce first and the built-in summarises what survived, so
                ``pipeline: prune,summary`` is a real chain rather than the usual
                either/or. Terminal — it keeps its own token bookkeeping, so the
                cascade returns its result instead of committing over it.

Settings live under ``plugins.entries.chain-compaction.settings``. The classifier
transport — endpoint, served model, Bearer key, breaker, cooldown — belongs to the
classifier-core plugin: this file builds no request, opens no socket and reads no
credential. A cascade that under-delivers files one gate friction (papercuts) before
handing the transcript to the built-in summary.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import logging
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol, cast

from . import stages
from .stages import StageError, parse_pipeline

logger = logging.getLogger(__name__)

# One classifier transport for the whole process: a second copy of the module would
# mean a second breaker, so one breaker serves every caller.
CLASSIFIER_CORE_PLUGIN: Final[str] = "classifier-core"
CLIENT_MODULE_KEY: Final[str] = "hermes_plugins_classifier_core"
# Distinct per plugin, so classifier_status attributes declines to the right consumer.
PRUNE_CALLER: Final[str] = "chain-compaction"

PAPERCUTS_PLUGIN: Final[str] = "papercuts"
FRICTION_MODULE_KEY: Final[str] = "hermes_plugins_papercuts_friction"
FRICTION_KIND: Final[str] = "summary_backstop"
FRICTION_SUMMARY: Final[str] = "chain-compaction fell through to the built-in summary"
# The stage trace is evidence, not an archive: it is bounded before it leaves here.
FRICTION_DETAIL_LIMIT: Final[int] = 1000
# The one under-delivery reason: every stage missed or stayed below target, so the
# untouched built-in summary becomes the backstop. Same string in the miss tally and
# as the friction identity, so the two records join.
CASCADE_INSUFFICIENT: Final[str] = "cascade_insufficient"

STATUS = {"attempts": 0, "committed": 0, "fallbacks": 0, "stage_misses": {},
          "friction": {"reports": 0, "last_reason": None, "unavailable": 0},
          "last": None}


class _QuestionClient(Protocol):
    """The one method this plugin needs from the shared classifier client."""

    def ask(self, state: str, questions: dict[str, object], *, caller: str,
            timeout: float | None = None) -> dict[str, object] | None: ...


class _SettingsClass(Protocol):
    """``ClassifierSettings``: parsed once, at register, through the host config reader."""

    def from_config(self, get_config: Callable[[str, object], object]) -> object: ...


class _ClientModule(Protocol):
    """The path-loaded surface of ``classifier-core/client.py`` this plugin depends on."""

    ClassifierSettings: _SettingsClass

    def shared_client(self, settings: object) -> _QuestionClient: ...


class _FrictionReceipt(Protocol):
    """``FrictionReceipt`` as this plugin reads it."""

    reason: str


class _FrictionModule(Protocol):
    """The path-loaded surface of ``papercuts/friction.py`` this plugin calls."""

    GateFriction: Callable[..., object]

    def report(self, friction: object) -> _FrictionReceipt: ...


# Stages the engine owns (need I/O or cross-plugin imports) as opposed to the
# pure stages in stages.py; both are valid pipeline names. `summary` is the normal
# compaction engine itself, run as the pipeline's terminal step.
ENGINE_STAGES = ("prune", "summary")

# Kept in sync with fast-compaction/proactive.MODES; defined locally so register
# works even when the sibling plugin is absent.
_PROACTIVE_MODES = ("off", "fifo", "classifier")


def _load_module_by_path(key: str, path: Path, missing: str) -> object | None:
    """Exec ``path`` under the shared registry name ``key``, reusing a live module.

    The module is registered BEFORE execution: a frozen dataclass resolves its string
    annotations through ``sys.modules``, and a shared-state registration inside the
    module depends on that entry existing too. A half-built module is never left shared.
    """
    shared = sys.modules.get(key)
    if isinstance(shared, types.ModuleType):
        return shared
    if not path.is_file():
        logger.warning("chain-compaction: %s", missing)
        return None
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        logger.warning("chain-compaction: %s is not loadable", path.name)
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        sys.modules.pop(key, None)
        logger.warning("chain-compaction: %s unusable (%s: %s)", path.name,
                       type(exc).__name__, exc)
        return None
    return module


def _load_classifier_core() -> _ClientModule | None:
    """Path-load classifier-core's client, preferring the process-wide registration.

    Returns ``None`` when the sibling plugin is absent or unloadable; the prune stage
    then misses with ``prune_unavailable:classifier_core`` instead of raising.
    """
    module = _load_module_by_path(
        CLIENT_MODULE_KEY,
        Path(__file__).resolve().parent.parent / CLASSIFIER_CORE_PLUGIN / "client.py",
        "classifier-core is not installed; the prune stage cannot run")
    return None if module is None else cast(_ClientModule, module)


def _shared_classifier_client() -> _QuestionClient | None:
    """The process-shared client for classifier-core's endpoint, or ``None``.

    classifier-core's own settings are read through the host config reader, once, here:
    a consumer never declares an endpoint, a model or a key environment variable.
    """
    module = _load_classifier_core()
    if module is None:
        return None
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        config = load_config_readonly()

        def read(key: str, default: object = None) -> object:
            return cfg_get(config, "plugins", "entries", CLASSIFIER_CORE_PLUGIN,
                           "settings", key, default=default)

        return module.shared_client(module.ClassifierSettings.from_config(read))
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning("chain-compaction: classifier-core settings unusable (%s: %s)",
                       type(exc).__name__, exc)
        return None


def _load_friction() -> _FrictionModule | None:
    """Path-load papercuts' friction sink, preferring the process-wide registration.

    Absent sibling: ``None``, counted as ``friction_unavailable``. A missing sink must
    never cost a compression — the backstop stays visible in STATUS either way.
    """
    module = _load_module_by_path(
        FRICTION_MODULE_KEY,
        Path(__file__).resolve().parent.parent / PAPERCUTS_PLUGIN / "friction.py",
        "papercuts is not installed; summary backstops will not be filed")
    return None if module is None else cast(_FrictionModule, module)


def _load_fast_compaction():
    """Reuse the fast-compaction pruner by path — one keep/drop policy in the repo,
    not two. Absent installation: prune is a miss. The classifier transport is not
    loaded here: it comes from classifier-core, shared with every other consumer."""
    path = Path(__file__).resolve().parent.parent / "fast-compaction" / "pruner.py"
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("chaincomp_pruner", path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load fast-compaction/pruner.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as exc:  # an absent or broken sibling is a miss, never a crash
        logger.warning("chain-compaction: fast-compaction pruner unusable (%s)", exc)
        return None


def _load_proactive():
    """The proactive mixin lives with fast-compaction (one host-seam policy).
    Absent installation: a no-op base, so the engine keeps host behaviour."""
    path = Path(__file__).resolve().parent.parent / "fast-compaction" / "proactive.py"
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("chaincomp_proactive", path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load fast-compaction/proactive.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        logger.warning("chain-compaction: proactive module unusable (%s)", exc)
        return None


def _engine_class():
    from agent.context_compressor import ContextCompressor

    proactive = _load_proactive()

    class _NoopProactive:  # fast-compaction absent: keep host behaviour entirely
        pass

    Mixin = proactive.ProactivePruneMixin if proactive else _NoopProactive
    ProactiveUnavailable = getattr(proactive, "ProactiveUnavailable", Exception)
    PROACTIVE_STATS = getattr(proactive, "PROACTIVE_STATS", {})

    class ChainCompactionEngine(Mixin, ContextCompressor):  # pyright: ignore[reportGeneralTypeIssues]
        """Built-in compressor + an ordered stage cascade ahead of the summary,
        and (opt-in) classifier/FIFO eviction on the host's proactive seam."""

        def __init__(self, settings, pruner, client, external=None):
            self._cc = settings
            self._pruner = pruner
            self._cc_client = client
            # Composition seam: stage modules named by `stage_modules`, resolved by name in
            # the pipeline like the built-in ones. chain-compaction stays the overlay; any
            # strategy that honours run_stage(messages, cfg, tokens) plugs in without
            # touching this file.
            self._external = dict(external or {})
            self._pipeline = parse_pipeline(
                settings["pipeline"], extra=ENGINE_STAGES + tuple(self._external))
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

        def __deepcopy__(self, memo):
            """clone_for_agent() deep-copies the registered engine, and the prune
            stage holds the fast-compaction *module*, which copy cannot clone —
            the host would then silently install the built-in compressor and the
            cascade would never run. Share stateless module references and the
            process-wide classifier client (copying it would fork its breaker and
            its lock); copy only the per-agent mutable state."""
            clone = self.__class__.__new__(self.__class__)
            memo[id(self)] = clone
            for key, value in self.__dict__.items():
                shared = isinstance(value, types.ModuleType) or key == "_cc_client"
                setattr(clone, key, value if shared else copy.deepcopy(value, memo))
            return clone

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
            # A pipeline that ends in `summary` is a chain, not a race: the tail is the
            # point, so an earlier stage reaching target_reduction must not commit and
            # skip the built-in.
            terminal_summary = bool(self._pipeline) and self._pipeline[-1] == "summary"
            for name in self._pipeline:
                try:
                    outcome, stats = self._run_stage(
                        name, candidate, current_tokens=current_tokens, force=force,
                        memory_context=memory_context, bypass_cooldown=bypass_cooldown)
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
                if name == "summary":
                    # Terminal: the parent compressor already booked this pass, so its
                    # result IS the commit. Committing again would double-count.
                    STATUS["committed"] += 1
                    STATUS["last"] = {"trace": trace, "reduction": round(reduction, 4)}
                    if not self.quiet_mode:
                        logger.info("chain-compaction: summarised after %d stage(s)",
                                    len([t for t in trace if "reduction" in t]))
                    return outcome
                if own < self._cc["stage_min_reduction"]:
                    # contributed nothing meaningful: keep the candidate (no bytes lost)
                    # but continue to the next stage — "not substantial enough" is the
                    # documented continue trigger, not a failure.
                    self._miss(f"{name}:below_stage_min")
                    continue
                candidate = outcome
                if terminal_summary:
                    continue
                if reduction >= self._cc["target_reduction"]:
                    candidate = self._commit(candidate, messages, current_tokens, force,
                                             trace, reduction)
                    STATUS["committed"] += 1
                    STATUS["last"] = {"trace": trace, "reduction": round(reduction, 4)}
                    if not self.quiet_mode:
                        logger.info("chain-compaction: committed after %d stage(s) "
                                    "(%.0f%% estimated reduction)",
                                    len([t for t in trace if "reduction" in t]), reduction * 100)
                    return candidate
            # The cascade under-delivered, so the untouched built-in summary is the
            # backstop. That is a gate refusal with evidence attached: file it (papercuts
            # owns the policy) BEFORE the handoff, where the trace still exists.
            self._report_summary_backstop(CASCADE_INSUFFICIENT, trace)
            return self._fallback(CASCADE_INSUFFICIENT, messages, current_tokens, force,
                                  memory_context, bypass_cooldown)

        def _own_delta(self, before, after):
            from agent.model_metadata import estimate_messages_tokens_rough
            pre = estimate_messages_tokens_rough(before)
            if pre <= 0:
                return 0.0
            return (pre - estimate_messages_tokens_rough(after)) / pre

        def _run_stage(self, name, messages, **kwargs):
            cfg = self._cc
            if name == "prune":
                return self._stage_prune(messages, cfg)
            if name == "summary":
                return self._stage_summary(messages, **kwargs)
            external = self._external.get(name)
            if external is not None:
                return external(messages, cfg, self._tokens)
            return stages.run_stage(name, messages, cfg, self._tokens)

        def _stage_prune(self, messages, cfg):
            """fast-compaction's proven prune pass, as one cascade stage."""
            pruner, client = self._pruner, self._cc_client
            if pruner is None:
                raise StageError("prune_unavailable:not_installed")
            if client is None:
                # classifier-core is the stage's only transport; with it unavailable the
                # stage misses rather than guessing an endpoint of its own.
                raise StageError("prune_unavailable:classifier_core")
            calls = pruner.collect_tool_calls(messages, cfg["preserve_recent_messages"])
            # The pin set is applied to the returned records rather than through the
            # pruner's signature: chain-compaction must honour its own setting whatever
            # implementation is loaded (pruner.py, a test stand-in, a future port), and
            # eligible_calls/apply_decisions already skip "pinned".
            pinned = stages.pinned_names(cfg)
            if pinned:
                for record in calls.values():
                    if record.get("tool") in pinned:
                        record["pinned"] = True
            eligible = pruner.eligible_calls(calls)
            if len(eligible) < 2:
                raise StageError("prune_nothing_eligible")
            goal = pruner.extract_goal(messages)
            prune_cfg = dict(pruner.DEFAULTS)
            # Precedence for the delete threshold: pruner default < this plugin's
            # configured drop_confidence < an explicit prune_defaults override.
            prune_cfg["drop_confidence"] = cfg.get(
                "drop_confidence", prune_cfg.get("drop_confidence", 0.5))
            prune_cfg.update(cfg.get("prune_defaults") or {})
            state = pruner.fit_state(messages, calls, goal, prune_cfg["max_state_tokens"],
                                     prune_cfg["max_request_tokens"], self._tokens)
            state = self._redact(state)
            decisions = {}
            for batch in pruner.build_batches(eligible):
                text, questions = pruner.build_request(batch, state)
                # The timeout is passed explicitly, as before: this plugin's
                # request_timeout is the budget, not the transport's own default.
                data = client.ask(text, questions, caller=PRUNE_CALLER,
                                  timeout=float(cfg["request_timeout"]))
                if data is None:
                    raise StageError("prune_classifier_unavailable")
                decisions.update(pruner.parse_response(data, batch,
                                                       prune_cfg["drop_confidence"]))
            pruned, stats = pruner.apply_decisions(messages, calls, decisions,
                                                   prune_cfg["truncate_head_chars"])
            stats["stage"] = "prune"
            return pruned, stats

        def _stage_summary(self, messages, current_tokens=None, force=False,
                           memory_context="", bypass_cooldown=False, **_kw):
            """The normal compaction engine, applied to the cascade's own output.

            The under-delivery fallback hands the ORIGINAL transcript to the built-in, so
            pruning and summarising never compose — one or the other runs. Here the parent
            summarises what the earlier stages left behind, which is what makes
            ``prune,summary`` a chain. It is terminal and self-booking (the parent keeps
            its own token bookkeeping), so ``compress`` returns its result directly
            instead of committing a second time.
            """
            compressed = super().compress(
                messages, current_tokens=current_tokens, focus_topic=None,
                # The cascade has already decided this pass must compact, so the
                # parent's own threshold and cooldown checks would only veto it.
                force=True, memory_context=memory_context, bypass_cooldown=True)
            if not isinstance(compressed, list) or not compressed:
                raise StageError("summary_unavailable")
            if compressed == messages:
                raise StageError("summary_no_change")
            return compressed, {"stage": "summary"}

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

        # -- proactive seam contract (ProactivePruneMixin) --------------------

        def _proactive_config(self):
            return {"mode": self._cc.get("proactive_prune_mode", "off"),
                    "trigger_tokens": self._cc.get("proactive_trigger_tokens", 0),
                    "target_tokens": self._cc.get("proactive_target_tokens", 0),
                    "preserve_recent": self._cc["preserve_recent_messages"],
                    "pinned_tools": stages.pinned_names(self._cc)}

        @property
        def _proactive_pruner(self):
            return self._pruner

        def _proactive_classifier_prune(self, messages):
            """The cascade's own prune stage on the host's proactive seam: the same
            transport, the same caller, the same reasons. Any stage miss degrades this
            turn to the FIFO floor."""
            try:
                return self._stage_prune(messages, self._cc)
            except StageError as exc:
                raise ProactiveUnavailable(str(exc)) from exc

        def _fallback(self, reason, messages, current_tokens, force, memory_context,
                      bypass_cooldown):
            STATUS["fallbacks"] += 1
            self._miss(f"fallback:{reason}")
            if not self.quiet_mode:
                logger.info("chain-compaction: falling back to built-in summary (%s)", reason)
            return super().compress(messages, current_tokens=current_tokens, focus_topic=None,
                                    force=force, memory_context=memory_context,
                                    bypass_cooldown=bypass_cooldown)

        def _report_summary_backstop(self, reason: str, trace: list[dict[str, object]]) -> None:
            """File the under-delivery as gate friction before the built-in summary runs.

            papercuts decides whether a refusal becomes anything; this plugin only reports
            what it observed, with the stage trace as bounded evidence. The receipt reason
            is kept in STATUS so ``chain_compaction_status`` shows what the last backstop
            did, and a missing sink is counted (``unavailable``) rather than fatal.
            """
            tally = STATUS["friction"]
            friction = _load_friction()
            if friction is None:
                tally["unavailable"] += 1
                return
            detail = json.dumps(trace, sort_keys=True, default=str)[:FRICTION_DETAIL_LIMIT]
            try:
                receipt = friction.report(friction.GateFriction(
                    source="chain-compaction", kind=FRICTION_KIND, identity=reason,
                    summary=FRICTION_SUMMARY, detail=detail,
                    session_id=str(getattr(self, "_session_id", "") or "")))
            except Exception:  # a foreign sink is untrusted code, and a papercut that
                # refuses to file must never cost a compression; the traceback is logged
                # so the refusal itself stays diagnosable.
                logger.exception("chain-compaction: summary backstop friction not filed")
                tally["last_reason"] = "error"
                return
            tally["reports"] += 1
            tally["last_reason"] = receipt.reason

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
            return compressed

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
    settings["drop_confidence"] = min(1.0, max(0.0, float(settings["drop_confidence"])))
    external, stage_problems = stages.load_stage_modules(
        ctx.get_config("stage_modules", []),
        reserved=tuple(stages.STAGES) + ENGINE_STAGES)
    for problem in stage_problems:
        logger.error("chain-compaction: stage module skipped (%s)", problem)
    try:
        parse_pipeline(settings["pipeline"], extra=ENGINE_STAGES + tuple(external))
    except ValueError as exc:
        logger.error("chain-compaction: bad pipeline config (%s); using default order", exc)
        settings["pipeline"] = "drop_reasoning,prune,clip,drop_stale"
    mode = str(settings.get("proactive_prune_mode") or "off").strip().lower()
    if mode not in _PROACTIVE_MODES:
        logger.error("chain-compaction: unknown proactive_prune_mode %r; using off", mode)
        mode = "off"
    settings["proactive_prune_mode"] = mode
    settings["proactive_trigger_tokens"] = max(0, int(settings["proactive_trigger_tokens"]))
    settings["proactive_target_tokens"] = max(0, int(settings["proactive_target_tokens"]))
    pruner = _load_fast_compaction()
    # One pin parser, kept with the fast-compaction pruner and loaded by path: a
    # malformed value is a TypeError/ValueError at register rather than a pin silently
    # doing nothing. fast-compaction absent (no pruner) is the documented degraded path
    # where only the pure stages can still run, so the same set is built leniently there.
    if pruner is not None:
        settings["pinned_tools"] = pruner.parse_pinned_tools(settings.get("pinned_tools"))
    else:
        settings["pinned_tools"] = stages.pinned_names(settings)
    proactive_mod = _load_proactive()
    # Resolved once: the shared client is keyed by classifier-core's endpoint, so the
    # prune stage and every other consumer in the process share one breaker.
    client = _shared_classifier_client()
    if client is None:
        logger.warning("chain-compaction: classifier-core unavailable; the prune stage "
                       "will miss with prune_unavailable:classifier_core")
    engine = _engine_class()(settings, pruner, client, external)
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
                                "tallies, whether the classifier transport and the "
                                "papercuts friction sink resolved, the summary-backstop "
                                "friction counters (reports, last_reason, unavailable), "
                                "and the last trace. Reads state only; changes nothing."),
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: json.dumps({
            "engine": "chain-compaction", "enabled": settings["enabled"],
            "pipeline": engine._pipeline,
            "external_stages": sorted(external),
            "target_reduction": settings["target_reduction"],
            "stage_min_reduction": settings["stage_min_reduction"],
            "drop_confidence": settings["drop_confidence"],
            "proactive_mode": settings["proactive_prune_mode"],
            "proactive": dict(getattr(proactive_mod, "PROACTIVE_STATS", {})),
            "prune_available": pruner is not None,
            "classifier_core_available": client is not None, **STATUS}),
        check_fn=lambda: True)
