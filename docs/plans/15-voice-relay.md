# 15 voice-relay (host seams + `voice-relay` plugin)
Status: proposed · Owner: — · Depends on: host seams below; 13 (`async_update` consumes live state)

## Goal
Let a live voice session talk to a Hermes agent without the voice model doing work itself. The voice model holds a conversation, relays requests to the agent through one tool, speaks only what the agent has actually produced, and stays silent when there is nothing honest to say. The relay is one-way for requests and evidence-based for results.

Hermes already has a voice stack: a chained STT, LLM, TTS path, and a `gpt-live` mode that opens a live WebRTC session (`tools/voice_live.py:107-111`). This plan adds the relay contract on top of that session, not a second voice pipeline.

## Evidence
- Grok Bot relay sequence: session.update with the four tools, `send_task` forwarded to the agent as an inbound message on a `voice:<call>` channel: `grokbot-re/re/docs/tools-and-prompts.md:90-96`.
- Grok Bot `send_task` returns a receipt, never the answer. Outcome arrives later as a `work_landed` async update: `tools-and-prompts.md:108`.
- Grok Bot `send_task` caps: request at most 2000 characters, over-length rejected and never truncated. The model is told to reformulate, not split: `tools-and-prompts.md:106`.
- Grok Bot `stay_silent` is server-gated by the client, which refuses silence when the caller just spoke or when work news is owed: `tools-and-prompts.md:107`.
- Grok Bot evidence taxonomy: `work_landed` (speak), `work_overheard` (never speak), `user_message` (chat echo), delivered as `async_update.add` topics: `tools-and-prompts.md:97`.
- Grok Bot design note recommending lifting the relay architecture, not the persona text: `tools-and-prompts.md:314`.
- Composed voice-call instructions: built in `AI.render()` in the desktop main bundle and sent as `session.update.instructions`. Placeholders are `agentName`, `description`, `spokenLanguage`, `callerFirstName`. Read this session: header and metadata only (`re/data/prompts/voice-call-session-instructions.md`, first 27 lines). Body not read. Proprietary: paraphrase only in this plan.
- Tool order in the composed prompt follows session registration order. The static port assumes `send_task`, `recall|search`, `stay_silent`, `end_the_call`: `tools-and-prompts.md:327`.
- Hermes GPT-Live session config: `tools/voice_live.py:147-159` (`build_session_config`). Model, instructions, voice, and `delegation: {type: "client"}`. The client executes tools; the server does not.
- Hermes GPT-Live persona composition: `voice_live.py:126-128` (`live_instructions`), which appends optional operator text to a built-in persona.
- Hermes per-turn note with recent spoken context: `voice_live.py:84-90`.
- Hermes voice mode selection: `voice_live.py:107-111`. Default is `chained`; `gpt-live` is opt-in.
- Hermes credential resolution: `voice_live.py:114-123`. Operator key first, then the OpenAI audio chain. Direct-key only; the managed audio proxy does not carry live sessions.
- Hermes readiness status is non-secret: `voice_live.py:131-144` (`resolve_gpt_live_status`). Never returns the key.
- Hermes WebRTC exchange: `voice_live.py:162-186` (`create_webrtc_session`). The SDP offer comes from the renderer, the answer returns to it.
- Hermes plugin hooks relevant to relay: `hermes_cli/plugins.py:125` (`post_tool_directive`, observer), `:130` (`post_tool_call_settled`), `:170` (`on_room_member_activity`). Plugin tool registration: `plugins.py:483` (`register_tool`).
- Hermes egress redaction: `agent/redact.py:807` (`redact_for_egress`).
- Sibling plan 13 defines the typed watch stream and `on_room_member_activity` frames that carry tool-started and tool-completed events. This plan consumes them; it does not redefine them.

