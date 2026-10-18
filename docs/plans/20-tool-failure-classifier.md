# 20 tool-failure-classifier
Status: proposed · Owner: — · Depends on: plan 04 (`STEER[v1]` catalog, consumer of labels), plan 07 (per-label retry budgets); coordinates with `loop-guard` (identity owner); reads `model_tools` observer fields only

## Goal
A tool call failed. Hermes today tells the model the same generic thing for a missing binary, a denied permission, a flaky network, and a bug in the tool. Plan 04 can only write the right steering line if it knows which of those happened, and plan 07 can only budget retries if it knows which failures are worth retrying. This plan adds a six-label failure classifier:

- `transient`: a retry after a delay can succeed.
- `env-missing`: something the tool needs is absent (path, cwd, binary, env var, MCP server).
- `permission`: denied by policy or the OS (approval, EACCES, auth required).
- `bad-args`: the arguments are malformed or violate the tool schema.
- `model-misuse`: arguments are valid but the call targets the wrong thing (stale patch text, oversized read, wrong tool).
- `real-bug`: the tool failed in a way the model cannot fix.

Labelling is rule-first. A deterministic table labels the unambiguous cases, and a model is consulted only for the residual. The label feeds plan 04's catalog selection and plan 07's budgets. It never changes a result, never retries by itself, and never counts repeats (loop-guard owns identity).

## Evidence
- **Observer fields, verified by read.** `_tool_result_observer_fields` (`~/projects/hermes-agent/model_tools.py:646-690`) returns `(status, error_type, error_message)`. A dict with an `error` key gives `error_type="tool_error"`, which is generic. Otherwise `("ok", None, None)`. Consequence: `error_message` carries nearly all the signal, and `error_type` alone is too coarse to label on.
- **Hook order, verified by read.** `post_tool_call` is emitted at `model_tools.py:944` and `transform_tool_result` at `:945`. A label computed in `post_tool_call` exists before any transform runs.
- **Exit codes, verified by read.** `tools/process_registry.py:1361-1365` carries `exit_code`, `completion_reason`, `termination_source`. `tools/terminal_tool.py:803` and `:821` return `exit_code: -1` on exceptions, which is ambiguous between transient and real-bug. `:1118` returns `exit_code: None` on yield-to-background, which is not a failure at all. `:1175-1179` returns `status: "degraded"` with `reason` and `retry_hint`, a hint the tool itself supplies.
- **Loop-guard identity, verified by read.** `plugins/loop-guard/__init__.py:91-106` builds exact failure identity from `[error_type, payload.code, error, error_message]`. `after()` at `:224-262` counts repeats. This plan must not change either.
- **Steering classes, verified by read.** Plan 04 `Design` lists the catalog: `approval_denied`, `approval_timeout`, `no_human_present`, `path_not_found`, `patch_no_match`, `file_too_large`, `mcp_auth_required`, `mcp_transport_down`, `terminal_cwd_missing`, `plugin_block_loop`, `plugin_block_review`. Unknown failures get no envelope.
- **Retry budgets, from plan 07.** `doom_max_retries` is 2 for identical repeats. General backoff is `min(2000 ms << (n-1), 30 000 ms)` with ±20% jitter. Plan 07 excludes tool-call loops from its scope.

## Current state
- No failure labels exist. Plan 04 has no label input; its catalog is keyed by `(tool, error_type or regex)`.
- Plan 07 budgets are per-turn and per-provider, not per failure cause.
- The meta-classifier pattern (`plugins/meta-classifier/client.py`, `MAX_TEXT = 100000` at `:17`) is the model-call precedent. This plan reuses its shape without editing those files.
- No plugin named `*failure*` exists under `plugins/` (checked).

## Labels
Single label per failed call. The label is about cause, not about what the agent should do next. Steering and retry policy are derived from it downstream.

