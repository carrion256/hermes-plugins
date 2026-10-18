# 07 turn resilience
Status: proposed · Owner: — · Depends on: none for P0 (observer-only); host seam for P1 (stream consumer, `agent/chat_completion_helpers.py`); boundary with loop-guard stated in §Overlap

## Goal
Make one assistant turn recover from degenerate model output and transport stalls without the user seeing a stuck turn, and without spending a general retry budget on failures that need a different response. Hermes already handles empty streams, stale streams, and `Retry-After`. The gap is degenerate text repeated inside a single response, which Hermes does not detect, and the lack of a retry budget separated by failure class. Tool-call loops belong to loop-guard and are out of scope.

## Evidence
- **Grok, source-backed spec** (`re/docs/grok-build-harness.md`):
  - `:299-300` request headers: `x-grok-doom-loop-check` (window 512..=4096 tokens, default 1024) and `x-grok-exact-repetition-check: 64`. The detection runs server-side; the client only asks for it.
  - `:303` SSE order: per frame, doom-loop events are intercepted before stream-error parsing and typed decode.
  - `:307` stream idle abort after 300 s.
  - `:323` `classify_error` order (`SAMP/retry.rs:95-163`): auth 401 emits; encrypted-content 400 emits; `max_retries==0` is fatal; 413 triggers image strip; `x-should-retry:false` or context-length is fatal; a doom-loop result retries on its own budget (`doom_max_retries` 2, 0–250 ms jitter); 429 is fatal at an attempt threshold, otherwise backs off by `Retry-After` or jitter.
  - `:325` general backoff `min(2000 ms << (n-1), 30 000 ms)` with ±20% jitter; transport rebuild 200 ms.
- **droid, observed and source** (`droid/notes/DEEP-FINDINGS.md:129`): loop guards are identical-tool-call stop (`AgentLoop.ts:6747-6769`), output-degeneration resampling, and todo and completion reminders. Thresholds live in `apps/cli/src/hooks/constants.ts`; those values were not read for this plan and must not be copied.
- **droid retry policy** (`/var/lib/hermes/projects/droid/extracted/bunfs/sources/apps/cli/src/utils/retryPolicy.ts:1-170`): `DEFAULT_BASE_DELAY=1000`, `NON_INTERACTIVE_BASE_DELAY=2000`, `TIMEOUT_BASE_DELAY=3000` with `TIMEOUT_MAX_DELAY=15000`, `THROTTLE_MIN_DELAY=5000` with `THROTTLE_MAX_DELAY=30000`, `SINGLE_PROVIDER_CAPACITY_RETRIES=10`. Default retries: NoRetry→1, NonInteractive→5, otherwise 4 (`:46-51`).
- **Hermes, verified by read**:
  - Stale stream: `agent/chat_completion_helpers.py:576` `HERMES_STREAM_STALE_GIVEUP` (default 5); `:573-583` `_check_stale_giveup` raises `RuntimeError` past the threshold; `:2414` `_on_stale`; `:2433-2436` otherwise ends this call with `TimeoutError` and the streak carries forward.
  - Stale timeout config: `hermes_cli/timeouts.py:38` `get_provider_stale_timeout`; `_configured_timeout` at `:12-30` (per-model key beats provider key). The numeric default was not captured; P0 records it.
  - Empty response: `agent/empty_response_guard.py:49-73` budgets 3 (default) and 1 (reduced), cost threshold $0.25; `:59-63` state is scoped to one consecutive empty streak; `:182-187` history is cleared at the first empty.
  - Backoff: `agent/retry_utils.py:112` `jittered_backoff(attempt, base_delay=5.0, max_delay=120.0)`, reached via `agent/conversation_loop.py:1658`.
  - `Retry-After`: `agent/turn_recovery.py:1019-1075` `compute_error_backoff` honours it on every retryable error (`:1032-1035`), capped at 600 s; waits over 60 s are surfaced (`:1076-1080`). Call site `agent/turn_api_error.py:362-365`.
  - Error classes: `agent/error_classifier.py:26-70` `FailoverReason`.
  - Classification hook: `transform_api_error_classification` (`hermes_cli/plugins.py:141`, `:148`, `:2173`, `:2189`); listed in `SHELL_UNSUPPORTED_HOOKS` (`:226`).
  - Repetition: `agent/repetition_guard.py:1-40` checks **truncated** (`finish_reason=length`) responses only (`MIN_FRAGMENT_LENGTH=400`, `_REPEAT_WINDOW=60`, `_MIN_REPEAT_COUNT=5`, `_DOMINANCE_RATIO=0.5`). It is not a doom-loop detector and this plan must not cite it as one.
  - Stream observers: `agent/plugin_stream_hooks.py` runs per-consumer bounded queues (1024, drop-oldest); `has_stream_observer_hooks` at `:139-147`.

