# 27 task-routing-classifier
Status: proposed · Owner: — · Depends on: 01 (meta-classifier client, egress/window helpers, signals log, labels), `plugins/model-routing` (role table), `plugins/prewalk` (executor target). Host seam only for P4, and only if the owner lifts the README no-routing rule.

## Goal
Label every session's task as `trivial`, `standard`, `deep-reasoning`, `long-context` or `vision`, and record which model role the operator's own table would give that label. In shadow the verdict only produces data: the label, the role the table maps it to, and what actually ran. This lets the owner see whether an operator-written `label → role` table would have been right before any model choice is made from it. The model never switches in this plan's default configuration.

## Evidence
- **droid Factory Router (`-m auto`)** (observed/source, `notes/arch/classifiers.md:19,76-88`): a `gpt-5.4-mini-fast` call before the first LLM call of a session returns per-candidate first-attempt scores; the selector takes the cheapest candidate scoring ≥ `QUALITY_THRESHOLD` (`droid-core/src/model-router/selector.ts:66`, reason labels `resolveSelectionReason` at `:131-158`). Reason labels are `Routine`, `TaskFit`, `Hard`, `OrgPolicy`, `VisionRequired` (`classifiers.md:86`). Cadence: one call per session, cached; it re-classifies when images arrive for an image-blind pick (`:87`).
- **droid's own fit verdict** (`classifiers.md:139`): a label head per candidate model is a "Poor" fit, because the service knows nothing about model capability. The recommended split is to classify the task (type, difficulty) and keep a static capability table. This plan is that split: the classifier labels the task, and the operator's `model-routing` table is the capability table.
- **Router fixtures in the WIP** (read): `plugins/meta-classifier/replay.py:23-26` defines the score bands and `:56-59` builds the router payload from fixtures only. The plugin does not implement the router (`replay.py:7-9`), and plan 01 lists the router as a non-goal.
- **Grok Bot**: `[inferred]` no client-visible task→model classifier. Model choice there is server-side.

## Current state
- `model-routing` resolves `role → {model, provider}` deterministically from an operator table (`plugins/model-routing/__init__.py:1-23`, resolver built at `:173-200`, fields `_ROLE_FIELDS` at `:36`). `route_apply` writes explicit pins (`:295-342`). The README forbids any classifier-driven choice: "Nothing picks a model from classifier output, cost or quota" (`README.md:24,41,138`).
- `prewalk` switches planner→executor model after a validated handoff (`plugins/prewalk/__init__.py:1-33`). Its target comes from `target_model`/`target_provider` or the host `resolve_role_model` seam (`plugins/prewalk/plugin.yaml:20-27`). Neither plugin consumes a task label.
- The host `register_role_resolver` seam that `model-routing` registers through (`model-routing/__init__.py:274-278`) does not exist in `~/projects/hermes-agent`: a repo-wide search for `role_resolver` finds nothing, so `route_status` reports `host_seam: unavailable` today.
- The WIP has a per-delegate-goal head `COMPLEXITY_SCHEMA` (`trivial|moderate|deep`, `schemas.py:346-352`) wired as the shadow `complexity` addon on `pre_tool_call` (`addons.py:18,227-230`). `subagent-effort` has `{"difficulty": ["simple","complex"]}` (`subagent-effort/__init__.py:61`). Neither labels the session's top-level task, vision or context size.
- `pre_llm_call` fires with `session_id, task_id, turn_id, user_message, conversation_history, is_first_turn, model, platform, parent_session_id, sender_id` (`agent/turn_context.py:674-686`); a returned `{"context": ...}` is injected into the user message (`:663-717`).
- Gap: no record anywhere of what a task-fit label would have routed to.

## Design
Plugin only, shipped as a new `route` addon inside `meta-classifier`, following `addons.py` (`classify()` at `:60-78`, which redacts through `egress`, bounds through `window`, and writes one JSONL row). No new client, no new log.

**Labels are partly deterministic.** `vision` and `long-context` are facts, not judgments, and the classifier cannot see them. A pre-check computes them from the payload before any HTTP call:
- `vision`: the `user_message` content has an image part, or the session already carries one.
- `long-context`: estimated prompt tokens (`conversation_history` char count / 4, `[inference]` as a heuristic) ≥ `long_context_tokens`.
Only if neither holds does the service head choose among `trivial|standard|deep-reasoning`. Deterministic labels take precedence and are recorded with `source: "deterministic"`; they do not spend a classifier call.

