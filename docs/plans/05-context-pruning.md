# 05 context-pruning
Status: proposed · Owner: — · Depends on: none (uses existing `llm_request` middleware kind); optional host seam H2 for a fractional usage gate

## Goal
Keep old tool output from crowding the context window, using Grok Build's age-based policy for the band Hermes currently leaves untouched: recent-but-not-newest tool results. The user-visible outcome is fewer tokens spent per turn on stale bulky results (large file reads, long shell output) without losing the newest results the model is still working from, and with no change to the stored session history.

## Evidence
- **Source (Grok Build, RE doc)**: `re/docs/grok-build-harness.md:674` (§5.2). Defaults from `CS/types.rs:65-94`, `request_builder.rs:12-177`: enabled, `keep_last_n_turns=3`, `soft_trim_threshold=4000` chars, head/tail 1500 each, `hard_clear_age_turns=10`. Age = count of `User` items counted from the end. `<3` turns old: untouched. `>=10`: body replaced by `[Tool result omitted — too old]`. Otherwise, if longer than 4000 chars: `head(1500) + "\n\n[…trimmed…]\n\n" + tail(1500)`. Applied only above 50 % window usage.
- **Source (Grok Build)**: `grok-build-harness.md` §5.2 (prompt-cache rule not re-read; treated as [INFERENCE]): the trim is a pure function of content and age, which is the property a stable prefix needs.
- **Observed (droid)**: no equivalent turn-age trim was found in the droid notes that were read; droid compaction is threshold-driven (`notes/DEEP-FINDINGS.md:124`). Not used as evidence for this plan.
- **Inferred**: a stateless request-time trim re-derives identical bytes on each turn, so the cached prefix stays stable until a new user turn crosses an age boundary. This is an inference from the algorithm, not an observed cache hit. P0 measures it.

## Current state
Hermes core already prunes tool results, in more aggressive form than Grok for old results:
- `agent/context_compressor.py:2755` `_prune_old_tool_results`: runs dedup (pass 1), summarises tool results to one line for everything before `prune_boundary` (passes 2, via `_demote_tool_result_at` at `:2675`), truncates large tool-call arguments (pass 3), retires old images (pass 3.5), and pressure-demotes the tail when a token budget is set (`_pressure_demote_tail` at `:2705`).
- `_PRUNE_MIN_CHARS = 200` (`context_compressor.py:661`) is the floor below which nothing is summarised.
- `agent/context_compressor.py:2859` `prune_tool_results_only`: the deterministic, no-LLM entry point. Gated by `proactive_prune_tokens` (`:2880-2882`), a rearm hysteresis check (`:2889`), and an `archive_and_compact` capability check (`:2894-2896`); without that capability every prune is a no-op. Its passes call `_prune_old_tool_results` (`:2897`), which changes stored history.
- Tail protection is by message count (`protect_last_n`) with an optional token walk (`_prune_boundary` at `:2628`). Messages inside the tail are never summarised by pass 2.

**The exact gaps relative to Grok §5.2:**
1. **Recent-tail band (ages 3 to 9).** A large tool result inside the protected count-tail is kept verbatim in full. Grok would trim it to head/tail once it is older than 3 turns. Hermes has no head/tail trim of tool-result content on the request path. Head/tail slicing exists only for summariser input (`agent/context_compressor.py:3168-3172`), tool-call argument JSON (`:1266-1275`, `head_chars=200`), and prompt-builder files (`agent/prompt_builder.py:1443-1451`); grep for `soft_trim|head_chars|tail_chars|_trim_middle` in `agent/` finds these and no tool-result equivalent.
2. **Turn-age, not message-count.** Hermes ages by message position. Grok ages by user turns. A single turn can contain many tool messages.
3. **Usage gate.** Hermes gates on an absolute `proactive_prune_tokens` (`:2880`). Grok gates on 50 % of the window. These are different knobs; no fractional gate exists.
4. **Hard-clear at age >= 10 (`hard_clear_age_turns`).** Hermes has no explicit equivalent, but it is already covered: every result outside the tail is summarised to one line by pass 2 regardless of age. P2 verifies this and drops the item if it holds.

