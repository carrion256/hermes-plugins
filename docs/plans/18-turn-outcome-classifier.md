# 18 turn-outcome-classifier
Status: proposed · Owner: — · Depends on: 01 (shared classifier client, label store, replay); 10 (`pre_stop` seam, only for turns with no code edits); coordinates with `continuation`, `loop-guard`

## Goal
When a turn ends, the agent says it is finished. Today nothing checks that statement unless the turn edited code. This plan adds a label-head classifier that reads the final assistant message and the last few tool results, then says which of five things happened: `done`, `blocked-needs-user`, `gave-up`, `claims-done-without-evidence`, `mid-task`. In shadow it only records verdicts. Once promoted it backs the `continuation` finish gate with one extra, evidence-corroborated nudge: "you stopped mid-task" or "you claimed completion but nothing in this turn verifies it". It never blocks a turn on its own word, never certifies completion, and never replaces the deterministic evidence checks in `continuation`.

## Evidence
- **Hermes core, verified:** the stop path runs verify-on-stop, then `pre_verify`, then the kanban stop guard (`agent/turn_stop_gates.py:109-112`). `pre_verify` fires only when the turn edited files: `_edited and has_hook("pre_verify") and attempt < max_verify_nudges()` (`turn_stop_gates.py:61`). Nudges are bounded by `agent.max_verify_nudges` (`agent/verify_hooks.py:29-34`).
- **droid, decoded:** the Local Signals rubric `intent_completeness` has an outcome head `Complete | Partial | Failed` (`plugins/meta-classifier/schemas.py:110-114`, copied from `notes/arch/classifiers.md` §5-7). It is observation only. droid also resamples on todo and completion reminders (`DEEP-FINDINGS.md:129`). Our five labels split `Partial`/`Failed` by what the model should do next, because the nudge differs.
- **Grok Build, source:** Grok Build suppresses the wake turn during goal loops (`re/docs/grok-build-harness.md:784`). It has no claim-versus-evidence classifier `[inferred]` from the doc search; nothing in the doc names one.
- **Grok Bot:** no turn-outcome evidence `[inferred: none decoded]`.

## Current state
- `continuation` already gates edited-code turns on deterministic evidence: `pre_verify` at `plugins/continuation/__init__.py:1332` checks the native todo ledger (`:1354-1368`), acceptance dispositions (`:1379-1385`), unbound required outcomes (`:1389-1404`), source drift (`:1405-1415`), then fresh native verification (`:1418-1428`). It returns `{"action": "continue", "message": ...}` or falls through to the review dispatch and finally `None` (`:1563`). `transform_llm_output` labels an unresolved outcome "blocked/unverified" (`:1565-1599`).
- `loop-guard` also registers `pre_verify` (`plugins/loop-guard/__init__.py:372`), so two plugins already coexist on that hook.
- Gaps, all verified by read: (1) a final message that claims completion is never read, only ledger state; (2) a turn with no edits never reaches `pre_verify`, so a research or ops turn that ends with "I'll now run the migration" stops silently; (3) a turn that ends with "I couldn't get this to work" is indistinguishable from a finished one; (4) `blocked-needs-user` is never recognised, so a legitimate question to the user is treated like any other stop.
- `post_llm_call` fires once per turn with `assistant_response`, `conversation_history`, `user_message`, `model`, `platform`, `turn_id` (`agent/turn_finalizer.py:423-433`). That is the observer input.

## Labels
Single-label head, schema `{"outcome": {"labels": {...}}}` in the same shape as `risk_schema` (`meta-classifier/schemas.py:69-73`).

| label | definition | positive | negative |
|---|---|---|---|
| `done` | The assistant states the task is complete and the tool results in the turn support it. | "Tests pass (12 passed). The fix is in `parser.py`." after a passing `pytest` result. | Same sentence after only an `edit_file` call. |
| `blocked-needs-user` | The assistant stops because it needs a decision, secret, approval or missing information only the user can give, and says so. | "Which of the two APIs should I target? I'll wait." | "I'm stuck, giving up." (no question, no ask) |
| `gave-up` | The assistant stops without finishing and without asking: it abandons, hand-waves or declares the task infeasible. | "I wasn't able to fix this; you may want to try upgrading the library." | "I can't proceed without the staging URL. Please send it." |
| `claims-done-without-evidence` | The assistant says it is complete, fixed or verified, but no tool result in the window verifies that claim. | "All tests pass now." when the last tool result is a file write. | "Tests pass." right after a `pytest` result with exit code 0. |
| `mid-task` | The assistant ends on a statement of next intent or an unfinished step and the work is plainly not finished. | "Now I'll update the remaining callers." then no tool call. | "That's all the callers updated." |

Precedence for the human labeller is `blocked-needs-user` > `claims-done-without-evidence` > `gave-up` > `mid-task` > `done`. The classifier head is single-label, so a tie is the service's call; the replay report shows confusion between these pairs.