## Current state
Already covered, do not rebuild:
- Empty streams: budgeted resample with a cost cap (`empty_response_guard.py:49-73`).
- Stale streams: streak give-up (`chat_completion_helpers.py:573-583`, `:2433-2436`).
- `Retry-After` and jittered backoff (`turn_recovery.py:1032-1035`, `retry_utils.py:112`).

Verified gaps:
- No detector for degenerate text inside one response. `repetition_guard` runs only on truncated output.
- No separate budget for a "resample because output degenerated" failure. Any such resample would consume the empty-response or stale-streak counters, which exist for other causes.
- No Hermes equivalent of droid's output-degeneration resampling (`DEEP-FINDINGS.md:129`).

### Overlap with loop-guard (explicit)
Loop-guard owns **tool-call** repetition and its existing cycle detection (`plugins/loop-guard/__init__.py`; tests in `tests/test_loop_recovery.py`, e.g. `test_short_cycles_detected_but_polling_and_progress_are_not`). Plan 07 owns **degeneration of assistant text within one response** and the resample of that request.

Boundary rule:
- If the degenerate span is a tool call, plan 07 does nothing and loop-guard handles it.
- If the degenerate span is prose or reasoning text, plan 07 owns it.
- Loop-guard's existing tests must pass unchanged with plan 07 enabled. Plan 07 must not add a second tool-call detector.

## Design
1. **Degeneration detector.** A pure function over a rolling window of streamed assistant text. It flags when one fragment of at least `N` characters repeats at least `K` times within the last `W` characters. Pinned defaults: `N = 400` (Hermes `repetition_guard.MIN_FRAGMENT_LENGTH`, the only in-repo precedent; Grok's spec has no fragment-length parameter), `K = 8` (the one Grok sampler config setting `max_threshold: 8`, `xai-grok-sampler/src/config.rs:247-251`, scope unverified), and `W = 4096` characters (Grok's default window of 1024 tokens, `grok-build-harness.md:299`, `:333`, at [INFERENCE] 4 characters per token). Grok's `max_threshold` clamp is `2..=64` (`:333`). P0 may change a value only by writing the measured result, with the sweep data over {8, 16, 64} for `K`, into this file before P1.
2. **Action on hit.** Abort the current response and resample the same request once. Use a separate budget, `resilience.degeneration_max_retries` (default 2, Grok parity), with 0–250 ms jitter. Do not decrement the empty-response or stale-streak counters.
3. **Budget exhausted.** Deliver the last response with a status line. Never silently drop text.
4. **Retry classes stay separate.** Transport failures keep `compute_error_backoff`. Degeneration uses only its own budget. The backoff curve does not change in P1.
5. **Observer first.** P0 runs the detector as a stream observer through `plugin_stream_hooks.py`, so measurement needs no core change. This depends on observers receiving assistant text deltas, which is unverified (P0 to confirm).
6. **Stale timeout.** No behaviour change. P0 records the effective default so the open number is filled in.