**Config** (`plugins.entries.meta-classifier.settings`, addon name `route`):
- `route_label_roles: {}`: operator table `label → role name`, for example `{trivial: "cheap", deep-reasoning: "deep"}`. Role names must exist in `model-routing`'s `roles`; the addon only records them and never resolves a model.
- `route_mode: shadow` (`shadow` | `advise`); `advise` only after the P3 gate.
- `route_long_context_tokens: 100000`, `route_timeout_s: 2.5`.
- `route_reclassify_on: [images_added, model_changed]`, per the droid cadence (`classifiers.md:87`). Otherwise one call per session, on the first non-trivial-length user turn.

**Shadow row** (`route` addon record): `{addon: "route", sessionId, turnId, label, source: deterministic|classifier, mappedRole|null, actual: {model, provider}, firstTurn, textDigest}`. `actual` is read from the `pre_llm_call` `model` kwarg, with the provider left unknown if the host does not supply it. Rows are linked later to the observed outcome by `sessionId` (turn success is read from existing `post_llm_call`/`on_session_end` facts; no correctness claim).

**Threading.** In shadow the classifier call runs on a bounded daemon worker (the `subagent-effort` pattern, `_MAX_CLASSIFIERS=4`, `subagent-effort/__init__.py:67`), so the turn never waits. `advise` mode needs the verdict before the model call, so it is synchronous, first turn only, within `route_timeout_s`.

**Advise mode (post-gate).** `pre_llm_call` returns `{"context": "[route: this task looks like <label>; the operator table suggests role <role>. Advisory only; use model_role=<role> when you delegate.]"}`. It does not switch the session model, does not touch prewalk's target, and does not write pins.

## Host changes
None for P0–P3. P4 is optional and blocked on an owner decision: a `ctx.suggest_turn_model(role)` seam that applies a role for one turn. It needs `model-routing`'s resolver seam to exist first (see current state), would reverse README line 138, and is not planned here. No design for it is given beyond the name.

## Phases
- **P0 deterministic labels.** `route` addon registers `vision`/`long-context` pre-checks and a no-op classifier path. Accept: a turn with an image yields one `route` row with `label: vision, source: deterministic` and makes zero HTTP calls (loopback counter); a 120k-token history yields `long-context`.
- **P1 classifier head and shadow rows.** Add `ROUTE_SCHEMA` for `trivial|standard|deep-reasoning` to `schemas.ADDON_SCHEMAS`. Accept: with a fake service, a plain "rename x to y in foo.py" turn records `trivial`; the turn's latency is unchanged (worker thread); a dead service writes `error: classifier_unavailable_or_invalid` and no label.
- **P2 mapping and outcome link.** `route_label_roles` validated against configured roles; rows carry `mappedRole`. Accept: unknown role name fails registration naming the key (`model-routing` precedent); `hermes meta-classifier-replay --cases` lists route rows.
- **P3 evaluation report.** `hermes route-eval` joins rows with labels (below) and prints the confusion matrix and the promotion-gate verdict. Accept: reports NOT_QUALIFIED with fewer than N cases.
- **P4 advise mode.** Only after the gate passes and the owner approves it; behind `route_mode: advise`. Accept: the note appears once on the first turn, never when the classifier failed, and the session model is unchanged.

## Tests
- `tests/test_meta_classifier_route.py` (real native loader, `HERMES_SOURCE=~/projects/hermes-agent`, schema-dispatching fake `urlopen`; `tests/conftest.py:27-30` blocks real connects): deterministic labels need zero calls; classifier label recorded; `mappedRole` matches the table; unknown label from the service becomes `error`, not a default; shadow mode never returns a `context`; `advise` returns exactly one note and no model change.
- Redaction: a staged secret in `user_message` never appears in the transmitted text or in the row.
- Concurrency: with 5 simultaneous first turns, at most 4 workers run and the fifth records `reason: overloaded`.

