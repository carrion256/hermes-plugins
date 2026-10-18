# 13 conversation-plane (host seams + `conversation-plane` plugin)
Status: proposed · Owner: — · Depends on: host seams H1-H4 below; complements plan 14 (box state frames) and plan 15 (voice `async_update` consumes live state)

## Goal
A client (desktop, mobile, a relay, another agent) can drive a Hermes conversation the way the Grok Bot client drives its server:
- send a turn with a client-minted `message_id` and learn durably whether it was ACCEPTED, a DUPLICATE or REFUSED, even after a network failure;
- tail the transcript with a resumable `(generation, updated_seq)` cursor;
- receive typed live state ("running, calling `terminal` on `build.sh`, awaiting approval, stale after 20 s") instead of inferring it from token deltas;
- render tool calls as lean `{name,status,summary}` rows.

Almost all of this is host work. The plugin supplies only what a plugin can own: the lean projection and a read-only status tool. Hermes plugins cannot register HTTP routes or RPC methods (see Current state), so the plugin must not pretend to be the transport.

## Evidence
- **Send/probe** [decoded] `agent-protocol.md` §2.1 rows `SendGrokBotUserMessage` (`message_id` = client nonce, idempotency key) and `GetGrokBotSendStatus` (`NOT_FOUND/ACCEPTED/REJECTED/PENDING/UNKNOWN_DURABILITY`, `echo_entry_id?`, `rejection_code?`). Delivery enum `ACCEPTED_BOX|ACCEPTED_TEMPORAL|DUPLICATE|REFUSED`; turn state machine §2.4 (network error → poll status; `DUPLICATE` = safe resend). Error mapping §2.1 line 74 (`InvalidArgument` + refusal → `refused`).
- **Watch stream** [decoded] `agent-protocol.md` §2.2 frames `connected{stream_id,server_time_ms,absolute_lifetime_ms}`, `rows{agent_id,generation,entries[],deletes[],replay,session_id}`, `cleared{new_generation}`, `cursor_too_old`, `heartbeat`, `agent_state{live[],snapshot,client[]}`, `turn_failed{code,summary,…}`.
- **Cursor shape** [decoded] per-(agent, session) cursor `{generation, after_updated_seq}`. Rows are ordered by `updated_seq`, a second cursor for in-place edits; `seq` is append order (`backend-api.md` §4, line 209). `cursor_too_old` means re-list. The client must reconnect before `absolute_lifetime_ms`.
- **Failure codes** [decoded] `turn_failed.code` ∈ INTERNAL / TIMEOUT / USAGE_LIMIT / RATE_LIMIT / … (`agent-protocol.md` §2.2, line 84).
- **Live state** [decoded] `agent-protocol.md` §2.2: `GrokBotAgentLiveState{is_running, is_composing_message, is_retrying, activity{kind,tool,detail,target,call_id}, awaiting{tab_id,reason,since_ms}, updated_at_ms, stale_after_ms, has_running_subagents, …}`.
- **Lean tool entry** [decoded] `sand-gateway-api.md:4089-4098`: `SandToolCallEntry{id, kind:"tool-call", name, status:"done"|"failed"|"pending", summary?, seq?, timestampMs?}`. Full args/result are not in the entry (§2.3 says the full tool-call entry shape is unknown; the server renders tools).
- **Session keying** [decoded] sessions are keyed `(agent_id, session_id)`. Whether Slack and Teams threads map to sessions of the same agent is [INFERENCE: `backend-api.md:211` was not fully read; the range was truncated].
- **Backend API cursor** [decoded] `backend-api.md:205-215` (§4.7 `conversations_transcripts`): `ListGrokBotTranscriptEntries`, `SendGrokBotUserMessage`, `InterruptGrokBotAgentRun`.