## Host changes
- **No new hook name.** The detector is a pure function. Hermes already exposes the stream observer path; no entry is added to `VALID_HOOKS`.
- **Core change, P1 only.** A new `detect_degeneration(text_window)` in `agent/repetition_guard.py`. The stream consumer in `agent/chat_completion_helpers.py` calls it on accumulated text. On a hit it raises a dedicated exception that the turn loop catches to resample. The exact consumer site is not verified by read; P0 locates it. The nearest verified anchors are the stale-stream sites at `:2414-2436` in the same file.
- **Config**: `resilience.degeneration_resample` = `shadow` | `apply` | `off` (default `shadow`), `resilience.degeneration_max_retries` = 2, `resilience.degeneration_fragment_chars` = 400, `resilience.degeneration_repeat_count` = 8, `resilience.degeneration_window_chars` = 4096.

## Phases
- **P0 — measure, observer only, shadow.** Log every detector hit with response length and whether the response ended normally. Record the effective stale timeout per provider (`hermes_cli/timeouts.py:38`). Hand-review at least 50 real responses. Exit gate: false-positive rate and the chosen `N`, `K`, `W` are written into this file before P1.
- **P1 — apply.** Core detector in the stream consumer, separate budget, `shadow` then `apply` once P0 false positives are acceptable. Exit gate: zero resamples on responses P0 labelled clean; resample count and budget exhaustion are logged.
- **P2 — backoff curve, conditional.** Compare `jittered_backoff(5.0, 120.0)` with the Grok curve (`:325`) and droid's (`retryPolicy.ts`) only if P0/P1 logs show retry waits dominate turn latency. Otherwise close as "not pursued" with the data.
- **P3 — docs.** Update the resilience section of the Hermes docs, if any, and this file's Status line.

## Tests
- `tests/test_degeneration.py` (new). Assertions:
  - The detector flags a synthetic repeated fragment of at least `N` characters repeated at least `K` times.
  - Long non-repetitive text is not flagged.
  - A fragment repeated fewer than `K` times is not flagged.
  - Budget exhaustion surfaces the text rather than dropping it.
  - The resample path does not decrement the empty-response or stale-streak counters.
- Loop-guard regression: the existing `tests/test_loop_recovery.py` tests pass unchanged with plan 07 in `apply` mode.
- Each detector test must fail when the detector call is removed (verify once by hand, then keep the check).
- Run once after all phases land, not per phase.

## Risks & open questions
- **False positives.** Legitimate repetitive output (tables, code, lists) can match. The P0 gate measures this. Mitigation: the `N ≥ 400` floor from `repetition_guard` precedent.
- **Resample cost.** Each resample is a full request. The cap is 2.
- **Same output on resample.** A resample with unchanged sampling may reproduce the degeneration. Open: whether to change sampling on resample. How Hermes sets temperature on this path is not verified; decide in P1.
- **Unknown stale default.** The effective Hermes stale timeout is not captured. P0 fills it in.
- **Unread droid constants.** `apps/cli/src/hooks/constants.ts` was not read for this plan. Do not copy droid's degeneration thresholds without reading that file.
- **Grok doom-loop semantics.** `grok-build-harness.md:333` is now read: `DoomLoopRecoveryPolicy` defaults `max_threshold=64`, `max_retries=2`, `window_tokens=1024`. Whether `max_threshold` is a repeat count is not confirmed, so the `K=8` choice rests on the sampler config, not this default. Do not cite beyond `:333`.
- **Observer delta access.** Whether stream observers receive text deltas is unverified; if not, P0 must hook the consumer directly.

## Non-goals
- Not implementing tool-call loop detection or cycle detection; loop-guard owns both.
- Not adopting droid's `SINGLE_PROVIDER_CAPACITY_RETRIES=10` or the 5 s throttle floor without data.
- Not changing the empty-response guard or the stale-streak give-up; both already work.
- Not using `repetition_guard` as a doom-loop detector.
- Not adding a server-side doom-loop header; Hermes does not control the provider's inference service.
