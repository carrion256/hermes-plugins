# 28 delegation-fit-classifier
Status: proposed · Owner: — · Depends on: 01 (meta-classifier client, `addons.py` shadow pattern, signals log); host `tools/delegate_tool.py` and `tools/async_delegation.py` (read-only consumers); `plugins/subagent-effort` (effort only, boundary below).

## Goal
For each parent-agent turn, label whether the work should be done `do-inline`, `delegate-parallel`, `delegate-background`, or `needs-user`, and record that label next to what the parent actually did. In shadow the verdict is data only. It answers one question before any delegation is automated: would this label have chosen the same mode the agent or user picked, and did that choice finish cleanly? Nothing about delegation changes in the default configuration.

## Evidence
- **`delegate_task` surface** (verified): `tools/delegate_tool.py:410` takes `role`, `background`, `output_schema`. `background` is normalised at `:439` via `is_truthy_value`. The comment at `:437-438` says a batch is one async unit that joins on every child and re-enters as a single consolidated message, so single-task and batch `background` share one path.
- **Children cannot ask the user** (verified): `delegate_tool.py:237-238` passes `skip_memory=True, clarify_callback=None` to the child. `:512-517` states children cannot call `clarify`, `memory` or `cronjob`, and `delegate_task` is refused at the depth limit (`:441-448`). Any `needs-user` outcome therefore has to be raised by the parent.
- **Durable async ledger** (verified): `tools/async_delegation.py` persists dispatches and completions to `state.db` table `async_delegations`. Functions: `_db_path` (`:80`), `_initialize_schema` (`:101`), `_capture_routing_origin` (`:123`, snapshots scope, user and parent-thread identity), `_persist_dispatch` (`:134`), `_persist_completion` (`:183`), `record_unit_child` (`:193`). Completion status gives the outcome signal this plan needs.
- **Clarify path** (verified): `tools/clarify_gateway.py` holds `_ClarifyEntry` (`:20`), a process-wide `_lock` RLock (`:32`), `_entries` keyed by `clarify_id` (`:33`) and a per-session FIFO `_session_index` (`:34`). This is the only host mechanism that waits on a human mid-turn.
- **Effort is not fit** (verified): `plugins/subagent-effort/__init__.py` emits `{"difficulty": ["simple","complex"]}` (`:61`), bounded by `_MAX_CLASSIFIERS=4`. It grades how hard a subagent's goal is, not whether to delegate or to whom the answer belongs. Plan 28 must not reuse its schema as a fit head.
- **Existing per-goal head** (verified): the WIP `COMPLEXITY_SCHEMA` (`plugins/meta-classifier/schemas.py:346-352`) labels a delegate goal's effort, shadow-wired at `addons.py:227-230`. It is a sibling of this plan, not its replacement.
- **Droid** (observed/source): droid's Router row (`notes/arch/classifiers.md:139`) rates a label head per candidate model as "Poor" for *model* choice, not delegation. Its transferable point is the recommendation to classify the task and keep a static capability table, which supports labelling mode and leaving capability to the operator. Droid has no delegation-fit verdict to copy.
- **Grok Bot** `[inferred]`: no client-visible parallel/background decision. Not used as evidence for the labels.

## Current state
- Delegation mode is chosen by the parent model from tool arguments: `background=true`, or several `tasks[]` in one call (`delegate_tool.py:410-439`). No plugin observes or scores that choice.
- `needs_user` does not exist in `tools/` (repo-wide grep finds nothing). A `needs-user` label has no host consumer today; the nearest mechanism is the `clarify` gateway, which children cannot call.
- The meta-classifier addons pattern already gives a shadow row, redaction (`egress`), bounded text (`window`) and one JSONL write per call (`addons.py:60`, `classify()`).
- Gap: nothing records whether a parent's delegate-or-inline choice matched the label a classifier would have produced, or whether the chosen mode completed.

## Design
A new `fit` addon inside `meta-classifier`, following `addons.py`. No new client, no new log.

