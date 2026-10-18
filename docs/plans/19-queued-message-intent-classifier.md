# 19 queued-message-intent-classifier
Status: proposed · Owner: — · Depends on: 01 (shared classifier client, label store, replay), 08 (boundary inbox: consumes `steer` and `append-to-queue` verdicts); host seam H-route (a busy-route observer, not in `VALID_HOOKS` today); coordinates with the gateway busy policy (`busy_input_mode`)

## Goal
A user sends a message while the agent is already running a turn. Today Hermes routes it by configuration, not by content: the default is to interrupt the live turn, and the alternatives are steer (inject at the next tool boundary) or queue (run after the turn). This plan adds a label-head classifier that reads the queued message together with a snapshot of the running turn and says which of four things the user meant: `steer`, `interrupt`, `append-to-queue`, or `unrelated-new-task`. In shadow it only records the verdict next to the route the gateway actually took. Once promoted, it may choose between steer and queue for text messages when the user has not set an explicit mode. It never upgrades a message to an interrupt on its own word, and it never touches slash commands, stop/approve commands, media, or voice.

## Evidence
- **Hermes core, verified by read.** Busy-session dispatch enters `_hm_handle_running_session_message` (`gateway/run_inbound.py:611`), whose docstring states the default: "interrupt by default (minimal latency); busy_input_mode queue/steer, subagent and compression protection demote to queue" (`:614-615`).
- **Steer path, verified.** `_hm_busy_steer` (`gateway/run_inbound.py:566-579`) calls `running_agent.steer(...)` and, if that returns False or raises, falls back to `_queue_or_replace_pending_event`. The interrupt path `_hm_busy_interrupt` (`:581-609`) uses `running_agent.redirect()` when `_supports_active_turn_redirect` is true (`:588-597`), and otherwise `running_agent.interrupt(...)` (`:609`).
- **Steer semantics, verified.** `InterruptControlMixin.steer()` (`agent/interrupt_control.py:217-226`) queues text for delivery "as its own user row after the current tool batch finishes (no interrupt)", and multiple calls concatenate with a newline (`:225`). `_drain_pending_steer()` (`:299-304`) returns and clears the slot.
- **Steer application, verified.** `apply_pending_steer_to_tool_results` (`agent/agent_runtime_helpers.py:3167-3205`) appends the drained text as a new `role:"user"` row after the last tool result (`:3201`). If the batch has no tool row it requeues the text as a next-turn user message (`:3195-3199`). Plan 08 already generalises this seam; this plan does not add a second drain.
- **Hook surface, verified.** `pre_gateway_dispatch` is in `VALID_HOOKS` (`hermes_cli/plugins.py:152-155`). It fires once per incoming message, before auth and dispatch, and can return `skip` or `rewrite` only. It cannot choose a busy route. No hook in `VALID_HOOKS` (`:110-221`) reports the busy route that was actually taken, so shadow evaluation needs one (see Decision wiring).
- **Reference (droid, observed in its e2e note).** `droid/notes/e2e/mission-sendmessage.md:24-28` describes a running recipient queuing a message and draining it at a safe boundary as a separate user message; the `userMessageInjection.ts:173-300` citation is the note's own source pointer and was not re-read here. The note describes no intent classification, so this plan is the classifier layer that the documented mechanism lacks.
- **Reference (Grok Bot, decoded).** No intent classifier is documented `[inferred: none decoded]`. The Grok Build harness resumes via system-reminder on the next tool result (`re/docs/grok-build-harness.md:784`), which is the same boundary plan 08 targets.

## Current state
- Routing is config-driven. The busy-mode setting is resolved by `_effective_busy_input_mode` (`gateway/run_inbound.py:621`) and applies to every message, so changing one message's route means changing it for all of them.
- Steer is append-only and newline-joined (`agent/interrupt_control.py:225`). The text is wrapped with origin attribution by `_steer_text_with_origin` (`gateway/run_inbound.py:572`), but nothing marks its intent, and the model cannot tell where one steer ends and the next begins.
- A queued message waits in `_queue_or_replace_pending_event` until the turn ends, so a message that was meant as a correction arrives after the wrong work is finished.
- No labelled corpus of busy-session messages exists. The meta-classifier (`plugins/meta-classifier/client.py`, `MAX_TEXT = 100000` at `:17`) is the shared client to reuse; it is read-only for this plan.

