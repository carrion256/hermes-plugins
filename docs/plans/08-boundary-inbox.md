# 08 boundary-inbox
Status: proposed · Owner: — · Depends on: host seam H1 (generalise the existing `/steer` drain into a typed boundary inbox), H2 (`delegate_task` background completion routed through that inbox); coordinates with `04-tool-steering` (failure envelopes) and `11-machine-relay` (cross-machine messages); builds on `agent/agent_runtime_helpers.py::apply_pending_steer_to_tool_results`

## Goal
Anything that reaches an agent while it is working is delivered at the next safe boundary, in the agent's own turn, with explicit provenance. Three inputs need this: a user message sent while a turn is running, an inter-agent message from an orchestrator, and the completion of a background subagent. Today Hermes delivers the first one mid-turn and the other two only as a new turn. The plan closes that gap without a second queue, a second drain, or a parallel results channel.

The inbox sits at the tool-result boundary: after a tool batch finishes and before the next model call. Each item is framed by kind (`[From orchestrator]`, `[Subagent <id> finished]`, user steer) so the model can tell an instruction from a tool result. Background completions are summarised and capped inline, with a poll pointer for the rest.

## Evidence
- **Hermes already delivers `/steer` mid-turn.** `agent/agent_runtime_helpers.py:3167-3205` (`apply_pending_steer_to_tool_results`) drains `agent._drain_pending_steer()` at the end of a tool batch and appends the text as a new `role:"user"` row after the last tool result. If there is no tool row in the batch, it re-queues to the next-turn fallback (`:3195-3199`). This is the seam to generalise, not replace.
- **Background subagents surface only as a new turn.** `tools/async_delegation.py:1-8` says completion is pushed onto the shared `process_registry.completion_queue`, drained by CLI/gateway "while idle, so results surface as a NEW turn (never mid-turn)". Delivery is acknowledged by `mark_completion_delivered` (`:312-316`). This is the gap for background work.
- **Hermes has no subagent `resume_from`.** A grep for `resume_from` in `tools/delegate_tool.py` returned no matches (checked during planning; not re-read after).
- **Reference behaviour (Grok Build, source).** `re/docs/grok-build-harness.md:784`: resume requires a completed source (in-memory ≤1024 or durable `subagents/<id>/meta.json`); an active source is refused; the child inherits the raw transcript, cwd/worktree and model; lifecycle `pending → active → completed` plus `queued`; spawn is rejected when the parent session is stopped. Completion is delivered "as system-reminder on the next tool result and, if the parent is idle, a synthetic wake turn (suppressed during goal loops)" (same line). The result envelope is the last assistant message plus `<subagent_meta>` and `<subagent_result>`, with a **16 000 B inline cap and poll pointer** (same line). Model selection on resume is pinned to the source (`:786`).
- **Reference tool surface.** `re/docs/grok-build-harness.md:832` lists the spawn options (`run_in_background` default true, `isolation_worktree`, `resume_from`, `phase`, and others). `lineage-backfill.md:237-238` lists `get_task_output`/`wait_tasks` (≤20 ids, 1 h cap), `kill_task`, `send_subagent_message`.
- **Unverified for Hermes.** The 16 000 B figure is documented only on the Grok Build side. No Hermes source sets a tool-result cap of that size (checked in the planning pass; a grep of droid notes for `16384` found only LLM request `max_output_tokens`, not a tool-result cap). Treat it as a candidate value to measure in P1, not an established constant.
- **Adjacent plugin, different surface.** `plugins/keyword-steering/__init__.py:1-26` handles turn-local *mode* activation from keywords (`ultrathink`, `orchestrate`, `workflow`) and is not a message queue. Its `plugin.yaml` (`provides_hooks`: `pre_llm_call`, `on_session_reset`, `on_session_finalize`, `agent_loop_stopped`) states "no messages or tool args are ever rewritten". It is out of scope for delivery.
- **Failure-result envelope is owned elsewhere.** `docs/plans/04-tool-steering.md` (Design, lines 22-43) owns the four-part refusal text and `transform_tool_result` ordering. This plan must not add a second failure envelope.
- **Peer evidence, read.** Droid Mission V2 (`droid/notes/e2e/mission-sendmessage.md:24-28`): a running recipient queues the message, and `injectQueuedUserMessages` (`apps/cli/src/core/userMessageInjection.ts:173-300`) drains it at a safe boundary as a separate user message with a `[From orchestrator]` / `[From agent "<title>"]` header. Grok Bot (`grokbot-re/re/docs/backend-api.md:207`): `SendGrokBotAgentMessage` has an explicit delivery taxonomy (`DELIVERED_TEMPORAL`, `DELIVERED_BOX`, `DUPLICATE`, `TARGET_NOT_FOUND`, `FORBIDDEN`, `BOX_UNREACHABLE`, `TEMPORAL_UNAVAILABLE`, `INVALID_TARGET`). Hermes has no equivalent result type for `delegate_task` child messages.

