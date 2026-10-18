# 06 compaction prefire
Status: proposed · Owner: — · Depends on: host seam H3 (background summary job + boundary-keyed reuse in `agent/context_compressor.py`); relates to plan 05 (pruning) and plan 07 (turn resilience)

## Goal
Hide the latency of context compaction. Today Hermes starts the summary LLM call only once the request is already over its compaction threshold, so the user's turn waits on a summary call before the main request can go out. The plan adds a background "prefire": when the conversation nears the threshold, a summary of the stable older prefix is computed early and cached. If compaction then fires and the cached prefix still matches the live history, the cached summary is used and no wait occurs. If the prefix has changed, the cache is discarded and behaviour is identical to today. Output on a cache miss must be byte-identical to the current path.

## Evidence
- **Grok Build, source-backed spec** — `re/docs/grok-build-harness.md` §5 (turn sequence, `:192-196`): each turn calls "maybe prefire two-pass compaction", then `check_auto_compact_needed() -> run_compact_only(trigger)`. Prefire is T7 (`:691`): background pass 1, not a compaction. It fires when `exceeds_threshold(est, cw, threshold − 10)` holds, two-pass is active, no cache exists, and nothing is in flight (`compaction.rs:180-192`, `turn.rs:2853-2865`).
- **Grok, lead and split constants** — `:702`: prefire lead is 10 points, overridable via `GROK_PREFIRE_LEAD_PERCENT` (`compaction.rs:62-68`). `:703`: two-pass split is 0.95 by token weight, clamped to 0.05–0.95; conversations under 4 items skip two-pass (`two_pass.rs:13,33`). `:704`: the seed is the last closed `<summary>` block if over 1000 chars, else the whole output, capped at 60 000 chars (`two_pass.rs:16,19,163-219`). `:705`: a summary seed under 500 chars after cleaning is rejected (`GC/code_compaction/config.rs:15-20`). `:721`: two-pass is on by default.
- **Grok, hooks and memory** — `:693`: the only `user_context` given to the summariser is the active goal pin (`goal.rs:1079-1103`); `auto_continue` exists but production passes `None` (`compaction.rs:611-612,2342-2343`). `PreCompact{source}` fires before sampling (`:983-990`) and `PostCompact{source}` after replacement (`:2027-2034`), with `source` ∈ {`manual`,`auto`}. Legacy-memory mode flushes memory before compaction (`maybe_pre_compaction_flush`, `:534-573`).
- **Grok, default window** — `:701`: unknown context window defaults to 256 000 (`SHX/remote/client.rs:589,638-639`).
- **droid, observed** — `droid/notes/DEEP-FINDINGS.md:124-127`: the threshold is checked every loop iteration (`AgentLoop.ts:6349-6458`); the default limit is 250 000 tokens, overridable by `compactionTokenLimit` (a value of 50 was observed firing). The summariser is a tool-less `/responses` call with `max_output_tokens:16000` and `reasoning.effort:"none"`. Manual compaction is `/compress` (`:127`, `:202`); `/compact` does not exist.
- **droid, unverified** — a note says the prefire trigger is "70% speculative / 5% tolerance / 5-min discard". No file under `re/docs` or `droid/notes` supports these numbers. Only an `[inferred match]` exists at `lineage-backfill.md:337`. **These values are not used anywhere in this plan.** They are recorded so reviewers know they were checked and rejected.

## Current state
- Trigger: `ContextCompressor.__init__` defaults `threshold_percent=0.50` (`agent/context_compressor.py:2297`). Small-context floor raises it to 75% under 512K (`:2252-2256`). Token threshold: `_compute_threshold_tokens` (`:2260`). Predicate: `should_compress_info` (`:2490`), returning `(bool, reason)` with reasons `cooldown:<s>` or `ineffective`.
- Call sites: `agent/turn_preflight.py:144` (pre-API, after the request is built) and `:304`, both `agent._compress_context(...)`. The first site refunds the attempt on `context_compression_timed_out` (`:148-156`) and on lock or cooldown deferral (`:157-165`).
- Summary generation: `_build_summary_prompt` (`context_compressor.py:3352-3407`) builds either a fresh or an iterative prompt. The iterative branch embeds `PREVIOUS SUMMARY` and `NEW TURNS TO INCORPORATE` (`:3373-3388`). The section template comes from `_summary_template_sections` (`:3426`). `_generate_summary` is at `:3295`; its body was not re-read for this plan.
- Hooks: `conversation_compression.py:2610-2647` (`_pre_compress_memory_context`; `on_pre_compress` calls at `:2633` and `:2644`) runs before compaction. The hook is the only pre-compaction seam; no post-compress hook was found.
- **No two-pass or prefire exists.** `grep two_pass|two-pass|TwoPass` over `hermes-agent/agent` returns no matches. The compressor performs one summary LLM call per compaction, at the point of need.
- Existing knobs: `protect_first_n=3`, `protect_last_n=20`, `summary_target_ratio=0.20` (`context_compressor.py:2297-2298`).