**Hooks.** Two observations, both existing plugin hooks:
- `pre_llm_call` (`agent/turn_context.py:674-686`): the parent's new user message. Classify the task as a whole, before the parent chooses a mode.
- `pre_tool_call` on `delegate_task`: the mode actually chosen (`background`, count of `tasks[]`, `role`). Record it on the same row.

**Deterministic facts** (recorded, never judged): `actualMode` is `inline` when no `delegate_task` call happened in the turn, otherwise `delegate-parallel` (batch > 1) or `delegate-background` (`background` truthy). `needs-user` is the only label that can be computed without the classifier when the parent's last tool result is an unresolved clarify entry (`clarify_gateway._session_index` non-empty for the session). That is a state fact, so it is recorded with `source: deterministic`.

**Classifier head** for `do-inline|delegate-parallel|delegate-background|needs-user` on the turn text only, windowed to `addon_chars`. The model sees the user message and no tool output, so it cannot learn from the parent's choice.

**Shadow row**: `{addon: "fit", sessionId, turnId, label, source, actualMode, background, taskCount, role, outcome}`. `outcome` is filled later from `async_delegations` status (`completed|failed|cancelled`), joined by session and dispatch id. A row with no delegation has `outcome: n/a`.

**Threading.** Off-thread on a bounded worker (the `subagent-effort` pattern). The turn never waits in shadow.

**Config** (`plugins.entries.meta-classifier.settings`, addon `fit`): `fit_mode: shadow` (only value this plan ships); `fit_timeout_s: 2.5`; `fit_reclassify_on: [new_user_turn]`.

## Host changes
None. P0–P2 are read-only observers of existing hooks and the existing `async_delegations` table. A promoted mode (the classifier steering `background`) would change host behaviour and is out of scope; it needs its own plan and owner approval.

## Phases
- **P0 deterministic actual mode and needs-user.** Register the `fit` addon with the `pre_tool_call` observer for `delegate_task` and the `pre_llm_call` pending-clarify check. Accept: a turn with `background=true` records `actualMode: delegate-background` with zero HTTP calls; a session with an open clarify entry records `needs-user` with `source: deterministic`.
- **P1 classifier head and shadow rows.** Add `FIT_SCHEMA` to `schemas.ADDON_SCHEMAS` with the four labels. Accept: with a fake service a "fix the flaky test and also update the changelog" turn records `delegate-parallel`; latency unchanged; a dead service writes `error: classifier_unavailable_or_invalid` and no label.
- **P2 outcome link.** Join rows to `async_delegations` completion status. Accept: a background run that fails appears with `outcome: failed`; a row with no ledger entry keeps `outcome: n/a`.
- **P3 evaluation report.** New command `hermes fit-eval` prints the confusion matrix between label and `actualMode`, plus outcome rates per label, and the gate verdict. Accept: reports NOT_QUALIFIED with fewer than the gate minimum.

## Tests
- `tests/test_meta_classifier_fit.py` (real native loader, `HERMES_SOURCE=~/projects/hermes-agent`, schema-dispatching fake `urlopen`; `tests/conftest.py:27-30` blocks real connects): deterministic `actualMode` needs zero calls; `needs-user` deterministic when a clarify entry is open; classifier label recorded; unknown label becomes `error`, never a default.
- Redaction: a secret in `user_message` never appears in the transmitted text or the row.
- Concurrency: five simultaneous turns, at most 4 workers run; the fifth records `reason: overloaded`.
- Shadow invariant: `pre_tool_call` for `delegate_task` returns `None` in every case, so no argument is rewritten.