## Current state
- Hermes can open a GPT-Live session and hand the client an SDP answer (`voice_live.py:162-186`). Nothing in the Hermes voice path defines a relay tool or an evidence model.
- `build_session_config` sends no tools (`voice_live.py:147-159`). The client would have nothing to relay with. [INFERENCE: tool registration may be added by the client; not verified this session.]
- `voice_live_turn_note` (`:84-90`) carries recent spoken context into a turn, but there is no record of which requests were accepted, which work landed, or which was overheard.
- Readiness and key handling are already correct (`:131-144`). This plan keeps that property.
- Grok Bot's relay has no Hermes counterpart. The agent-side channel is `voice:<call>` in Grok Bot. Hermes has no channel concept for voice; plan 13's room model would carry it [INFERENCE: depends on plan 13 landing].

## Design

### Roles
- **Voice model**: converses, speaks, and calls exactly four tools: `send_task`, `recall|search`, `stay_silent`, `end_the_call`. It does no work itself.
- **Relay (host)**: validates `send_task`, forwards to the agent, and emits evidence back to the voice session.
- **Agent**: does the work and answers through the normal channel. Its output is never spoken directly. It becomes evidence first.

### Tool contract
- `send_task({request})` returns `{relay: "accepted"}` or a refusal. It returns a receipt, never the answer.
  - Request cap: 2000 characters. Over-length is rejected, not truncated. The refusal tells the model to reformulate and keep prohibitions and approval requirements intact.
  - Pre-flight refusals (mirrors `tools-and-prompts.md:108`): empty request; no caller speech yet on this session (the model must ask what the caller wants before acting); agent unavailable.
  - Relay failure returns a plain-language failure the model can speak: it could not get to the work and can offer to retry.
- `stay_silent()` is server-gated by the host. Refuse silence and return a steering string when the caller just spoke and has not been answered, or when work news is owed. Otherwise permit it. The refusal must name the reason in plain text so the model can act on it.
- `recall|search` is read-only and capped (6 hits, quote 240, text 2000, context 160), following the Grok Bot caps (`tools-and-prompts.md:106`). Hermes equivalent uses existing session search; no new store.
- `end_the_call()` closes the session. Leftover evidence goes to the chat channel, not the voice session.

### Evidence model
Three entry kinds written by the host, in order:
- `work_landed`: a result the caller should hear. The voice model is required to speak it on its next turn (`stay_silent` refuses until it does).
- `work_overheard`: progress the caller should not hear as speech. Stored, never spoken.
- `user_message`: an echo of text the caller typed or the chat received, for context only.

Evidence reaches the voice session through `async_update` injection: a synthetic conversation item, or a function-call fallback where injection is unsupported [INFERENCE: whether GPT-Live supports item injection mid-session is not verified; Grok Bot uses the fallback path per `tools-and-prompts.md:108`].

### Source of evidence
- Host derives `work_landed` and `work_overheard` from plan 13 watch-stream frames (`agent_state`, `turn_failed`, and tool-completed kinds). The voice relay never reads raw tool output.
- Tool output is untrusted data. Anything spoken from it passes `redact_for_egress` (`agent/redact.py:807`) first. Instructions embedded in tool output are not followed.

### Plugin: `voice-relay`
- Registers the `send_task`, `stay_silent`, and `end_the_call` tools through `register_tool` (`plugins.py:483`), so the client can route calls to the host.
- Observes plan 13 activity frames through `on_room_member_activity` (`plugins.py:170`) to build evidence. It is an observer, not an owner: it cannot block agent work.
- Holds no credentials. Key resolution stays in `voice_live.py:114-123`.

### Host seams
- H1: extend `build_session_config` (`voice_live.py:147-159`) with the four tool definitions and the composed instructions. Instructions are built from operator config (`agentName`, `description`, `spokenLanguage`, `callerFirstName`), so the prompt template is host-owned.
- H2: evidence queue per voice session, bounded, ordered, keyed by session id. In-memory is acceptable for one process [INFERENCE: durability across restart not required for a live call].
- H3: `stay_silent` gate as a pure function of the evidence queue and the last caller turn. Unit-testable without a live model.