## Current state
- **Idempotent run submit exists.** `gateway/platforms/api_server_run_idempotency.py:53-155`: durable SQLite table `run_idempotency(scope, idempotency_key, fingerprint, run_id, status_json, …)`. `reserve()` returns `created|reused|conflict` atomically (`BEGIN IMMEDIATE`). `lookup()` returns `missing|reused|conflict` without reserving [INFERENCE: `lookup` at `:157` was not re-read this session]. Retention is 24 h (`:58-59`). `durable` (`:61-64`) is false when the store fell back to `:memory:` (`:65-82`, warning at `:80-82`). Advertised as capability `runs_idempotency` (`api_server.py:2276`, `run_submission: True` at `:2275`).
- **Run routes exist.** `api_server.py:80-89`: `POST /v1/runs` (81), `GET /v1/runs/{run_id}` (82), `/events` (83), `/approval` (84), `/steer` (85), `/stop` (86). `Idempotency-Key` and `X-Hermes-Session-Id` are CORS-allowed (`api_server.py:776-782`).
- **Gap in idempotency.** The store is HTTP-run-scoped, keyed by a header, not by a transcript-visible `message_id`. It speaks `created/reused/conflict` rather than ACCEPTED/DUPLICATE/REFUSED. There is no status probe by key, only by `run_id`.
- **No resumable SSE anywhere.** A grep for `Last-Event-ID` over `gateway/platforms` returned no matches. `/v1/runs/{id}/events` has no resume.
- **Event sequencing exists but is volatile.** `tui_gateway/event_replay.py:1-34`: per-session monotonic `seq` stamped on every event frame, a bounded ring (512 events, 4 MiB per session; 64 sessions; 64 MiB per process at `:28-34`), and `_REPLAY_EPOCH` (`:24`) so clients detect a process restart. `session.events.since` (`tui_gateway/methods_session.py:2170-2182`) takes `session_id` and `last_seen` and returns `events`, `latest_seq`, `truncated`, `count`, `epoch`. `session.events.stats` is at `:2185-2189`.
- **Gap in the ring.** It is not durable (resets on restart). It is per live event, not per transcript row. It has no in-place-edit cursor, no `deletes`, and no `cleared`.
- **Durable rows have no edit cursor.** The `messages` table (`hermes_state_common.py:391-418`) has an `id` autoincrement (usable as append `seq`), `timestamp`, `active` (`:411`), `compacted` (`:412`), `display_kind` / `display_metadata` (`:414-415`), and `display_order` (`:417`). These columns are updated in place, but there is no `updated_seq` and no generation.
- **Migration idiom.** `hermes_cli/sqlite_util.py:70-79` defines `transaction(conn, *, immediate=False)` (commit, rollback, always close). `add_column_if_missing` (`:82-95`) returns False on `duplicate column name` (#21708). This is the house migration path.
- **Live typed state is absent.** No typed live-state projection (`is_running`, `stale_after_ms`) was found in the `tui_gateway/` files read this session [INFERENCE: grep was not re-run after reading]. Activity today is untyped frames: `tool.start`, `tool.complete`, `approval.request`, `message.delta` (`tui_gateway/hosted_room_member_activity.py:22-31`).
- **Room activity hook exists, hosted-only.** `on_room_member_activity` (`hermes_cli/plugins.py:165-170`, hook name at `:170`) is an observer, queued off the token path (`agent.plugin_stream_hooks`), never written to the durable room log. Kwargs are the coordinates `room_id, thread_id, member_id, turn_id, task_id, execution_generation` plus `kind`, `seq`, `payload`. It fires for hosted Group Chat member sessions only.
- **Approval hooks exist.** `pre_approval_request` and `post_approval_response` (`plugins.py:164`) are registered hook names. Their kwargs were not re-read this session [INFERENCE: expected to carry the command, description, and session key; confirm before P3].
- **Tool hooks are settled exactly once.** `post_tool_directive` (`plugins.py:125`) fires once per dispatched call; `post_tool_call_settled` (`plugins.py:130`) fires once after every `post_tool_call`.
- **Plugins cannot own routes or RPC.** `class PluginContext` starts at `plugins.py:248`. Its `register_*` methods are `register_approval_transport` (`:457`), `register_tool` (`:483`), `register_cli_command` (`:684`), `register_command` (`:698`), `register_context_engine` (`:737`), `register_context_reference` (`:758`), and `register_memory_provider` (`:774`). None registers an HTTP route or a gateway RPC.
- **Redaction exists.** `agent/redact.py:807` `redact_for_egress(text) -> str` is the single scrub for text leaving the process (`redact_sensitive_text(force=True)` plus a bearer sweep).

## Design
**Posture.** Three rules apply to every phase:
- *Shadow-first.* P0 ships with no host change; the plugin only observes and projects.
- *Explicit consent.* The cursor and watch surfaces are off by default (`transcript.cursor: false`) until P2 passes; live-state emission is opt-in per profile.
- *Native stores authoritative.* `state.db` is the source of truth for rows; the live ring and the plugin projection are derived views and must never be cited as the record.

**Split.** Protocol surface = host (H1-H4). Plugin `conversation-plane` = lean projection plus a read-only diagnostic tool. The plugin owns no routes, no tables, and no transport.

**H1 send ledger (`message_id` → verdict).** Generalise `RunIdempotencyStore` with a `message_id` namespace, scoped `(profile, session_id)`. Reuse the table with a `kind` column added via `add_column_if_missing`.
- New `POST /v1/sessions/{sid}/messages` with body `{message_id, text, attachments?}` returns `{delivery: ACCEPTED|DUPLICATE|REFUSED, run_id, echo_entry_id?, refusal?: {code, summary}}`.
- Mapping: `created` → ACCEPTED; `reused` with the same fingerprint → DUPLICATE, returning the *original* `run_id` and never starting a second turn; `conflict` (same `message_id`, different body) → REFUSED `message_id_conflict`.
- New `GET /v1/sessions/{sid}/messages/{message_id}/status` returns `status ∈ NOT_FOUND|ACCEPTED|REJECTED|PENDING|UNKNOWN_DURABILITY` from `lookup()`.
- `UNKNOWN_DURABILITY` is returned iff `store.durable` is false. This is the honest answer after a restart on the in-memory fallback; NOT_FOUND would be a lie there.

**H2 durable transcript cursor.** Sidecar table `transcript_cursor(session_id, generation INTEGER, next_seq INTEGER)`, plus `messages.updated_seq INTEGER`.
- SQLite `AFTER INSERT` and `AFTER UPDATE` triggers assign `updated_seq` from `transcript_cursor.next_seq`. Every existing write site (compaction setting `compacted=1`, `active=0`, display column updates) is covered without auditing callers.
- Deletes write a tombstone to `transcript_deletes(session_id, generation, entry_id, updated_seq)`.
- `generation` bumps only on rollback, undo, or rewrite. A new session id already means a new conversation in Hermes, so there is no StartFresh analogue; clients key cursors by session id.
- Row `body` is omitted (`body_omitted: true`) above `inline_body_max_bytes`. The client fetches the body through the existing `GET /api/sessions/{id}`.

**H3 watch stream.** `GET /v1/transcripts/watch?cursor=<sid>:<gen>:<seq>[,...]` (SSE). Frame names match Grok Bot exactly:
- `connected{stream_id, server_time_ms, absolute_lifetime_ms}`
- `rows{session_id, generation, entries[], deletes[], replay}`, ordered by `updated_seq`
- `cleared{session_id, new_generation}`
- `cursor_too_old{session_id, generation}`, when the cursor predates retained tombstones or `transcript_gc_horizon` (proposed name; does not exist in the codebase today)
- `heartbeat{server_time_ms}` every 15 s
- `agent_state{live[]}`
- `turn_failed{session_id, turn_id, code, summary, failed_at_ms}`

`code` set: INTERNAL, TIMEOUT, USAGE_LIMIT, RATE_LIMIT, plus PROVIDER_CONTENT_BLOCKED if the Hermes error classifier produces that reason [INFERENCE: classifier module not located this session]. The stream ends at `absolute_lifetime_ms` (proposed default 30 min); clients reconnect with their cursor. In-flight rows come from the live ring (`event_replay`); durable rows come from `state.db`. The cursor is always the durable one, so a restart does not strand clients. This replaces the `epoch` trick for this surface only.

**H4 typed live state.** An `agent_live_state(session_id)` projection, fed from the same events the replay ring already sees:
- `{is_running, is_retrying, activity: {kind: model|tool|approval|compaction|idle, tool?, detail?, target?, call_id?}, awaiting: {reason, since_ms} | null, updated_at_ms, stale_after_ms, has_running_subagents}`
- `target` is a path, URL, or host only, redacted via `agent/redact.py` `redact_for_egress`. `detail` is at most 120 chars. Raw args are never emitted.
- `stale_after_ms` = 3 × heartbeat while running. A consumer that sees `now - updated_at_ms > stale_after_ms` must treat the state as unknown, not as "still running". No fabricated liveness.
- `awaiting` is set by `pre_approval_request` (and clarify) and cleared by `post_approval_response`.
- Read RPC `session.live_state` in `tui_gateway/`, plus an observer hook `on_agent_live_state(session_id, live)` (new name, observer, queued off the token path like `on_room_member_activity`), so plugins need not poll.

**Plugin `conversation-plane`.**
- (a) Observer on `post_tool_call_settled` deriving the lean entry `{id: tool_call_id, name, status: pending|done|failed, summary}`. `summary` comes from a deterministic per-tool template (file path for `read_file` and `patch`, first command token plus exit code for `terminal`, URL host for fetch), at most 120 chars, secrets stripped via `redact_for_egress`. It is stored as a `display_kind='tool-call'` row (`messages.display_kind`, `hermes_state_common.py:414`) only when H2 is present. Without H2 it stays in an in-process ring.
- (b) Read-only tool `conversation_state {session_id?}` returns the live-state projection and last cursor for the agent itself and for plan 15's relay.
- (c) For hosted rooms, `on_room_member_activity` is mapped into the same lean entries, so Group Chat members appear in the plane with no host work beyond H2.

Config (`plugins.entries.conversation-plane.settings`): `summary_max_chars: 120`, `include_target: true`, `redact_targets: true`, `heartbeat_ms: 15000`.

**Failure modes.**
- A fingerprint conflict is REFUSED, never silently deduped.
- A probe on a non-durable store reports UNKNOWN_DURABILITY.
- A cursor past the GC horizon yields `cursor_too_old`; the client re-lists, so there are no holes.
- A stream past its lifetime closes with a final `heartbeat`; the client resumes.
- A plugin summary template error yields `summary: null`, never an exception on the tool path.

## Host changes
- **H1** `RunIdempotencyStore` message namespace, plus `POST /v1/sessions/{sid}/messages` and the status route. Touch points: `gateway/platforms/api_server.py` route table (`:80-89`) and store `gateway/platforms/api_server_run_idempotency.py`. No hook suffices: the store is host-internal and routes are host-owned.
- **H2** `hermes_state_common.py` DDL: `transcript_cursor`, `transcript_deletes`, `messages.updated_seq` plus triggers. Migration via `add_column_if_missing` (`hermes_cli/sqlite_util.py:82`). No hook suffices: write sites are in core.
- **H3** SSE watch route plus `transcript_gc_horizon`. Reuses the `tui_gateway/event_replay.py` ring for in-flight rows.
- **H4** `live_state` projection in `tui_gateway/`, a read RPC `session.live_state`, and the observer hook `on_agent_live_state` registered in `VALID_HOOKS` (`hermes_cli/plugins.py:110`).

## Phases
- **P0 shadow** (plugin only): lean entries and `conversation_state` from existing hooks; no host change. Check: a scripted turn with 3 tool calls yields exactly 3 entries; `status` transitions pending → done; a failing `terminal` yields `failed`; no entry `summary` contains a seeded fake `sk-` secret.
- **P1 H1**: `message_id` send and probe. Check: same body twice yields one `run_id`, and the second reply is DUPLICATE; changed body with the same id yields REFUSED `message_id_conflict`; kill and restart the gateway between send and probe, then status is ACCEPTED or terminal when durable and UNKNOWN_DURABILITY when `store.durable` is false.
- **P2 H2 + H3**: durable cursor and watch. Check: compact a session mid-stream and see the touched rows re-emitted with higher `updated_seq`; reconnect with the last cursor after a gateway restart and receive only newer rows; a cursor older than the horizon yields `cursor_too_old`; rollback yields `cleared` with `new_generation = old + 1`.
- **P3 H4**: typed live state plus `on_agent_live_state`. Check: `awaiting` appears within one heartbeat of a pending approval and clears on `post_approval_response`; killing the agent thread without a terminal event makes the state go stale per `stale_after_ms`, not "running".

Exit gate for each phase: its check passes, the existing `/v1/runs` and `tui_gateway` tests stay green, and the flag stays off by default.

## Tests
- `tests/test_conversation_plane_projection.py`: real plugin loader, loopback model. Three tool calls yield lean entries; redaction holds; template failure yields `summary None`.
- `tests/test_conversation_plane_send.py`: real `RunIdempotencyStore` on a temp DB. ACCEPTED, DUPLICATE, and REFUSED mapping; durable versus in-memory probe.
- `tests/test_conversation_plane_cursor.py`: temp `state.db`. Trigger-assigned `updated_seq` on insert, update, and compaction; tombstones; `cursor_too_old`; `cleared`.
- `tests/test_conversation_plane_live_state.py`: approval await and clear; staleness.
- Host-side tests belong in `hermes-agent/tests/`, beside the existing hosted-room hook tests [INFERENCE: exact test path not re-verified this session].
- Every test that asserts a redaction must use a seeded fake secret and assert the secret is absent from every emitted field, not only from `summary`.

## Risks & open questions
- Trigger-assigned `updated_seq` adds a write per UPDATE on the hottest table. Measure on a 100k-row session before enabling by default.
- Hermes already has `/v1/runs/{id}/events` without resume. Decision: the watch stream sits beside it, with no cutover of existing clients.
- Hermes rooms use their own durable room log (`turn.started` / `turn.settled`). The plane projects it but does not merge the two ledgers [INFERENCE: the room log write path was not read this session].
- `PROVIDER_CONTENT_BLOCKED` and `UNPAID_INVOICE` have no Hermes counterpart. Emit only codes the classifier actually produces.
- `generation` semantics differ. Grok Bot bumps on StartFresh; Hermes starts a new session id. We bump only on rollback, so clients must key cursors by session id.
- Grok Bot's full tool-call entry shape is unknown. Only the lean shape is evidenced.
- `on_agent_live_state` and `transcript_gc_horizon` are proposed new names; neither exists today.
- Idempotency keys are retained 24 h (`api_server_run_idempotency.py:58-59`). A client retrying a message older than 24 h gets a fresh run. Decide whether `message_id` needs a longer retention.
- The `messages.display_kind` column is already in use by other writers [INFERENCE: writer set not enumerated]. The `tool-call` kind must not collide with existing `display_kind` values.

## Non-goals
- Replacing `tui_gateway` events or the `/v1/runs` API.
- A plugin-owned HTTP server.
- Blob or presigned body storage.
- Unread counts, drafts, reactions, and widgets or cards. Approval and clarify already have native transports.
- Multi-user presence (Grok Bot `ReportGrokBotClientPresence`).