## Current state
- Mid-turn: one kind of input (user `/steer`) reaches the tool boundary. Drain function and marker live in `agent/agent_runtime_helpers.py`; the drain is reached through `run_agent.py:836`.
- Background subagents: completion is durable (`tools/async_delegation.py` SQLite delivery state, `:312-316`) and surfaces only on the completion queue, drained while idle. A running parent never sees it until it goes idle and a new turn begins.
- Inter-agent messages: no framing kind exists. Messages from an orchestrator to a child (and child to parent) have no provenance prefix in the boundary path.
- Subagent resume: not present (`tools/delegate_tool.py` has no `resume_from`).
- Inline cap: none verified for subagent results in the boundary path.

## Design
Generalise, do not fork. One **boundary inbox** holds typed items; the existing drain becomes its first consumer.

**Item kinds** (one `kind` string each, framing rendered by the host, not by the sender):
- `user_steer`: the current `/steer` text, marker text unchanged.
- `inter_agent`: a message from another agent, rendered `[From <sender>] <text>`. `<sender>` is an agent id or `orchestrator`; the sender is set by the host, never by the message body.
- `subagent_complete`: a background delegation finished. Rendered as a system-reminder naming the id, status and a summary. Body is the last assistant message, clipped to the inline cap, with a `get_task_output <id>` pointer when clipped.

**Drain point.** Unchanged: end of a tool batch, before the next API call (`apply_pending_steer_to_tool_results`). Items are appended as separate `role:"user"` rows after the last tool result, which keeps alternation legal (the existing pattern, `:3172-3185`).

**Idle path.** If no tool batch is running when a `subagent_complete` item is ready, the existing completion-queue drain starts a new turn (current behaviour, `async_delegation.py:1-8`). Grok Build's "synthetic wake turn" is the same behaviour; it is kept, not added.

**Inline cap.** A subagent body over `inbox.inline_cap_bytes` (default 16 000, candidate value, see Evidence) is clipped at a UTF-8 boundary with `…[truncated, <n> bytes; get_task_output <id>]`. The full result stays in the durable store. Cap is applied in the inbox, not in the subagent.

**Resume.** `delegate_task(resume_from=<id>)` starts a child with the source's raw transcript, cwd/worktree and model. Rejected if the source is active or not completed; rejected if the parent is stopped. Lives in the host (`delegate_tool.py`); the plugin does not implement it.

**Plugin role.** `boundary-inbox` plugin registers no tools in P0. It consumes the host inbox for `inter_agent` and `subagent_complete` framing through a `transform_tool_result`-free path: items are rendered by host code, so the plugin provides only the `inbox_status` read tool and config (`inline_cap_bytes`, `enabled`).

**Why not `transform_tool_result`.** That hook is first-string-wins and rewrites results (see `04-tool-steering`, Current state). Appending inbox items there would collide with steering envelopes and would fire on blocked results, which skip it (`model_tools.py:926-929`). The boundary drain is the correct seam.

## Host changes
- **H1 (boundary inbox).** Replace `_drain_pending_steer` with a typed `drain_boundary_inbox(agent) -> list[InboxItem]` in `agent/agent_runtime_helpers.py`. `apply_pending_steer_to_tool_results` keeps its name as a thin caller during P0 and is deleted in P1 (no shim left after cutover). Re-queue behaviour for "no tool row in batch" (`:3195-3199`) applies to every kind.
- **H2 (background completion routing).** `tools/async_delegation.py` pushes `subagent_complete` into the inbox when a parent turn is running, instead of only the completion queue. Idle behaviour unchanged. `mark_completion_delivered` (`:312`) is called only after the row is appended to the transcript, so a crash between drain and append re-delivers rather than drops.
- **H3 (resume).** `tools/delegate_tool.py` accepts `resume_from`, validates source state, and pins model and worktree. Refusal messages name the reason (`active`, `not_completed`, `parent_stopped`).
- **H4 (sender identity).** `inter_agent` items carry a host-set `sender` field. Message bodies cannot set it. This is the provenance guarantee the framing relies on.