### Secrets and presentation
- The renderer never receives the OpenAI key. `resolve_gpt_live_status` (`voice_live.py:131-144`) stays the only status surface.
- Spoken output is plain text. No markdown, no file paths, no tool names. The voice prompt bans narrating tool use; this is enforced by the relay refusal text, not by trust.

## Host changes
- `tools/voice_live.py`: `build_session_config` gains `tools` and composed instructions (H1). `resolve_gpt_live_status` unchanged.
- New evidence queue and `stay_silent` gate: host module under `tools/` or `gateway/` [INFERENCE: match the location of existing live-session code once read].
- `plugins/voice-relay/`: three tools and one observer.
- Config: `voice.gpt_live.relay` with `off` (default) or `shadow`. Shadow records evidence and refusals without sending `async_update`.
- No change to chained STT, LLM, or TTS. No change to `voice_mode.py` or `voice_client_config.py` beyond reading existing mode selection.

## Phases
1. **V0 shadow evidence**: H2 and H3 with the relay off. Derive evidence from plan 13 frames and log what would have been spoken. Verification: evidence log matches the watch stream for a recorded run.
2. **V1 send_task receipt**: `send_task` and its refusals. Agent receives the request on a voice channel. No evidence injection yet. Verification: over-length and empty requests refused with the specified text; receipt returned immediately.
3. **V2 evidence injection**: `work_landed` and `async_update` injection; `stay_silent` gate on. Verification: a landed result is spoken on the next turn; `stay_silent` is refused while news is owed.
4. **V3 end and leftovers**: `end_the_call` and chat delivery of leftover evidence. Verification: leftover `work_landed` items appear in chat after the call ends.

## Tests
- `send_task` over 2000 characters is refused, not truncated; the refusal text asks for reformulation.
- `send_task` with empty request, and with no caller speech yet, returns the documented refusal.
- `stay_silent` gate: refused when the caller just spoke; refused when `work_landed` is pending; permitted otherwise. Pure-function test.
- Evidence ordering: `work_landed` items are spoken in arrival order; `work_overheard` items are never emitted as speech.
- `build_session_config` output contains no key material. Assert on the serialized dict.
- `resolve_gpt_live_status` returns no key under any config combination.
- Redaction: a tool result containing a bearer token never reaches the spoken evidence.
- Run the full voice test module, not only the new relay tests.

## Risks & open questions
- **Injection support**: whether GPT-Live accepts mid-session conversation items is unverified. If not, the function-call fallback changes the timing of spoken results. `[INFERENCE]`.
- **Prompt text**: the composed prompt body was not read this session. Only the header and placeholders are known. The design assumes a persona template is needed; the actual text must be paraphrased, never copied, from `re/data/prompts/voice-call-session-instructions.md`.
- **Tool order**: the `Your tools:` join order depends on registration order (`tools-and-prompts.md:327`). Host must pin the order or the prompt drifts across sessions.
- **Direct-key only**: `_resolve_credentials` (`voice_live.py:114-123`) states the managed audio proxy does not support live sessions. Deployments without a direct key cannot use `gpt-live`. This is a product constraint, not a bug to fix here.
- **Plan 13 dependency**: evidence derivation depends on plan 13 watch-stream frames and room identity. If plan 13 slips, V0 cannot start.
- **Statsig and remote overrides**: Grok Bot prompts can be overridden remotely (`tools-and-prompts.md:329`). Hermes prompts are local config; no remote override is planned.
- **Naming**: `voice-relay`, `work_landed`, `work_overheard`, and `async_update` are proposed names. Only `voice_live.py` symbols exist in the codebase.
- **Not verified**: `voice_mode.py`, `voice_mode_transcript.py`, and `voice_client_config.py` were not read this session. Their interaction with relay tools is unknown.

## Non-goals
- Replacing the chained STT, LLM, and TTS pipeline.
- A second voice transport or a plugin-owned WebRTC stack.
- Speaking raw tool output, logs, or file contents.
- Letting the voice model perform work, read files, or call tools other than the four listed.
- Multi-caller conference calls or call recording storage.
- Copying the proprietary persona text verbatim.