## Input contract
- **Hook:** `post_llm_call` for shadow and for all turns (`turn_finalizer.py:423`). Promoted action uses `pre_verify` (`continuation` pattern, `turn_stop_gates.py:68-73`), which also carries `final_response` and `changed_paths`.
- **Fields sent**, as one JSON object serialised into `text`:
  - `final`: the assistant's last message, head 1 000 + tail 3 000 characters with `…[n omitted]…` between, so a long summary keeps both the opening claim and the closing sentence.
  - `tools`: the last 6 tool rows of the turn as `{tool, status, exit_code|null, summary}`, where `summary` is the first 300 characters of the redacted result. `status` and `error_message` come from the row itself.
  - `edited`: `bool(changed_paths)`; `task`: the user message, clipped to 800 characters.
  - `todo_open`: count of open native todo items, read through `agent` when available, else `null`.
- **Budget:** at most 6 000 characters of `text`, well under the service limit `MAX_TEXT = 100000` (`meta-classifier/client.py:17`). Over budget: shrink `tools` summaries to 150 characters, then drop the oldest tool rows. Never clip `final`'s tail.
- **Redaction:** the whole `text` goes through `agent.redact.redact_for_egress` (the call `loop-guard` and `auto-answer-classifier` use, `auto-answer-classifier/__init__.py:78`). The redacted string is sent, not dropped, because the verdict does not need secret bytes. If the redactor import fails, no request is made.
- Sub-agent turns (`_persist_disabled`, detached forks) are skipped, matching `turn_finalizer.py:421-422`.

## Design
New plugin `plugins/turn-outcome/` (hooks: `post_llm_call`, `pre_verify`, `on_session_reset`, `on_session_finalize`; tool: `turn_outcome_status`). It uses the vendored client from plan 01 P1 (`Classifier.classify`, 45 s cooldown). No cross-plugin import.

Config (`config_schema`):
- `mode: shadow | apply | off` (default `shadow`).
- `endpoint`, `api_key_env` (same defaults as `subagent-effort/plugin.yaml`).
- `timeout_s: 2.5`; `min_final_chars: 40` (shorter finals are not sent).
- `act_on: [mid-task, claims-done-without-evidence]` (default empty list in `shadow`; the list is the only labels that may nudge).
- `nudge_budget: 1` (per turn, additionally capped by `agent.max_verify_nudges`).
- `shadow_log: $HERMES_HOME/logs/turn-outcome.jsonl` (1 MiB cap, one rotation).