## Phases
- **P0 — typed inbox, behaviour unchanged.** Introduce `InboxItem` with the existing `user_steer` kind only; route the current drain through it. Acceptance: existing `/steer` tests pass byte-identical; a `/steer` sent during a batch with no tool row is re-queued and delivered as a next-turn user message.
- **P1 — background completion mid-turn.** Add `subagent_complete` (H2). Acceptance: a background child that completes during a parent tool batch appears as one `[Subagent <id> finished]` row after that batch's last tool result; the parent does not start a new turn for it; `mark_completion_delivered` runs once.
- **P2 — inline cap and poll pointer.** Enforce `inline_cap_bytes`; add `get_task_output` pointer. Acceptance: a 40 KB child result is clipped at a UTF-8 boundary, the pointer names the id, and the full text is retrievable from the store unchanged.
- **P3 — inter-agent framing and sender identity.** Add `inter_agent` (H4). Acceptance: a message whose body contains `[From orchestrator]` but whose host sender is `child-7` renders `[From child-7]`.
- **P4 — `resume_from` (H3).** Acceptance: resuming a completed child keeps its model and transcript prefix; resuming an active child is refused with `active`; resuming after parent stop is refused with `parent_stopped`.

## Tests
`tests/test_boundary_inbox.py` (real drain path, no model; loopback model only where a batch must run):
- `/steer` with a tool batch → one `user` row after the last tool row (existing assertion kept).
- `/steer` with no tool row → re-queued, delivered next turn.
- background completion during a batch → one `subagent_complete` row, `mark_completion_delivered` called once; idle completion still starts a new turn.
- body over cap → clipped at UTF-8 boundary, pointer present, store copy intact.
- body containing a fake `[From orchestrator]` header with host sender `child-7` → rendered `[From child-7]`.
- resume: active source refused, completed source resumed with same model id, stopped parent refused.

## Risks & open questions
- **16 000 B cap is unverified for Hermes.** It is documented in Grok Build (`grok-build-harness.md:784`); no Hermes source sets it. P2 measures typical child results before the default is accepted. If the number is wrong, change the config default, not the design.
- **Mid-turn delivery changes the prompt-cache prefix.** Appended rows after a tool batch are new bytes for the next request. Same risk the existing `/steer` path already accepts (`agent_runtime_helpers.py:3180-3185`); P1 must confirm no extra cache miss beyond one turn.
- **Parent may be inside a long tool call.** A completion that lands while a 10-minute `terminal` call runs is delivered only after the batch ends. Accepted; the idle wake covers the case where no batch runs. Not verified that Hermes exposes a batch-running flag to the drain; P0 must check.
- **Idle wake vs goal loops.** Grok Build suppresses the synthetic wake during goal loops (`grok-build-harness.md:784`). Hermes has no verified goal-loop equivalent; do not add the suppression until one is identified.
- **`resume_from` ownership.** Assumed to belong in host `delegate_tool.py`. Not verified: the file was not re-read, and `resume_from` was not found by grep, so the current spawn signature must be read before P4.
- **Sender spoofing.** The body-level `[From orchestrator]` string is ordinary text; only the host `sender` field is authoritative. If any path lets a child set `sender`, the provenance claim fails. H4 must be reviewed for that.
- **Truncation must keep the poll pointer.** The 16 000 B inline cap cuts child output; if the cut drops the poll pointer, the parent cannot recover the rest and may re-poll indefinitely. P2 asserts the pointer survives truncation at every size.
- **Multi-item ordering at one boundary.** A user steer, an inter-agent message, and a subagent completion can queue together. Order must be deterministic. Plan: arrival order, with the `/steer` marker unchanged. P1 tests this directly.

## Non-goals
Rewriting failure or refusal text (owned by `04-tool-steering`); a second message queue or results channel; cross-machine transport (owned by `11-machine-relay`); new steering keywords or modes (owned by `keyword-steering`); changing the `/steer` marker text; subagent concurrency limits (Grok Build's 32 per run is documented at `grok-build-harness.md:832` and is not part of this plan).