Hermes is more lossy than Grok outside the tail (summary, not head/tail) and less lossy inside it (verbatim, not trimmed). The plan targets only the second difference.

## Design
**Plugin, not core.** A new `plugins/context-pruning/` registers `register_middleware` on the `llm_request` kind (`hermes_cli/plugins.py:943`; kind dispatch `hermes_cli/middleware.py:88` `apply_llm_request_middleware`; fire site `agent/turn_api_request.py:151`, just before the provider call). Its input is `api_kwargs`, and its output replaces the kwargs via `RequestMiddlewareResult` (`middleware.py:30-35`, `changed` flag). [INFERENCE, P0 to confirm: core's prune pass runs before the request is built, so the plugin only sees what core left in the tail.]

Rationale for a plugin over a core change: the logic is a pure transform on the request copy, it does not touch session storage, and a shadow mode can ship without a host release. Rationale against changing `_prune_old_tool_results`: that path persists through `archive_and_compact` (`:2894-2896`), so a per-request rule would either duplicate that persistence or diverge from it.

**Algorithm (pure function of `(messages, config)`):**
1. Compute age for each `role == "user"` message as the number of user messages from the end (Grok definition). Tool messages inherit the age of the user message that precedes them.
2. For each `role == "tool"` message with string content:
   - age < `tail_age_min_turns` (default 3): untouched.
   - content length <= `soft_trim_threshold_chars` (default 4000): untouched.
   - already contains the marker `"[…trimmed…]"`: untouched (idempotence).
   - otherwise: replace with `head[:1500] + "\n\n[…trimmed…]\n\n" + tail[-1500:]`.
3. Skip the whole pass unless the usage gate passes (see Host changes, H2). Until H2 lands, the gate is `current_tokens >= usage_gate_tokens`, a plugin config value the operator sets to 50 % of the model's window. Explicit beats implicit.
4. Never touch `tool_call_id`, `role`, or message count. Never touch assistant `tool_calls` (that is `_truncate_tool_call_args_at`'s job in core).

**Config (`config_schema`):**
- `mode: shadow|apply`, default `shadow`.
- `tail_age_min_turns: 3`.
- `soft_trim_threshold_chars: 4000`.
- `head_chars: 1500`, `tail_chars: 1500`.
- `usage_gate_tokens: 0` (0 disables the gate; a positive value must be set explicitly).
- `shadow_log: <path>` default `$HERMES_HOME/plugins/context-pruning/shadow.jsonl`, 1 MiB cap, one rotation.

**Shapes.** `api_kwargs` differs by api mode. For chat-completions the list is assumed to be `kwargs["messages"]`; for Responses it is assumed to be `kwargs["input"]`. Both are **unverified** for this plan and P0 must confirm them from `agent/turn_api_request.py` before any apply work. Api modes not confirmed are a logged no-op.

**Persistence.** None. The transform is recomputed each request. Nothing is written to the session store.

**Failure modes.** Any exception in the transform returns the original payload unchanged and logs once. Content that is not a string (multimodal lists) is skipped. The middleware fails open: `RequestMiddlewareResult` with `changed=False`.

**Cache behaviour.** A message's trimmed bytes depend only on its content and its age, and age changes only when a new user turn arrives. The prefix before the first newly trimmed message is therefore byte-identical across the boundary crossing. P1 acceptance checks this directly.

## Host changes
**H2 (optional, P1 gate only): fractional usage gate.** Expose the model's context window to `llm_request` middleware context (it is not in the kwargs listed at `turn_api_request.py:151-156`). Signature: add `context_window: int | None` to the `apply_llm_request_middleware(**context)` call. Fire-site: `agent/turn_api_request.py:151`. Why no existing hook suffices: the middleware context includes `model` and `provider` but not the window size, and the plugin cannot derive it reliably. If H2 is not granted, `usage_gate_tokens` (absolute) is the fallback and the plan says so.

No other host changes. `llm_request` middleware, `register_middleware`, and the `RequestMiddlewareResult` shape already exist.

## Phases
- **P0, shadow measurement.** Plugin skeleton, config, `llm_request` middleware that computes the transform and logs to `shadow.jsonl` without changing the payload. Record: message count, number of tool messages at each age band, bytes that would be removed, and the api-mode shape check. Acceptance: on a recorded chat-completions fixture, the logged byte total equals the sum of the head/tail reductions computed by hand, and the returned payload is the input object (identity), so `changed` is `False`.
- **P1, apply for chat-completions.** `mode: apply`, api modes confirmed in P0 only. Acceptance: (a) applying the transform twice yields byte-identical payloads; (b) message count, roles, and `tool_call_id`s are unchanged; (c) for two consecutive requests where the second adds one user turn, the byte prefix before the first changed message is identical; (d) a 3000-char tool result stays whole at age 2 and is trimmed to 3000+marker at age 4.
- **P2, verify hard-clear is redundant (no code if confirmed).** Run a fixture with a 12-user-turn history and confirm every tool result older than 10 turns is already a one-line summary from core pass 2. If it is not, add `hard_clear_age_turns: 10` with replacement `[Tool result omitted — too old]` and a P2 acceptance test. Acceptance: the fixture decision is recorded in the plan's Risks section, one way or the other.
- **P3, usage gate.** Only after H2 lands, switch the gate from `usage_gate_tokens` to the fractional value. Acceptance: below 50 % usage the payload is identity; above it the P1 checks pass.

## Tests
`tests/test_context_pruning.py`, using real loaders via `load_plugin` as in `tests/test_loop_recovery.py`, with no model:
- Determinism: transform output is identical across two runs and idempotent on re-application.
- Band edges: age 2 untouched, age 3 trimmed, length 4000 untouched, length 4001 trimmed, content already containing the marker untouched.
- Structure: role order, count, and every `tool_call_id` preserved; assistant `tool_calls` untouched.
- Prefix stability: two requests differing by one appended user turn share the same byte prefix up to the first changed message.
- Shadow: payload object identity is returned, one JSONL row per call, and the row has no raw content (only counts and byte totals).
- Fail-open: a malformed message (non-string content, missing role) passes through unchanged and logs once.

Tests are the minimum: one file, no fixtures beyond the in-test message lists.

## Risks & open questions
- **Shape unverified.** The `messages` vs `input` key assumption is not verified for every api mode. P0 must confirm before P1; unconfirmed modes stay no-op.
- **Loss of information in the band.** Head/tail trim drops the middle of a result. For tool output that is mostly middle content (long logs), this may remove the one line the model needs. The soft-trim threshold is high (4000) so most results are untouched, but this is a real trade-off. P0's byte totals decide whether the savings justify it.
- **Usage gate semantics.** Grok's gate is a fraction of the window. Hermes's `proactive_prune_tokens` is absolute. `usage_gate_tokens` requires the operator to know the window; H2 removes that need.
- **Interaction with core commits.** Core prune commits through `archive_and_compact` and changes history. The plugin does not, so for a session that core has already pruned, the plugin's band may be empty. This is intended; P0 measures how often it is non-empty.
- **Cache claim is inferred.** The prefix-stability acceptance check tests the algorithm, not a live provider cache hit. A live cache measurement is out of scope for this plan.
- **Does not overlap plan 06.** Plan 06 covers compaction triggers. This plan never summarises with an LLM and never fires at compaction time.

## Non-goals
- No LLM summarisation, and no changes to `_summarize_tool_result`.
- No changes to the stored session history or to the core commit path (`archive_and_compact`).
- No change to the dedup, argument-truncation, or image-retirement passes in core.
- No change to compaction thresholds or the compaction prompt (plan 06).
- No Grok image-budget eviction (`CS/image_budget.rs`); core pass 3.5 already covers images.