Shadow path: `post_llm_call` hands the input to a bounded worker (`BoundedSemaphore`, fail-open on overflow, the `subagent-effort` pattern at `subagent-effort/__init__.py:144-147`) and returns immediately. The turn is already over; nothing waits. The row is `{ts, session_id, turn_id, model, label|null, state: classified|unavailable|skipped, reason, edited, final_chars, tool_rows, latency_ms}`. The text sent is not stored; the label row stores a SHA-256 of the redacted input (plan 01's content-free rule).

Apply path (`pre_verify`, only when `mode: apply`): run after the deterministic checks fall through, so a `continuation` blocker is never second-guessed. Because both plugins register `pre_verify`, P1 first verifies how `get_pre_verify_continue_message` combines two plugins' returns (not read yet). Each label maps as follows, and every mapping requires a deterministic corroboration read from the transcript, never the verdict alone:
- `claims-done-without-evidence`: nudge only if the window has no tool row with status ok whose tool is a terminal-like check (`status == ok` and `exit_code == 0`) newer than the last mutating tool row. Message: "You said the work is done, but no tool result in this turn verifies it after your last change. Run the check or state what is unverified."
- `mid-task`: nudge only if the final message has no question mark and the turn issued at least one tool call. Message: "You ended on a next step you haven't taken. Take it, or say what is blocking you."
- `gave-up`: never nudges. It is recorded and, when `transform_llm_output` is extended in P3, labelled "stopped without completing" so the stop is not silent.
- `blocked-needs-user`, `done`: pass through, no ceremony.

Clean path adds no ceremony: `done` and unavailable verdicts return `None` from `pre_verify`.

## Decision wiring
- **Consumer:** `continuation`'s finish gate (`continuation/__init__.py:1332`, falling through at `:1563`) and the host nudge budget (`turn_stop_gates.py:61`, `verify_hooks.py:29`). The classifier registers its own `pre_verify` rather than patching `continuation`, because plugins load independently and `continuation` is evidence-bound by design.
- **Shadow mode:** observes only. It returns `None` from every hook. `turn_outcome_status` lists recent verdicts and agreement with `continuation`'s own blockers (read from the visible final-response label `_NOTE_MARK`, `:1598`).
- **After promotion:** `act_on` labels may nudge once, subject to the corroboration above and `max_verify_nudges`. `continuation`'s hard blockers still win. Non-edit turns are only covered once plan 10's `pre_stop` seam exists; until then `post_llm_call` is observe-only for them.

## Data & evaluation
- **Sources:**
  1. Replay of real turns from the session store (`post_llm_call` rows reconstructed from transcripts; no new capture needed).
  2. Plan 01's label store with a new `kind: outcome` and `/classifier-label <ref> <label>`. A human labels the turn's correct class directly.
  3. Weak implied signals, reported separately and never used for thresholds: a user message in the next turn that matches "you didn't actually run" / "continue" / "keep going" after a `done` verdict; a `continuation` blocker (`record["blockers"]`) present at the final message.
  4. Synthetic: 20 hand-written finals per label, mutated by stripping the tool evidence, to test the `claims-done-without-evidence` boundary. Synthetic cases are a regression suite, not a gate.
- **Metric:** per-label precision and recall, plus the false-nudge rate on `done` turns.
- **Promotion gate** (P2 → P3): at least 300 human-labelled turns with at least 30 positives for each label in `act_on`; precision ≥ 0.90 at recall ≥ 0.50 for each `act_on` label; false-nudge rate on `done` turns ≤ 2 %; agreement is computed after the corroboration rule, not before. Any label failing the gate stays out of `act_on`.
- **Latency:** shadow is off-path. In apply the budget is `timeout_s` 2.5 s, p95 measured over the shadow rows. A timeout skips the nudge.

## Failure policy
- No key, no endpoint, transport error, timeout, cooldown: `label=null`, `state=unavailable`, no nudge, no retry beyond the client's one 429/503 retry.
- Unknown label or malformed reply: treated as unavailable, with the raw value logged as `reason` and never mapped to the nearest known label.
- Low confidence: the service returns labels, not probabilities (`meta-classifier/schemas.py:78-81`), so there is no threshold to apply. Instead a verdict is acted on only when the deterministic corroboration also holds. A disagreement is logged as `suppressed: no_corroboration`.
- Classifier says `done` but `continuation` holds a blocker: `continuation` wins; the row records the disagreement.
- Never fabricate a verdict; never synthesize `done` on failure.

## Host changes
None for P0-P2. P3 non-edit turns need plan 10's `pre_stop` seam; this plan does not define it.

## Phases
- **P0 — shadow.** Plugin skeleton, `post_llm_call` worker, shadow log, `turn_outcome_status`. Accept: a loopback classifier returns each label once and every row is written; a turn with the service down writes `unavailable` and the turn result is byte-identical; `PYTHONPATH=$HERMES_SOURCE pytest tests/test_turn_outcome.py -q` is green.
- **P1 — replay and labels.** Replay CLI over stored sessions, `kind: outcome` labels, confusion report. Accept: replay over the stored sessions prints a label × label matrix; the `pre_verify` combination order is documented in this file.
- **P2 — corroboration dry run.** Compute what the apply path would have nudged, still returning `None`. Accept: the shadow log carries `would_nudge` and `suppressed: no_corroboration` counts; no turn behaviour changed.
- **P3 — apply.** `mode: apply` for labels that pass the gate. Accept: a synthetic claims-done turn gets exactly one nudge; a `done` turn gets none; `max_verify_nudges` is honoured; `loop-guard` and `continuation` tests are unchanged and green.

## Tests
`tests/test_turn_outcome.py` with the real native loader and a schema-dispatching fake `urlopen` (the pattern in `tests/test_meta_classifier.py`; `tests/conftest.py:20-38` blocks real sockets):
- each label returns through shadow and is logged; text sent contains no staged secret;
- unknown label, timeout and missing key all yield `unavailable` and no nudge;
- apply: `claims-done-without-evidence` nudges only when no passing check follows the last mutation; `mid-task` does not nudge when the final message is a question;
- `gave-up` never nudges;
- the worker overflow path records `overloaded` and never blocks `post_llm_call`;
- each nudge test fails when the corroboration call is removed (checked once by hand).

## Risks & open questions
- **Decision:** a separate plugin, not a change to `continuation`, to keep `continuation` free of a network dependency.
- **Resolved (read):** `get_pre_verify_continue_message` (`hermes_cli/plugins.py:2146-2164`) runs every `pre_verify` hook and returns the first non-empty `continue`/`block` message in registration order. A classifier nudge therefore appears only when `continuation` returns nothing, so `continuation` keeps precedence whenever it is registered first. P1 must assert that registration order in a test.
- A confident wrong `claims-done-without-evidence` makes a correct turn run an extra check. Mitigation: one nudge, corroborated, and bounded by `max_verify_nudges`.
- "Evidence" is the tool window, not the repo. A claim verified by an earlier turn looks unverified. P1 measures how often.
- Final messages in other languages or short acknowledgements are skipped by `min_final_chars`.
- Droid's `Complete/Partial/Failed` split lacks our `blocked-needs-user`; label agreement with droid's rubric is not expected.

## Non-goals
- Replacing or weakening any `continuation` blocker, or certifying completion.
- Choosing a model, effort or tools from the verdict.
- Rewriting the assistant's final message.
- Implementing the `pre_stop` seam (plan 10).
- Uploading any turn content anywhere.