## Design
**Prefire is a boundary-keyed cache, not a second compaction.**
1. Each turn, after the request is built and before the main call, use `request_pressure_tokens` (parameter at `agent/turn_preflight.py:58`). If it is `≥ threshold_tokens − lead` and no prefire is cached or in flight, start a background summary of the **stable prefix**: messages up to a boundary that excludes the last `protect_last_n` messages and any open tool round. The boundary is chosen by the same `_prune_boundary` logic (`agent/context_compressor.py:2628`) that compaction already uses, so the prefix is a subset of what compaction would later summarise.
2. Store the result as `{boundary_fingerprint, summary_text, model, created_at}`. The fingerprint is a hash over the role and `tool_call_id` sequence of the prefix. P1 also hashes the prefix content (see Risks), so a prune by plan 05 or core pruning that rewrites tool results invalidates the cache.
3. At compaction time (`_compress_context`), recompute the fingerprint for the boundary it would use. If it matches the cache, use `summary_text` and skip the LLM call. If not, discard the cache and run the existing path unchanged. **The discarded cache must never influence output.**
4. Lead default: 10 percentage points below threshold, matching Grok (`compaction.rs:62-68`), overridable via `compression.prefire_lead_percent` (Hermes config key, new). Setting it to 0 disables prefire.
5. Two-pass (Grok's 0.95 token-weight split) is **not** part of phase P1. It is evaluated in P2 only if the P0 data show prefix-stability is high enough to make a larger prefix worth caching.
6. Single slot. One cache entry per compressor; a new boundary replaces the old one. Ceiling: the cache is cleared after each compaction, so prefire cost scales with compaction count, not turn count. Upgrade path: a two-entry LRU if P0 shows boundary churn.

Rejected: a separate timer thread that summarises on a clock. Compaction triggers on token pressure, so a time-based prefire would waste calls when pressure never arrives.

## Host changes
- **H3 (new seam)**: `ContextCompressor.prefire(messages) -> None` and a private `_prefire_cache` slot in `agent/context_compressor.py`. The cache is consulted inside `_compress_context` (`agent/compression_facade.py:189-193`) before it calls `_generate_summary` (`context_compressor.py:3295`). This is core, not a plugin, because summary generation, boundary choice, and message rewrite are all internal.
- **Config key**: `compression.prefire_lead_percent` (default 10, 0 disables). Added where `threshold_percent` is read.
- **Observability**: one status line when a prefire starts and one when a cache hit or discard occurs. Use the existing `automatic_compaction_status_message` path (`turn_preflight.py:127`). No new hook is needed for P0-P1. Hook names are not added to `VALID_HOOKS`.
- **No change to `PreCompact` or `PostCompact`**: Hermes has `on_pre_compress` (`conversation_compression.py:2610`) but no post-compress hook. Adding one is out of scope.
- Memory flush ordering: Grok flushes memory before compaction (`grok-build-harness.md:534-573`). Hermes already runs `_pre_compress_memory_context` at `conversation_compression.py:2610`. The prefire must **not** run that hook, because it is a side effect and would fire twice or early. The prefire is read-only with respect to memory.

## Phases
- **P0 — measure, no behaviour change.** Compute and log, on every turn that crosses `threshold − lead`, the boundary fingerprint and the summary that prefire *would* have produced. Do not send it or reuse it. Record at compaction time whether the fingerprint would have matched. Ship only the logging. Exit gate: hit rate (fingerprint matches at compaction time) is measured on at least 20 real compactions; the number is recorded in this file before P1.
- **P1 — apply, single pass.** Enable the cache for the non-streaming summary path only. Add `prefire_lead_percent`. Exit gate: zero output differences on a cache miss (byte-compared against the P0 recorded baseline); hit path's summary is byte-identical to a cold call for the same input under `temperature=0` where the provider supports it, else documented as non-deterministic.
- **P2 — two-pass, conditional.** Only if P0 hit rate is high enough that a larger prefix (0.95 token weight, Grok `two_pass.rs:13`) adds value. Otherwise close P2 as "not pursued" with the data.
- **P3 — docs.** Update the compaction section of the Hermes docs (if any) and this file's Status line.

## Tests
- `tests/agent/test_prefire_cache.py` (new). Assertions:
  - Fingerprint is stable under content-only edits to the prefix (same roles and `tool_call_id`s → same fingerprint). Changing message count or roles changes it.
  - On a hit, `_generate_summary` is called zero times during `_compress_context`.
  - On a miss, `_compress_context` output equals the cold-path output for the same messages (compare message lists exactly).
  - With `prefire_lead_percent=0`, no background call is started.
  - A prefire that raises is swallowed; the compaction still runs cold and the failure is logged once.
  - Concurrency: a compaction that starts while a prefire is in flight either waits for it or runs cold; it never reads a partially written cache entry.
  - A cache entry created under model A is not used when the active summary model is B.
  - At `threshold − lead − 1` tokens no background job starts.
- Each test must fail if the fingerprint check is removed (verify once by hand, then keep the check).
- Run once after all phases land, not per phase.

## Risks & open questions
- **Extra spend.** Prefire may summarise a prefix that compaction never uses (conversation ends, or pressure drops after a prune by plan 05). P0 measures the waste rate; if it is high, raise the lead or disable.
- **Prefix stability.** Hermes trims and dedupes tool results during pruning (`context_compressor.py:2755`, `:2641`). A prune between prefire and compaction changes content but not roles or IDs, so the fingerprint still matches and the cached summary describes text that has since been rewritten. **Open:** decide whether the fingerprint should include a content hash of the prefix (safer, lower hit rate) or whether summaries are tolerated as slightly stale. Default in this plan: include content hash in P1; revisit with P0 data.
- **Cache lifetime.** `_reset_session_compaction_state` (`agent/context_compressor.py:1868`) is the shared reset for `/new`, `/reset` and session end. The prefire cache must be cleared there. Model switch is **open**: the cache must also be cleared when `summary_model_override` or the model changes, which is not yet traced.
- **Model choice for the prefire.** It must use the same summary model that compaction would use (`summary_model_override`, `context_compressor.py:2298`; stored as `self.summary_model` at `:2380`). `agent_init.py:1839` passes `summary_model_override=None`, so the effective model is set elsewhere; this is not traced.
- **Prompt caching.** The prefire call is a separate request, so it does not alter the main prompt's cache prefix. This is [INFERENCE]; it has not been measured.
- Grok's "auto_continue" and goal-pin `user_context` (`grok-build-harness.md:693`) are not copied. Hermes has no equivalent goal pin in this path that was verified.
- The droid threshold figures in the Evidence section are not used. If someone later finds a source for "70% / 5% / 5-min", it must be re-verified before use.
- **Overlap with plan 05.** Plan 05 rewrites only the request copy, not compressor history, so the prefix the compressor summarises is unchanged by it. P0 confirms this with the plan 05 middleware enabled.
- **Observability cost.** P0 logs the would-be summary text. Cap any stored text at the budget `_compute_summary_budget` already enforces (defined at `agent/context_compressor.py:2930`, called at `:3316`), and keep it out of session storage.
- **Refund semantics.** A compaction deferred by lock or cooldown, or timed out, gives back its attempt (`agent/turn_preflight.py:148-165`). A discarded prefire must not count as a compaction attempt, so it never touches `compression_attempts`.

## Non-goals
- Not changing the summary prompt text or sections (`_build_summary_prompt`). Prompt quality is a separate plan.
- Not changing the threshold itself (0.50 default, `context_compressor.py:2297`).
- Not adding a time-based or wall-clock trigger.
- Not replacing the summariser model or provider.
- Not touching `PreCompact`/`PostCompact` semantics or `on_pre_compress` memory flush.
- Not implementing Grok's two-pass in P0 or P1.
- Not caching across sessions; the cache is per-compressor-instance and dies with the session.
- Not persisting the prefire cache to disk or session storage; a restart discards it.