## Risks & open questions
- **Boundary with `subagent-effort`.** Effort (how hard a goal is) and fit (whether and how to delegate) are different questions. `subagent-effort` answers effort only and must not gain a fit label. Recorded here so the two schemas are not merged by accident.
- **Children cannot ask.** `needs-user` describes the parent's situation. A child that needs a human cannot raise it; the parent must. If a child's goal is inherently interactive, the label is `needs-user` only when the parent can see the question.
- **Background is one async unit.** A batch with one slow child delays the whole consolidated re-entry (`delegate_tool.py:437-438`). The label does not weight this; `delegate-parallel` vs `delegate-background` is recorded but cost is not measured here.
- **Outcome is lifecycle only.** `completed` does not mean the delegation was correct. Quality needs human labels (plan 01 P3 `/classifier-label`, proposed; adds `kind: fit`).
- **Mode is partly the operator's preference.** Some users want every multi-step task delegated regardless of fit. The shadow gate measures agreement with the label, not with the user's preference.

## Labels
| label | definition | positive | negative |
|---|---|---|---|
| `do-inline` | Small, sequential or interactive work; the parent should just do it. | "rename the config key and update the test" | "audit five modules in parallel and report" |
| `delegate-parallel` | Several independent subtasks whose results the parent needs together. | "check the three services for the same bug and compare" | one file, one edit |
| `delegate-background` | Long work the parent does not need to wait on; result can arrive later. | "run the full migration and tell me when it passes" | a question needing an answer now |
| `needs-user` | The task cannot proceed without a human decision or missing input the parent must ask for. | "which of the two schemas should win?" | a decision the parent can make from the repo |

## Input contract
- Hooks: `pre_llm_call` (in `VALID_HOOKS`, `hermes_cli/plugins.py:133`) and `pre_tool_call` for `delegate_task`.
- Fields used: `session_id`, `turn_id`, `user_message` (text only), `delegate_task` arguments `{background, role, len(tasks)}` (no goal text is sent from tool arguments), and the open-clarify flag from `clarify_gateway`.
- Text sent to the service: `message_text(user_message)` only, windowed to `addon_chars` (12000). Messages under 8 stripped characters are skipped.
- Redaction: `egress()` before windowing; a `ValueError` means no call and an error row (`addons.py:65-69`). Tool arguments are not sent.

## Decision wiring
- Consumer: none in shadow. `actualMode` and the label are written to the signals log only.
- The verdict never changes `background`, `tasks[]`, `role` or the clarify queue. `pre_tool_call` returns `None` on `delegate_task`.
- A future promoted mode is a separate plan. It would have to respect the child restrictions above, and it would move a decision the parent currently owns.

## Data & evaluation
- Sources: (1) replay of real sessions through `meta-classifier-replay`, with the existing fixtures as a smoke set; (2) human labels via plan 01 P3's `/classifier-label` (proposed; adds `kind: fit`, `given`, `label: correct|wrong`) on the shown row; (3) 20 hand-written prompts per label. Implied labels (the operator later changed the mode) are reported separately and never feed the gate.
- Metrics: per-label precision and recall against `actualMode`, and the outcome rate (`completed` share) per label. Cost is not measured in this plan.
- Gate to leave shadow: `needs-user` precision ≥ 0.90 (wrong `needs-user` interrupts work); `delegate-background` agreement with `actualMode` ≥ 0.70 on at least 150 human-labelled turns across at least 20 sessions, each label at least 20 cases.
- Latency: classifier p95 ≤ 2.5 s, off-thread; adds 0 ms to the turn.

## Failure policy
- Timeout, transport error, 429/503 after one bounded retry, unusable reply or unknown label: write an `error` row with no label. Never default to `do-inline`, never carry a previous label forward.
- Deterministic `actualMode` never fails: if the tool-argument read raises, record `actualMode: unknown`, not `inline`.
- A dead service never delays a turn (cooldown in shared `client.py`).
- `needs-user` is never fabricated from classifier output alone; the deterministic clarify flag is the only source of that label in shadow.

## Non-goals
Automatically choosing `background`, batch size or `role`. Blocking or prompting the user from a classifier verdict. Merging with `subagent-effort` or with the per-goal `COMPLEXITY_SCHEMA`. Judging the quality of a delegate's answer. Any change to `delegate_tool.py` or `async_delegation.py`.