## Risks & open questions
- **Policy conflict.** README says no path chooses a model from classifier output. This plan stays inside that rule (labels and an advisory note). P4 would need the owner to reverse it, so it is deliberately left unplanned.
- **Vocabulary split.** `COMPLEXITY_SCHEMA` uses `moderate`/`deep` (`schemas.py:346-352`); this plan uses `standard`/`deep-reasoning`. Decision: keep both. The per-delegate head and the session head have different consumers. Revisit if replay shows they should merge.
- The token estimate is a character heuristic. It may misjudge non-English or code-heavy text; the threshold is operator-set and logged, so misses are visible.
- `actual.provider` may be unavailable on `pre_llm_call`; recorded as unknown, never guessed.
- The resolver seam is absent in the host checkout, so any plan that needs a model switch cannot rely on it today.
- A label of `trivial` tells you nothing about whether a cheap model *succeeded*; that needs outcome evidence we only observe as lifecycle status.

## Labels
| label | definition | positive | negative |
|---|---|---|---|
| `trivial` | One mechanical, well-specified change or lookup; answer known once the file is open. | "rename `foo` to `bar` in `utils.py`" | "why does the cache corrupt under load?" |
| `standard` | Several steps/files with a clear approach; some judgment, no open design question. | "add a `--json` flag to the export command and a test" | "rename a variable" (trivial); "design a sharding scheme" (deep) |
| `deep-reasoning` | Open-ended investigation, unknown root cause, cross-cutting design, or easy-to-miss correctness risk. | "find why checkpoints diverge across two runs" | "bump the version string" |
| `long-context` | The task depends on reading a very large body of text (history or attached material) beyond the token threshold. Deterministic. | a 150k-token transcript plus "summarise the decisions" | a short question with a one-line paste |
| `vision` | The task includes an image the model must read. Deterministic. | a screenshot attached with "what is wrong here?" | a text path that merely mentions `.png` |

## Input contract
- Hook: `pre_llm_call` (in `VALID_HOOKS`, `hermes_cli/plugins.py:133`). Fields used: `session_id`, `turn_id`, `user_message`, `conversation_history` (length and image-part scan only; text is not sent), `is_first_turn`, `model`.
- Text sent to the service: `message_text(user_message)` plus nothing else, windowed to `addon_chars` (default 12000, `addons.py:56`). Messages under 8 stripped characters are skipped (`addons.py:174`).
- Redaction: `egress()` (native `redact_for_egress`) before windowing; a `ValueError` from it means no call and an error row (`addons.py:65-69`). Image bytes are never sent; only their presence is recorded.

## Decision wiring
- Consumer: none in shadow. The verdict maps through `route_label_roles` to a `model-routing` role name (`model-routing/__init__.py:36,173-200`); prewalk's target (`prewalk/plugin.yaml:20-27`) is not touched.
- Shadow: record `{label, mappedRole, actual}` only; nothing is returned from the hook.
- Promoted: `advise` mode adds one advisory `context` note on the first turn (`turn_context.py:663-717` channel). It never switches the model, writes pins or arms prewalk. Any stronger effect is P4 and needs an owner decision.

## Data & evaluation
- Sources: (1) replay of real sessions through `meta-classifier-replay --cases` with the router fixtures as a smoke set (`replay.py:56-59`); (2) human labels via plan 01's `/classifier-label` (`kind: route`, `given`, `label: correct|wrong`) on the shown row; (3) synthetic: 20 hand-written prompts per classifier label. Implied labels (the operator later overrode the model) are reported separately and never feed the gate.
- Metric: per-label precision and recall, plus the cost-weighted mistake rate (a `trivial` call on work that needed `deep-reasoning`).
- Gate to leave shadow: `trivial` precision ≥ 0.90 at recall ≥ 0.50 and `deep-reasoning` recall ≥ 0.70 on at least 150 human-labelled turns across at least 20 sessions, each label having at least 20 cases. Deterministic labels are tested separately and need no gate.
- Latency: p95 ≤ 2.5 s for the classifier call; shadow runs off-thread so it adds 0 ms to the turn; `advise` must fit its timeout or skip.

## Failure policy
- Timeout, transport error, 429/503 after the one bounded retry, unusable reply or unknown label: write an `error` row with no label. Never default to `standard`, never carry the previous label forward.
- Low confidence: the service returns labels without probabilities (`01-meta-classifier.md` risks), so there is none to threshold. `advise` stays off until replay calibrates it.
- Deterministic labels never fail: if the image scan raises, record `vision: unknown`, not `false`.
- A dead service never delays a turn (cooldown in the shared `client.py`).

## Non-goals
Choosing, switching or pinning a model from a label. Cost, quota or latency routing. Replacing `model-routing`'s explicit roles. Reimplementing the droid router's per-candidate scoring. Labelling subagent goals (that is the `complexity` addon and `subagent-effort`).
