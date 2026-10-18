# 04 tool-steering
Status: proposed · Owner: — · Depends on: host seam H1 (`transform_tool_refusal`, P2 only); coordinates with `loop-guard`, `auto-review-classifier`; reads `papercuts` conventions only

## Goal
Every tool refusal or failure the model sees tells it four things, in this order: **what happened**, **whether to retry**, **the sanctioned alternative**, and **whom to ask**. Today only some Hermes refusals do (approval denials say "Do NOT retry" but name no alternative or addressee; plugin blocks and ordinary tool errors say less). The plugin completes missing parts from a deterministic catalog, never rewrites what the tool said, never invents advice for failures it has no catalog entry for, and never stacks a second message on a result that already carries one.

## Evidence
- **Grok Bot, decoded** — `re/docs/tools-and-prompts.md` §4.1: the local-computer approval tree ends in strings that state (1) what happened, (2) whether to retry ("Do not retry it."), (3) the sanctioned alternative ("use your own computer instead (Shell, Read, AwaitShell)", i.e. omit `machineId`), (4) what to ask the user. "Already declined this task" variants say a later permission change does not authorize the action and tell the model to "say so in chat and let the user ask for it"; the over-long-approval variant tells it to split the action. §9 item 1 lists the pattern as "lift as-is (design, not text)".
- **Grok Bot, decoded** — §4.2 families: transport/routing (pick a desktop from `ListMachines`; capture a smaller area), computer-use guard ("Ask the user to do this step themselves"), shell denylist ("cannot be approved from this conversation… You may continue working on the task.", fail-closed on unparseable commands), MCP/connector ("call AuthenticateMcpServer so the user can allow it"; Chrome-cookie import "do not tell the user it failed, and do not retry").
- **Grok Build, source** — `re/docs/grok-build-harness.md` §5.2 (`:672`) describes harness-repaired tool results as short, one-part text, not four-part: `Tool execution was halted by the harness ({class}); the tool `{name}` was not executed.` The string is confirmed only by `compaction_utils_tests.rs:1700`; the production site `conversation.rs:2049-2057` and the reminder append (`reminders/mod.rs:155-162`) are **unverified** (source not re-read). Cross-cutting reminders are appended to the same tool result (`format_with_reminders`, §3.5 `:428`).
- **droid, observed** — notes show model-facing refusal copy is per-class (`server-llm-proxy.md` error table) rather than generic; no single envelope.

## Current state
- Hook: `transform_tool_result` runs after `post_tool_call`, before the result enters context, fail-open, **first string return wins** (`~/projects/hermes-agent/model_tools.py:834-851`, fire at `:945`). Kwargs include `status`, `error_type`, `error_message`, `duration_ms` (`:844-847`). It is not chained, so only one plugin may rewrite a given result; bundled `security-guidance` also uses it but only on success (`~/projects/hermes-agent/plugins/security-guidance/__init__.py:115-128`).
- **Blocked results skip it.** `_pre_dispatch_guards` returns `blocked` and `handle_function_call` returns at `model_tools.py:926-929` through `_emit(... status="blocked")` — `post_tool_call` fires, `transform_tool_result` does not. The agent loop's primary path is `agent/tool_executor.py`: its `pre_tool_call` block becomes `_blocked_tool_result` (`:609-619`, emit at `:618`), which likewise never reaches the transform. So `pre_tool_call` plugin blocks cannot be post-processed today.
- `post_tool_directive` is observer-only, fired once per dispatched call with `directives[]`, `delivered`, `selected_directive_id`, `suppressed_directive_ids` (`hermes_cli/plugins.py:112-125`); returns ignored.
- Core approval refusals already carry parts 1-2 (`~/projects/hermes-agent/tools/approval.py:635-644` `_STOP_COMMAND`/`_STOP_ACTION`; `:649-658` transport denial, `:675-676` CLI denial): "BLOCKED… The user has NOT consented… Do NOT retry… wait for the user". Parts 3-4 are absent or inconsistent; `block_message` (`:534-536`) tells the model to set `approvals.<key>: approve`, which is an instruction to edit config, not to ask.
- `loop-guard` (`~/projects/hermes-plugins/plugins/loop-guard/__init__.py`) emits `"LOOP GUARD: " + json(blocker)` as a `pre_tool_call` block (`:196-197`); blocker = `{invariant, approaches, next_step}` (`:164-168`). Parts 1 and 3 are present, retry and addressee implicit. Its `after()` skips any result containing `LOOP GUARD:` (`:227-232`).
- `auto-review-classifier` (`~/projects/hermes-plugins/plugins/auto-review-classifier/__init__.py`) returns `{"action": "block"|"approve", "message": "auto-review-classifier: operation classified risky; human review required.", rule_key}` (`:318-323`). Only the `deny` policy block reaches the model; `approve` goes to the native prompt.
- Gap: no shared envelope, no catalog for ordinary tool failures, and no way to normalise plugin blocks after the fact.