## Labels
Label head `intent`, single label, one request per queued message.
- `steer`: the message refines or adds a constraint to the running task, and the agent should take it into account after the current tool batch. Example: "also keep the existing tests passing".
- `interrupt`: the message contradicts or stops the running task, and continuing the batch is wasted or harmful. Example: "stop, wrong file".
- `append-to-queue`: the message is the next step of the same task, to run after this turn finishes. Example: "then open a PR".
- `unrelated-new-task`: the message starts a different task. Example: "what's the weather in Lisbon".

Label definitions go in `schemas.py`, shaped like `LUNA_TIERS` (`plugins/meta-classifier/schemas.py:58-63`), a dict of `label -> description`, with the description text carrying the boundary rules above. Ambiguous cases are labelled by what the user needs next, not by tone. `interrupt` requires an explicit stop or contradiction; uncertainty never selects it.

## Input contract
The classifier sees, and only sees:
- the queued message text, truncated to 2000 characters;
- the running turn's last assistant text, truncated to 1000 characters;
- the names of the tools in the current batch and their count. Arguments are excluded because they can hold secrets and paths;
- turn age in seconds and the number of tool batches completed so far;
- the current busy mode (`interrupt`, `steer`, `queue`) as configured.

It never sees tool results, session history beyond the last assistant text, user identity, platform, or chat id. Egress of the message text to the classifier model follows the policy set in plan 01; this plan does not widen it. Slash commands, `/stop`, `/approve`, media-only messages, and voice messages are excluded before the classifier runs and never reach it.

## Decision wiring
Three seams are involved. Only the first is verified.
1. **Apply, steer and queue only (verified seams).** When the mode is `auto` (config `busy_intent.mode`; default `shadow`, see Rollout) and the verdict is `steer` or `append-to-queue`, the handler calls the existing `_hm_busy_steer` path for `steer` and the existing `_queue_or_replace_pending_event` for `append-to-queue`. `unrelated-new-task` also queues. The apply path changes nothing in `interrupt_control.py` or `agent_runtime_helpers.py`.
2. **Interrupt is never classifier-chosen.** The classifier may downgrade the user's default interrupt to steer or queue. It may not create an interrupt where the configured mode is steer or queue. A verdict of `interrupt` under `auto` keeps the configured default route. This asymmetry is deliberate: the expensive error is an agent that keeps working on the wrong thing, and an interrupt the user did not ask for is cheap to recover from only if the user notices.
3. **Shadow observation (new seam H-route).** Shadow needs the route the gateway actually took, which no current hook reports. Host seam H-route adds a bounded observer hook named `busy_message_routed` with kwargs `session_key`, `message_id`, `route` (`interrupt|redirect|steer|queue`), `reason`. It must be added to `VALID_HOOKS` with a real fire site in `_hm_handle_running_session_message` (`gateway/run_inbound.py:611`), not registered ahead of the fire site. Until H-route lands, shadow runs but logs `route: unknown` and the label is unevaluable against routing; the plugin records verdicts only.

The plugin registers `pre_gateway_dispatch` only to see each incoming message in shadow and start the classifier call on a bounded worker. It returns `None` (allow) in every case; it never returns `skip` or `rewrite`. Verdicts are keyed by the message id so the H-route event can be joined to them. The plugin is `plugins/busy-intent/` and owns no state outside its JSONL shadow log.