| Label | Meaning | Retry guidance (plan 07) | Steering class (plan 04) |
|---|---|---|---|
| `transient` | network timeout, 429/503, connection reset, process killed by timeout | automatic retry with backoff | none (`mcp_transport_down` stays rule-owned by plan 04) |
| `env-missing` | path, cwd, binary, env var, or MCP server absent | none until environment changes | `path_not_found`, `terminal_cwd_missing` |
| `permission` | policy denial, EACCES, auth required, user not present | none; ask user | `approval_denied`, `approval_timeout`, `no_human_present`, `mcp_auth_required` |
| `bad-args` | schema violation or malformed value | one retry with corrected input | none (the tool's own error is already actionable) |
| `model-misuse` | valid arguments, wrong target (stale patch, oversized read, wrong tool) | one retry with changed input | `patch_no_match`, `file_too_large` |
| `real-bug` | unexpected exception or inconsistent result | none | none (never invent advice); logged for operators |

Rules for label assignment, applied before any model:
- `status == "ok"` or `exit_code is None` with a yield-to-background marker: not a failure, never labelled.
- `status == "blocked"`: labelled `permission` unless the message carries `LOOP GUARD:`, which is `model-misuse`.
- A message matching the plan 04 catalog's stable phrases maps to the matching class's label.
- `status == "degraded"` with a tool-supplied `retry_hint`: the hint decides `transient` versus `env-missing`.

## Input contract
The classifier sees only the following. Nothing else enters a prompt.
- `tool`: tool name.
- `status`: as recorded by `_tool_result_observer_fields`.
- `error_type`: as recorded; generic `tool_error` is passed through unchanged.
- `error_message`: truncated to 1000 characters, after the egress redaction filter. The exact filter to call is an open item (see Risks); plan 04 already declines to copy raw error text into logs.
- `exit_code`, `termination_source`, `completion_reason`: present only when the tool returned them.
- `retry_hint`, `reason`: present only for `degraded` results.
- `arg_keys`: sorted argument key names, not values. This is the only argument information sent.

Not sent: full tool output, argument values, user messages, transcript history, file contents.

## Decision wiring
Three stages, each gated by its own config switch.

1. **Rules (deterministic, no model).** The table above labels the unambiguous residual. Rule output is final for those calls. Config `rules: on` by default.
2. **Model, residual only.** Runs only where rules return no label. In shadow it also runs on rule-labelled calls to measure rule agreement, and logs disagreements; its output is never applied to a rule-labelled call.
3. **Apply.** Plan 04 selects catalog rows by label, and plan 07 applies per-label budgets. Apply is blocked until the promotion gate passes.

Hook placement: `post_tool_call` (a valid hook, `hermes_cli/plugins.py:110-221`), observer only, returns nothing. The model call runs in a bounded worker. The hook returns immediately, so no tool loop waits on a model. Verdicts are written to the shadow log when ready.

Handoff to plan 04 (decision D4 below): plan 04 vendors the rule table as a small `classify_rules()` function, following its own vendored `format_steering()` pattern. Plugins stay independently installable. Model verdicts reach apply only through a host change proposed separately (not in this plan).

Budget mapping (plan 07): `transient` uses the general backoff. `env-missing`, `permission`, and `real-bug` get zero automatic retries. `bad-args` and `model-misuse` get one retry with changed input. Plan 07's `doom_max_retries` keeps counting identical repeats and is unchanged.

## Data & evaluation
- **Corpus.** Failed tool results from existing session stores, stratified by tool and by `status`, sampled without live customer text leaving the machine. Labelling uses the replay pattern from plan 01 (`tooling/classifier-replay.mjs`).
- **Size.** At least 300 labelled failures with at least 30 per label. `real-bug` and `env-missing` are likely short; if either is short, the gate is not met and the label is reported as unevaluated, not merged silently.
- **Agreement.** 15% double-labelled. Cohen's kappa per label pair. If kappa is under 0.7 for `bad-args` versus `model-misuse`, they merge into `model-misuse` for steering. This pair is expected to be hardest.
- **Metrics.** Per-label precision and recall; confusion matrix; rule coverage (share of corpus labelled by rules alone); model-versus-rule agreement on the residual.
- **Harm metrics.** False `transient` on `real-bug` calls, because they cause wasted retries; target at most 5%. False steering on `ok` or yield-to-background calls, target 0.
- **Promotion gate.** Apply a label family only when (a) per-label precision is at least 0.90 for the steering-driving labels (`env-missing`, `permission`, `model-misuse`), (b) false `transient` on `real-bug` is at most 5%, and (c) the model adds recall over rules alone on the residual.
- **Test.** `tests/test_tool_failure_classifier.py`: rule table on fixed error strings for each label; yield-to-background is never labelled; `blocked` with `LOOP GUARD:` is `model-misuse`; `retry_hint` overrides a model `transient` label; argument values never appear in the prompt payload (assert on the built payload).

## Failure policy
- Model timeout, error, malformed output, or unknown label string: label `unavailable`. Never coerce to the nearest label.
- Rules fail: label `unavailable`, log once per tool. No steering is applied for an `unavailable` call.
- Worker overflow (bounded semaphore full): record `overflow`, label `unavailable`.
- The plugin never edits a tool result in any mode. Its only effects are log lines and, after promotion, label-driven catalog choice in plan 04.
- Logs store the label, tool name, and a hash of the error message. Raw error text is never logged.
- A `real-bug` label never triggers a retry or a model-written suggestion. It only lands in the operator report.

## Rollout
1. **P0, rules-only shadow.** Plugin `plugins/tool-failure-classifier/` registers `post_tool_call` and writes label records to `$HERMES_HOME/plugins/tool-failure-classifier/shadow.jsonl` (1 MiB cap, one rotation). Acceptance: on the sampled corpus, every failed call receives a label or `unavailable`; the `ok` and yield-to-background calls receive none; rule coverage is reported.
2. **P1, model shadow.** Model runs on the residual and on all rule-labelled calls for disagreement logging. Acceptance: confusion matrix and kappa reported; the promotion gate evaluated.
3. **P2, apply through plan 04 and plan 07.** Only after the gate passes for the relevant label family. Acceptance: a `path_not_found` failure gains the `STEER[v1]` envelope with the label as its class; an `ok` call is unchanged; a `real-bug` call gets no envelope and no retry.

## Risks
- **D1, rules first, model second.** Decided. Most failures are determinable from the error text and exit code; a model call per failure is paid cost and latency for no gain on those.
- **D2, `bad-args` and `model-misuse` may merge.** Decided, conditional on kappa under 0.7. The distinction depends on whether the model knew the target was wrong, which the error text often does not say.
- **D3, argument values are never sent.** Decided. Argument values can hold file contents, secrets, or user text. The cost is that some `bad-args` cases are harder to label from key names alone; accepted.
- **D4, handoff by vendored rule table, not a shared in-process map.** Decided. A shared map between plugins would be a cross-plugin import, which plan 04 forbids. Reversal trigger: if model labels must drive apply, propose a host seam for label passing as a separate change, not an import.
- **D5, tool-supplied `retry_hint` beats the classifier.** Decided. The tool knows its own retry semantics better than a label does.
- **Resolved (grep), egress redaction.** Use `agent.redact.redact_for_egress` and fail closed on `REDACTION_UNAVAILABLE` in the result, the same pattern as `plugins/meta-classifier/text.py:13-18` and `plugins/auto-review-classifier`. An unsafe or unavailable scrub drops `error_message` to `None` rather than sending it.
- **Resolved (read), `blocked` results.** `blocked` results do reach `post_tool_call`: `model_tools.py:926-929` emits with `status="blocked"`, `error_type`, and `error_message` from `_pre_dispatch_guards`. P0 acceptance may rely on that. Plan 04 H1 is consistent with this.
- **Open, `error_type == "tool_error"` coarseness.** Accepted for now. If rule coverage is under 60% on the corpus, add a per-tool error-type normalisation at the rule layer, not in the host.

## Non-goals
- No retry execution. Plan 07 owns backoff; this plan only labels.
- No steering text. Plan 04 owns envelope wording; this plan supplies a class choice.
- No repeat counting or identity change. `loop-guard` `_failure` (`:91-106`) keeps that.
- No change to approval decisions, to `blocked` semantics, or to any tool's result.
- No LLM-written advice for any label.
- No live customer text in prompts, logs, or labelling corpus.
