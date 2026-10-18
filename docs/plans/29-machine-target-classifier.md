# 29 machine-target-classifier
Status: proposed · Owner: — · Depends on: 01 (meta-classifier client, `addons.py` shadow pattern, signals log); 11 (machine-relay: `machine_id` argument and `list_machines`, which do not exist in Hermes yet); 14 (box-lifecycle: non-waking `describe`, used only for the wake-cost field); `tools/` and `hermes_cli/plugins.py` hooks (read-only).

## Goal
For each user turn, label where the work should run: `box` (the Hermes-hosted sandbox), `user-laptop` (the user's own computer, reached through plan 11), or `either`. In shadow the label is recorded next to where the agent actually ran each tool call. It answers one question before any target choice is automated: would this label have sent the work to the same place the agent or user did, and did that work succeed there? Nothing about targeting changes in the default configuration.

Design rule carried from plans 11 and Grok Bot: **box is the default. The laptop is used only when an explicit `machine_id` is passed.** A `user-laptop` label is advisory. It never selects a machine, and it never causes a tool call to be sent anywhere.

## Evidence
- **Box is the default; `machine_id` is the switch** (plan 11, verified read): `docs/plans/11-machine-relay.md:5` says the agent addresses a machine only by explicit `machine_id`, with Hermes' own host staying the default "box". Line 31 says `machine_id` is required even when `default_machine` is unset and only one machine exists; zero machines is an error that names `hermes machine enroll`; more than one machine without `machine_id` is an error.
- **Grok Bot, same switch** (grokbot-re, decoded): `re/docs/coordinator-and-box.md:15` (headline 4): "Local vs box" is a `machineId` switch, not a mode. Tools `Shell/Read/AwaitShell/CopyToBox/CopyFromBox/Computer/Screenshot` run on the user's desktop only when the agent passes `machineId`. The box is the agent's default computer.
- **Optional on send** (grokbot-re, decoded): `re/docs/agent-protocol.md:61`: `SendGrokBotUserMessage` has an optional `machine_id?`. Line 72: `UpdateSandMachineLocalToolPermission{machine_id,…}`. Line 74: error mapping, default RPC timeout 20 000 ms.
- **Credential and removal** (grokbot-re, decoded): `re/docs/agent-protocol.md:114-116` (§3.1): the machine id is `getMachineId()`; a credential is minted by `IssueGrokBotUserComputerCredential`; `FAILED_PRECONDITION` with `GrokBotUserComputerRemovedDetail` means stop. `local-exec-daemon.md:152-158`: the machine id is a UUIDv4 in the secret store (`cursor-machine-id`); presence comes from `WatchGrokBotUserComputerRequests`; `ListGrokBotUserComputers` backs the agent's `ListMachines` tool.
- **Cloud work does not need the laptop** (grokbot-re, decoded): `re/docs/lineage-backfill.md:402`: routines run in the cloud with the laptop closed. So "laptop offline" is a normal state, not an error, for `box` work.
- **Harness kinds are not targets** (grokbot-re, decoded): `re/docs/backend-api.md:174`: `GrokBotAgentHarnessKind{BOX,TEMPORAL}` is about where the loop runs. It is a separate axis from the machine a tool call touches, and this plan does not label it.
- **Hermes has no machine concept yet** `[unverified]`: `grep` for `machine_id` in `hermes-agent` returned no usable result in this pass (the tool call errored). Plan 11 is the proposal that would add it; until it ships, the only target Hermes can use is its own host, so the target fact is always `box` by construction.
- **Hooks** (verified): the meta-classifier addon header (`plugins/meta-classifier/addons.py:13-18`) lists `pre_tool_call` as an observer hook. `pre_llm_call` fires in `agent/turn_context.py:674-686`. Plan 28 cites both; plan 29 reuses them and adds no hook.
- **Effort classifier is not a target classifier** (verified): `plugins/subagent-effort/__init__.py:55-70` labels `simple|complex` only. Plan 29 does not reuse that schema.

## Current state
- No plugin records which machine a tool call ran on, because no tool takes a machine argument yet.
- Plan 11 is `proposed`. Its `machine_id` argument, `list_machines` tool and `machines` table do not exist in code.
- Plan 14 is `proposed`. Hermes has per-backend persistence and no non-waking status probe, so the wake cost of a `box` verdict cannot be read yet.
- The gap: nothing records whether a user's "run it on my laptop" intent matched the place the agent ran the work, or whether the work succeeded there.

## Design
A new `target` addon inside `meta-classifier`, following `addons.py`. No new client, no new log.

**Hooks.**
- `pre_llm_call` (`agent/turn_context.py:674-686`): the new user message, classified before the agent chooses any tool.
- `pre_tool_call` on any tool that accepts `machine_id` (plan 11): records the `machine_id` value the agent passed, or `none` when absent. The hook returns `None` and never rewrites arguments.

**Deterministic facts** (recorded, never judged):
- `actualTarget`: `laptop:<machine_id>` if the call carried a `machine_id` that resolved to an enrolled machine; `box` if the argument was absent; `unknown` if the argument could not be read. Absent means `box` by the plan 11 rule, so this value is never guessed.
- `laptopEnrolled`: count of enrolled machines from the plan 11 `machines` table (`0` means `user-laptop` is unreachable this turn; the label is still recorded).
- `boxState` (later, plan 14 P1+): non-waking `describe` result (`ABSENT|HIBERNATED|RUNNING|STARTING`), recorded so a `box` verdict on a hibernated box is visible as a wake cost. Not used by the label.

**Classifier head** for `box|user-laptop|either` on the user message text only, windowed to `addon_chars`. The head never sees tool output or the agent's chosen target, so it cannot learn from the agent's choice.

**Shadow row**: `{addon: "target", sessionId, turnId, label, source, actualTarget, laptopEnrolled, outcome}`. `outcome` is `ok|tool_error|n/a` from the tool result of the first target-bearing call in the turn. `n/a` when no tool call in the turn accepted `machine_id`.

**Threading.** Off-thread on a bounded worker (the `subagent-effort` pattern). The turn never waits in shadow.

**Config** (`plugins.entries.meta-classifier.settings`, addon `target`): `target_mode: shadow` (the only value this plan ships); `target_timeout_s: 2.5`; `target_reclassify_on: [new_user_turn]`.

## Host changes
None in this plan. P0–P2 read existing hooks and plan 11's tables after plan 11 ships. Any behaviour change (an advisory note in the prompt, or a default-target change) is out of scope here and needs its own plan and owner approval.

## Phases
- **P0 deterministic actual target.** Gated on plan 11 P1 (the `machine_id` argument and `machines` table existing). Register the `target` addon with the `pre_tool_call` observer. Accept: a call with `machine_id` set to an enrolled id records `actualTarget: laptop:<id>` with zero HTTP calls; a call with no argument records `box`; a call whose argument fails to parse records `unknown`, never `box`.
- **P1 classifier head and shadow rows.** Add `TARGET_SCHEMA` to `schemas.ADDON_SCHEMAS` with the three labels. Accept: with a fake service a "pull the failing test log from my laptop's build folder" turn records `user-laptop`; a dead service writes `error: classifier_unavailable_or_invalid` and no label; latency unchanged.
- **P2 outcome link.** Fill `outcome` from the first target-bearing tool result. Accept: a `laptop:<id>` call that returns `FAILED_PRECONDITION` (removed machine) records `outcome: tool_error`, and the row is not counted as a box miss.
- **P3 evaluation report.** New command `hermes target-eval` prints the confusion matrix between `label` and `actualTarget`, the outcome rate per label, and the gate verdict. Accept: reports NOT_QUALIFIED below the gate minimum.
- **P4 advisory note (proposed, owner decision required).** When the label is `user-laptop`, `laptopEnrolled > 0`, and the agent did not pass `machine_id`, inject one advisory `context` note through `pre_llm_call` asking whether the user meant their computer. The note never selects a machine. This phase is blocked until the P3 gate passes and the owner approves.

## Tests
- `tests/test_meta_classifier_target.py` (real native loader, `HERMES_SOURCE=~/projects/hermes-agent`, schema-dispatching fake `urlopen`; `tests/conftest.py:27-30` blocks real connects): deterministic `actualTarget` needs zero calls; absent `machine_id` records `box`; unparseable argument records `unknown`; classifier label recorded; unknown label becomes `error`, never a default.
- Redaction: a secret in `user_message` never appears in the transmitted text or the row.
- Concurrency: five simultaneous turns, at most 4 workers run; the fifth records `reason: overloaded`.
- Shadow invariant: `pre_tool_call` returns `None` for every tool call, so no `machine_id` is added, removed or changed.
- Offline laptop: `laptopEnrolled: 0` with a `user-laptop` label records the label and sets no target.

## Labels
| label | definition | positive | negative |
|---|---|---|---|
| `box` | The work needs only the Hermes sandbox: repo checkout there, network, long-running jobs, or nothing local. | "run the migration and tell me when it passes" | "open the spreadsheet on my desktop" |
| `user-laptop` | The work needs something only the user's own computer has: local files, local apps, a browser session, a local device. | "send me the PDF in my Downloads folder" | "run the migration on the server" |
| `either` | No environment dependency; either target would do. | "summarise this text" | "copy my Downloads folder to the box" |

`user-laptop` is advisory in every phase. A label never causes a `machine_id` to be filled in, changed or chosen.

## Input contract
- Hooks: `pre_llm_call` (`agent/turn_context.py:674-686`) and `pre_tool_call` for any tool that accepts `machine_id` (plan 11). Both are existing hook points; no new hook name.
- Fields used: `session_id`, `turn_id`, `user_message` (text only), the `machine_id` argument of tool calls (value or absence only), `laptopEnrolled` from plan 11's `machines` table, and `boxState` from plan 14 `describe` once that exists.
- Text sent to the service: `message_text(user_message)` only, windowed to `addon_chars`. Messages under 8 stripped characters are skipped.
- Tool arguments other than `machine_id`, and tool output, are never sent.
- Redaction: `egress()` runs before windowing; a `ValueError` means no call and an error row.

## Decision wiring
- Consumer: none in shadow. `actualTarget` and the label are written to the signals log only.
- The verdict never changes a `machine_id` argument, the default machine, the `machines` table or any tool result. `pre_tool_call` returns `None`.
- A `user-laptop` label on a turn with no `machine_id` is a candidate for the P4 note only. It is never a route.
- Any promoted target choice is a separate plan. It would have to respect the plan 11 rule that the agent addresses a machine only by explicit `machine_id`, and the plan 11 rule that no automatic machine selection happens.

## Data & evaluation
- Sources: (1) replay of real sessions through `meta-classifier-replay`, with the existing fixtures as a smoke set; (2) human labels, recorded through plan 01 P3's `/classifier-label` (proposed; adds `kind: target`, `given`, `label: correct|wrong`) on the shown row; (3) 20 hand-written prompts per label, with at least 5 mentioning a named local file, app or device. Implied labels (the operator later re-ran the work on another machine) are reported separately and never feed the gate.
- Metrics: per-label precision and recall against `actualTarget`; outcome rate (`ok` share) per label; share of turns where `laptopEnrolled: 0` and the label is `user-laptop`.
- Gate to leave shadow: `user-laptop` precision ≥ 0.90 (a wrong laptop label sends the operator to a laptop that was not needed); `user-laptop` recall ≥ 0.70 (a missed laptop label is a box run that cannot see local files, which fails visibly as a tool error); on at least 100 human-labelled turns across at least 15 sessions, each label at least 20 cases.
- Latency: classifier p95 ≤ 2.5 s, off-thread; adds 0 ms to the turn.
- Harm model: a wrong `box` verdict costs a failed call on a box that cannot reach local files. A wrong `user-laptop` verdict costs an unnecessary advisory note (P4) or a wasted label. Neither path moves work to the laptop.

## Failure policy
- Timeout, transport error, 429/503 after one bounded retry, unusable reply or unknown label: write an `error` row with no label. Never default to `box`, never carry a previous label forward.
- Deterministic `actualTarget` never fails open to `box`: if the argument read raises, record `unknown`.
- A dead classifier never delays a turn (cooldown in shared `client.py`).
- `user-laptop` is never produced from absent or ambiguous data. With `laptopEnrolled: 0`, the label is recorded but flagged; the advisory phase (P4) is skipped.
- Offline laptop: a `laptop:<id>` call against a removed or offline machine fails with the plan 11 error vocabulary (`removed`, `service_unavailable`, `machine_mismatch`). The plugin records it and does not retry on another machine. Plan 11 owns retry behaviour.
- Fail-open: with `target_mode: shadow` (or the addon disabled), existing behaviour is unchanged.

## Risks & open questions
- **Plan 11 is not implemented.** Until its `machine_id` argument and `machines` table exist, P0–P3 cannot run. This plan records that dependency rather than faking a `laptop` path with no argument to read.
- **Wording in the Grok Bot error string is ambiguous.** The text "Use your own computer instead by omitting machineId" (decoded in `coordinator-and-box.md:15`) reads as if omitting the argument means the user's computer. Headline 4 of the same document says the default computer is the box. This plan follows headline 4 and the plan 11 rule. Recorded so the ambiguity is not copied into Hermes wording.
- **Two-machine default.** With more than one enrolled machine, a `box` verdict is still correct for the agent's default, but users who usually mean their laptop will see mismatches. The gate measures agreement with the label, not with personal habit.
- **Box wake cost.** A `box` verdict on a hibernated box implies a wake. Plan 14 `describe` is non-waking, so the cost is recorded with `boxState`, but cost is not measured in this plan.
- **Privacy of labels.** Laptop-related prompts often name local files. The redaction rule covers the transmitted text; the signals log keeps `actualTarget` but not file names from tool arguments.
- **Machine ids in logs.** `actualTarget` contains a machine id. The row keeps the id only, never the plan 11 credential.

## Non-goals
Automatically choosing a `machine_id`, filling one in, or changing the default machine. Routing any tool call by label. Blocking or prompting the user from a classifier verdict in shadow. Merging with `subagent-effort`, with the harness kind (`BOX|TEMPORAL`), or with the plan 28 fit label. Any change to plan 11's transport, plan 14's lifecycle, or the Hermes tool argument schema.