## Data & evaluation
- **Corpus.** Mine queued messages from busy-session transcripts that already exist in the session store. Each example is the input contract above plus a human label. No live customer text leaves the machine for labelling; labelling uses the replay tool `tooling/classifier-replay.mjs` pattern from plan 01.
- **Size.** At least 300 labelled busy-session messages, with at least 30 per label. `unrelated-new-task` and `append-to-queue` are the likely rare labels; if either is short, the promotion gate is not met.
- **Shadow metrics.** Per-label precision and recall; confusion matrix; the rate of `route: unknown` (must be under 5% once H-route lands, otherwise the H-route fire site is wrong).
- **Promotion gate, for apply.** (a) On the `steer` and `append-to-queue` verdicts, precision ≥ 0.90 and recall ≥ 0.50 against labels. (b) The false-downgrade rate, meaning a true `interrupt` labelled as `steer` or `append-to-queue`, is ≤ 2% of true interrupts. (c) Classifier p95 latency ≤ 300 ms on the local path; if the classifier misses that budget the handler uses the configured default route.
- **Replay.** Replay the frozen label set through the classifier after every prompt or schema change. A change that moves any label's precision by more than 5 points is a new candidate, not a silent update.

## Failure policy
- Classifier timeout, error, malformed output, or missing key: use the configured default route. The message is never dropped.
- Classifier overflow (the bounded worker is full): use the configured default route and record `overflow`.
- A verdict that is not one of the four labels: treat as failure, not as the nearest label.
- Under no failure does the plugin route to `interrupt` on its own. A failure path cannot cause an interrupt that the configured mode did not choose.
- Queued text is never rewritten or truncated by this plugin; truncation in the input contract applies only to the classifier prompt.
- Redaction: the shadow log stores verdict, label, route, and lengths; it never stores message text. Raw text exists only in the replay corpus, under the plan 01 retention rule.

## Rollout
1. **P0, shadow, no H-route.** Plugin registers `pre_gateway_dispatch` (observer, returns allow), logs verdicts with `route: unknown`. Acceptance: the plugin never changes a routed message (diff of `_hm_handle_running_session_message` outcomes is empty in a replay run).
2. **P1, H-route lands.** Add `busy_message_routed` to `VALID_HOOKS` with its fire site, then compute the routing confusion matrix on live shadow data.
3. **P2, apply steer and queue.** Only after the promotion gate passes on P1 data. Config `busy_intent.mode: shadow|auto`, default `shadow`.
4. Each phase ships behind its own config key and can be reverted by flipping the key to `shadow` or `off`.

## Tests
`tests/test_busy_intent.py`, real native loader with a fake classifier client (pattern in `tests/test_meta_classifier*.py`; `tests/conftest.py:20-38` blocks real sockets):
- each label is recorded in shadow with `route: unknown`, and the message text is absent from the log;
- timeout, missing key, overflow, and an out-of-set label each yield the configured default route and no interrupt;
- an `interrupt` verdict under `auto` never changes a configured `steer` or `queue` route;
- slash and approval commands never reach the classifier (the fake client fails the test if called);
- `pre_gateway_dispatch` returns allow in every case, never `skip` or `rewrite`.

## Risks
- **D1, no classifier-chosen interrupt.** Decided. Upgrading to interrupt on a classifier verdict is the riskiest route change and gains least over the user's own choice. Only downgrades are applied.
- **D2, new hook for shadow.** Decided to add `busy_message_routed` (host seam H-route) rather than infer the route from logs. An inferred route is a second, unverified source of truth.
- **D3, steer text is newline-joined.** Kept as-is. `steer()` joins with `"\n"` (`agent/interrupt_control.py:225`). The classifier cannot distinguish two steers after they are joined. Changing the join or adding per-item framing belongs to plan 08's inbox, not to this plan.
- **D4, egress of queued text.** Governed by plan 01's egress policy. This plan adds no new egress.
- **D5, precision on `steer`.** Open. A `steer` verdict that is really `interrupt` means the agent keeps working for one more tool batch. The promotion gate's 2% false-downgrade cap is the chosen bound; a stricter bound needs labelled data this plan does not yet have.
- **D6, slash and approval commands.** Decided: excluded before the classifier. `/stop` and `/approve` must never depend on a model verdict.

## Non-goals
- Classifier-chosen interrupts, or any route change during P0 and P1.
- Changing steer framing or the newline join; that belongs to plan 08.
- Classifying slash, approval, or stop commands.
- Defining an egress policy; plan 01 owns it.
- Changing `_hm_busy_interrupt` or the redirect path.