## Design
New plugin `plugins/tool-steering/` (hooks: `transform_tool_result`, `post_tool_directive`, `transform_tool_refusal` once H1 lands; tool: `steering_status`).

**Envelope (v1).** Plain text, appended after the original text; the original is never edited or truncated:
```
STEER[v1] <class>
Happened: <what, from tool name + catalog slot values only>
Retry: no — <why> | yes — after <condition> | once — with <changed input>
Instead: <sanctioned alternative: tool + argument shape, or "none">
Ask: <the user / operator and what to ask> | nobody — continue
```
- JSON-object results get the same string under a `"steering"` key (existing keys preserved, `ensure_ascii=False`), because many Hermes tools return JSON strings; non-JSON results get `"\n\n"+envelope`. The `STEER[v1]` marker makes the transform idempotent.
- **Catalog, not generation.** A static table of classes keyed by `(tool, error_type or regex over error_message)`: `approval_denied`, `approval_timeout`, `no_human_present`, `path_not_found`, `patch_no_match`, `file_too_large`, `mcp_auth_required`, `mcp_transport_down`, `terminal_cwd_missing`, `plugin_block_loop`, `plugin_block_review`. Each row holds the four template strings. Unknown failures are left untouched — no fabricated alternative. `Instead:` may only name a tool that is in the session's resolved tool names (check against `tools.registry`; if unavailable, say "none" rather than name it).
- **Approval rows complete, not replace.** For core `BLOCKED:` text containing "Do NOT retry" the `Retry:` line is already satisfied; the envelope adds `Instead:` and `Ask:` only (header `STEER[v1] approval_denied (partial)`).
- **Single owner / no duplicates** (the coordination contract):
  1. `tool-steering` is the only plugin that registers `transform_tool_result` for *failure* results and `transform_tool_refusal`. `security-guidance` stays success-only; no conflict because first-string-wins never sees two failure rewriters.
  2. `loop-guard` and `auto-review-classifier` keep authoring their own block text (they own the state) but adopt the envelope in a one-line follow-up change using a vendored 15-line `format_steering()` (plugins stay independently installable — no cross-plugin import). Until then `tool-steering` recognises the legacy prefixes `LOOP GUARD:` and `auto-review-classifier:` and treats them as class `plugin_block_*`, appending only the missing `Retry`/`Ask` lines.
  3. Any result already containing `STEER[v1]` is skipped. `post_tool_directive` feeds a bounded `{tool_call_id → origin}` map so `steering_status` can show which plugin steered each call and which were skipped as already-steered (`directives[].origin`, `directive_id` prefix `loop:`).
- **Loop-guard interaction.** `loop-guard.after()` hashes the result seen by `post_tool_call`, which fires *before* `transform_tool_result` (`model_tools.py:944-945`), so appended steering cannot perturb repeat detection. Steering never counts against `redirect_cap`; it is not a directive.
- **Config** (`config_schema`): `mode: shadow|apply` (default `shadow`), `max_envelope_chars: 700`, `classes: {<name>: bool}` (all true), `extra_catalog: <abs path to YAML>` (optional, validated; unknown keys fail registration with the key named), `shadow_log: <path or empty>` (default `$HERMES_HOME/plugins/tool-steering/shadow.jsonl`, 1 MiB cap, one rotation).
- **Shadow mode.** Computes the envelope and records `{ts, tool, class, would_append_chars, already_steered}` without touching the result. Redaction: only catalog text and the tool name are logged; raw error text is never copied (it is already in the result and may hold secrets).
- **Failure modes.** Catalog or template error → return `None` (result untouched), log once per class. Result larger than 256 KiB → skip. Non-string result → skip. Every callback is wrapped; the hook is in `_HOOK_TIMEOUT_BOUNDED_HOOKS` (`hermes_cli/plugins_dispatch.py:41-46`), so work stays O(len(error_message)).

## Host changes
**H1 (P2, optional until then): new hook `transform_tool_refusal`.** Add to `VALID_HOOKS` (`hermes_cli/plugins.py:110`) and `_HOOK_TIMEOUT_BOUNDED_HOOKS` (`hermes_cli/plugins_dispatch.py:41-46`). Fire in `model_tools.handle_function_call` at the `blocked is not None` branch (`:926-929`, guard from `_pre_dispatch_guards` at `:750`) and in `agent/tool_executor.py` `_blocked_tool_result` (`:609-618`, emit at `:618`), which covers scope, plugin, and guardrail blocks on the agent-loop path. Kwargs: `tool_name`, `args`, `message`, `status` (`"blocked"`), `origin` (`plugin_hook|approval|guardrail`), `directive_id`, plus the id fields built from `ref` (see Risks). Return a non-empty string to replace; first valid string wins; fail-open.
Why not reuse `transform_tool_result`: security-guidance's warn mode would then fire on a write that never ran, and `post_tool_call` already carries `status="blocked"` while the transform path has no `status` override (`:844` derives it from the result via `_tool_result_observer_fields`).

## Phases
- **P0 — catalog + shadow.** Plugin skeleton, catalog for the eleven classes, shadow log, `steering_status`. Acceptance: with the plugin in `shadow`, a failing `read_file` of a missing path leaves the result byte-identical and `steering_status` lists one `path_not_found` would-append; a clean `terminal` call logs nothing and adds no latency beyond one `has_hook` check.
- **P1 — apply on the executed path.** `mode: apply`. Acceptance: missing-path result gains a `steering` key with all four lines; approval-denied text keeps its original body verbatim and gains only `Instead`/`Ask`; unknown error classes unchanged; second invocation on an already-steered string is a no-op.
- **P2 — blocked path (H1).** Land the seam; `tool-steering` registers `transform_tool_refusal`. Acceptance: a `loop-guard` block and an `auto-review` `deny` block each reach the model with exactly one `STEER[v1]` header and the original text first.
- **P3 — adopt envelope upstream in sibling plugins.** `loop-guard` and `auto-review-classifier` emit the envelope themselves; legacy-prefix recognition stays for one release. Acceptance: the conformance test below passes for all three emitters.

## Tests
`tests/test_tool_steering.py` (real loaders via `load_plugin`, like `tests/test_loop_recovery.py:294`; no model):
- catalog class → envelope has four labelled lines in order, ≤ `max_envelope_chars`;
- JSON-object result keeps all original keys and gains `steering`; non-JSON appends; idempotent on re-run;
- shadow mode returns `None` and logs; `apply` returns the string;
- two steering sources (loop-guard directive + tool-steering) yield one header (dispatch through `hermes_cli.plugins._dispatch_pre_tool_call_hooks` as the loop-guard tests do, then the transform hook);
- `Instead:` omits a tool name not in the registry;
- `loop-guard` repeat counters are unchanged with the plugin loaded (post_tool_call ordering);
- conformance: parse the output of loop-guard, auto-review `deny` and tool-steering with one regex and assert the four fields.
P2 adds a host-side test next to `tests/tools/test_model_tools.py`-style fixtures in the host repo for the new hook's fail-open and first-string-wins behaviour.

## Risks & open questions
- JSON-key insertion changes a result's shape; tools whose callers re-parse results (e.g. a plugin that `json.loads` the final result) might choke on an unexpected key. Mitigation: only add when the dict already has `error`/`success: false`; P1 acceptance runs the existing suites with the plugin on.
- `transform_tool_result` first-string-wins means a third plugin registering it before us silently starves steering; `steering_status` reports "transform ignored: another rewriter won" if our callback ran but the final result lacks our marker.
- Catalog drift against core strings (`approval.py`): matching on `BLOCKED:` + stable phrases, covered by a test that imports `tools.approval` constants.
- Resolved (verified): `tool_executor` does not call `_apply_transform_tool_result_hook`. Its non-blocked path calls `handle_function_call` (`agent/tool_executor.py:1536`), so the transform applies there; its blocked path returns from `_blocked_tool_result` (`:609-619`) and never reaches it, which is the H1 gap. `suppress_post_tool_call_hook()` (`:1535`) gates only `_emit_post_tool_call_hook` (`model_tools.py:673`), so `transform_tool_result` stays active inside that context.
- Open: whether the `Ask:` line should name `clarify` when available. Default: no — asking the user in plain text is always possible.
- Ceiling until H1 lands: a `pre_tool_call` block (loop-guard, auto-review `deny`, scope/guardrail blocks) reaches the model with no envelope. P0/P1 do not cover it; the plan states this rather than hiding it.
- Verified: `_CallIds` is not the parameter in `tool_executor._blocked_tool_result` (`agent/tool_executor.py:609`); that function takes `ref: _ToolCallRef` (`:249-257`). The H1 fire-site must build id kwargs from `ref.call_id` / `ref.task_id` (as `ref.emit_post` does at `:618`), not from `_CallIds.hook_kwargs()`.
- Name collision check: grep `tools.registry` for `steering_status` before P0 so the diagnostic tool does not shadow an existing name.

## Non-goals
- No LLM-generated advice; no classifier verdicts; no changes to approval *decisions* (steering is text only).
- No retries, no suppression of tool errors, no rewriting of successful results.
- No port of Grok Bot's computer-use deny lists or Tailscale copy.
- No change to `loop-guard` budgets or `auto-review` policy semantics.
