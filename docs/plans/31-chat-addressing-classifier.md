# 31 chat-addressing-classifier

Status: proposed · Owner: — · Depends on: 01 (shared classifier client, label store); one small host seam (H1: admission stamp on `MessageEvent`)

## Goal

In group chats where the agent answers without being @-mentioned, decide whether a message is for the bot at all. Hermes gates group traffic with a binary rule per platform (mention required or not). Where it is not required (free-response channels, joined threads, voice-linked channels), every message starts a full agent turn, including two humans talking to each other. A label classifier splits those messages into `addressed-to-bot`, `ambient` and `reply-to-other-human`, so ambient chatter costs no turn and no reply. Rollout is shadow first; the existing mention gates are untouched.

## Evidence

- Grok Bot rooms: `RequestGrokBotRoomMemberTurn` / `DeliverGrokBotRoomMemberTurnResult` carry per-member outcomes `PASS` / `SKIPPED` / `TIMEOUT` (`grokbot-re/re/docs/backend-api.md:357` [decoded schema]). A member can decline a turn; the decision is server-side and its model is not decoded [inferred].
- Grok Bot changelog delivery carries a `relevance_guidance` field per announcement (`backend-api.md:341,354` [decoded schema]), i.e. relevance is judged against free-text guidance, not a regex. Different surface, same idea.
- Hermes relay already models addressing as policy: `requireAddress`, `freeResponseScopes`, `allowOtherBots` (`gateway/relay/__init__.py:265-321` [source]).
- Existing agent-side silence: a model may answer with `[SILENT]` / `NO_REPLY` and the gateway suppresses delivery (`gateway/response_filters.py:12-56` [source]). That is an implied `ambient` label we already produce.
- Classifier pattern: `POST {text, schema}`, label dict back, `None` on failure (`plugins/meta-classifier/client.py:33-55` [source]).

## Current state

Addressing logic already in Hermes core, all per-adapter and lexical or structural:
- Discord: `_discord_require_mention` (`plugins/platforms/discord/adapter.py:4578-4580`, default true), free-response channel set (`:4711-4714`), `_in_bot_thread` (`:2189-2196`), explicit mention check `_self_is_explicitly_mentioned` (`:4730-4733`). The gate is at `:5716-5730`: a message passes if mention is not required, the channel is free-response, the channel is voice-linked, or the bot already joined the thread. Recovered messages use the same rule (`:2198-2211`).
- Telegram: `observe_unmentioned_group_messages` stores unmentioned group messages as context and dispatches only when addressed (`plugins/platforms/telegram/adapter.py:5069-5073,5445-5448`).
- Signal: `_apply_group_mention_rules` (`gateway/platforms/signal.py:374-390`, gate `:378-384`). WhatsApp: `_should_process_message` (`whatsapp_common.py:242-260`) admits a group message when free-response or `require_mention` is off, or when the body starts with `/`, is a reply to a bot id (`_message_is_reply_to_bot`, `:211-213`), mentions the bot (`:215-226`), or matches mention patterns (`:228-230`). BlueBubbles: no stable bot identity, so wake-word regexes (`bluebubbles.py:52-54`, defaults `:54`).
- Config keys bridged for all platforms: `require_mention`, `free_response_channels`, `mention_patterns`, `exclusive_bot_mentions` (`gateway/config_loader.py:193-197`).
- Hosted rooms (Bot Mode): `resolve_mentions` picks round-0 responders; no explicit mention means everyone (`default_all=True`, `gateway/hosted_room_discussion.py:291-307`). Later rounds are opt-in for peers a Bot cited (`:310-323,625-633`).
- `MessageEvent` already carries `reply_to_message_id`, `reply_to_text`, `reply_to_author_id`, `reply_to_author_name`, `reply_to_is_own_message` and `channel_context` (`gateway/platforms/event.py:63-67,75-77`).
- `pre_gateway_dispatch` fires once per incoming event, after the internal-event guard and before auth. A plugin may return `{"action": "skip", "reason"}` or `{"action": "rewrite", "text"}` (`hermes_cli/plugins.py:152-155`; `gateway/run_inbound.py:41-67`, called at `:186`). No plugin in this repo uses it (`grep` of `plugins/` returned nothing).

The gap: once a message is admitted by a free-response, thread or voice-linked rule, no check asks whether it is for the bot. The only brake is the model replying `[SILENT]` after a whole turn. A reply to another human with a reply reference is detectable by a rule; a reply without one, or a message addressed by name to a person, is not.

## Design

Plugin `addressing-classifier` on `pre_gateway_dispatch`, plus H1.

Pipeline per event:
1. Rule floors (no classifier call, verdict `rule:<why>`): DM (`source.chat_type == "dm"`); `reply_to_is_own_message` true (addressed); `event.is_command()`; `event.internal`; `allow_gateway_control` false; empty text with no media.
2. Rule shortcut: a reply reference whose author is not the bot (`reply_to_author_id` set, `reply_to_is_own_message` false) and no explicit mention of the bot in text gives `reply-to-other-human`, still logged in shadow. This also yields free weak labels for evaluation.
3. Otherwise call the classifier.

Config (`config_schema`):
- `endpoint` / `api_key_env` as siblings; `mode: shadow` (`shadow` | `apply`); `timeout_ms: 400`; `cooldown_s: 45`.
- `apply_labels: []` (default none; allowed: `ambient`, `reply-to-other-human`); `apply_channels: []` (platform:chat_id allowlist; empty means no channel is acted on).
- `context_messages: 4`; `log: true`; `log_path` default `$HERMES_HOME/logs/addressing-verdicts.jsonl`; `max_rows: 5000`.

State: append-only JSONL rows `{ts, platform, chat_hash, message_hash, admitted_by, rule|label, applied}`; no message text by default (`store_text: false`).

Hosted rooms: round-0 fan-out to all members when no mention is made (`hosted_room_discussion.py:305`) is a separate decision with no plugin hook. It stays out of scope (see Non-goals); P4 records it as an optional follow-up.

## Labels

Schema: `{"addressing": {"labels": {...}}}`, single-label.

| Label | Definition | Positive example | Negative example |
|---|---|---|---|
| `addressed-to-bot` | The speaker expects the bot to act or answer. | "can you also run the tests on that branch?" (follow-up in a bot thread, no mention) | "lol same" |
| `ambient` | Chatter between people, no ask of the bot. | "who's grabbing lunch?" in a free-response channel | "hermes, what's the build status?" |
| `reply-to-other-human` | Directed at a specific person, not the bot, even without a reply reference. | "@dana did you push the fix?" (or a reply to dana's message) | "@hermes did you push the fix?" |

## Input contract

- Hook: `pre_gateway_dispatch` (`plugins.py:155`), kwargs `event`, `gateway`, `session_store`.
- Fields sent as one text block of ≤ 2000 characters:
  - trigger text (≤ 1500), with the bot's own handle replaced by `<bot>` and other participant names replaced by `<user_a>`, `<user_b>`, in order of appearance (no real names egress);
  - `reply_to_text` (≤ 400) and a flag `reply_target: bot|other_human|none` (from `reply_to_is_own_message` / `reply_to_author_id`, never the id or name);
  - the last `context_messages` entries of `channel_context` (`event.py:75-77`), ≤ 800 total;
  - `chat_type`, `admitted_by`, `participants_recent` (count only).
- `admitted_by` (`mention|reply|free_channel|thread|voice_linked|wake_word|dm|unknown`) comes from H1. Without H1 it is `unknown` and the classifier still runs.
- Truncation: per-field caps, then the 2000 cap; over-cap clipped, never refused.
- Redaction: `agent.redact.redact_for_egress` over the whole block. Unlike `auto-answer-classifier` (`__init__.py:78-81`, abstains on secrets), a changed text is sent redacted; the classifier needs only tone and structure. Attachment bytes are never sent.

## Decision wiring

- Consumer: the `pre_gateway_dispatch` result handler in `gateway/run_inbound.py:58-67`. Today it honours skip and rewrite from any plugin.
- Shadow (default): the plugin always returns `None`. It records the verdict and what the adapter did, so over-triggering is measurable before anything is dropped.
- Promoted (`mode: apply`): for a channel in `apply_channels`, a verdict in `apply_labels` returns `{"action": "skip", "reason": "addressing:<label>"}`. All other verdicts return `None`. A skipped message is not part of any turn, so with plain `skip` it also drops out of context. Telegram's observe pattern shows the better outcome (store, do not dispatch; `telegram/adapter.py:5069-5073`). H1b (P4) adds `{"action": "observe"}` for that; until then apply is limited to channels where the loss is acceptable.

## Data & evaluation

- Weak labels for free, from the gateway's own signals:
  - Reply reference to a non-bot author: `reply-to-other-human` (rule). Reply to the bot's message: `addressed-to-bot`.
  - A dispatched message whose turn ends in `is_intentional_silence_response` (`response_filters.py:50-56`): implied `ambient`. Needs a `post_llm_call` correlation by session key; the kwargs there are unverified here (P1 check).
- Human labels: `/addressing-label <message_hash> addressed-to-bot|ambient|reply-to-other-human` and `hermes addressing-label`. Rows go to the plan 01 label shape (`01-meta-classifier.md:111`, `kind: addressing`); the false-positive report path follows plan 02 D6 (`02-approval-grants.md:46`): "the bot stayed out of a message meant for it" is the `false_negative`-style report, "the bot skipped my request" the `false_positive`.
- Replay: session-store transcripts from free-response channels, re-run through the classifier offline.
- Metric: per-label precision and recall; the headline is `addressed-to-bot` recall (a skipped real request).
- Promotion gate, on at least 400 labelled messages from at least three channels, at least 100 per label and at least 50 hard negatives (addressed without a mention or reply reference):
  - skipped-message precision (`ambient` + `reply-to-other-human` verdicts that are truly not for the bot) ≥ 0.97;
  - `addressed-to-bot` recall ≥ 0.98 on the hard-negative subset;
  - at least 60 % of the implied-`ambient` turns would have been skipped.
- Latency: p95 ≤ 400 ms; the hook sits on the gateway ingress path.

## Failure policy

- Timeout, no key, cooldown active, transport error: verdict `None`, return `None` (dispatch proceeds). The adapter's own admission already happened; failing open adds no new risk.
- Unknown label, multiple labels, empty reply: `None`, never mapped to a nearest label.
- Low confidence: the service exposes labels, not scores (`schemas.py:78-81`). A reply with no label is `None`. Never skip on `None`.
- Hook exceptions are caught by the host (`run_inbound.py:54-56`); the plugin also wraps its own body.
- Never fabricate: a skip reason always names a real verdict or rule.

## Host changes

- H1 `admitted_by` stamp: adapters set `event.metadata["admitted_by"]` at the gate sites above (Discord `:5728-5730`, Telegram, Signal, WhatsApp, BlueBubbles). Metadata is free-form (`event.py:80-81`); no schema change. Why no existing hook suffices: the plugin sees only the finished event, not which rule admitted it, so it could not tell a mention from a free-response pass.
- H1b `observe` action for `pre_gateway_dispatch` (P4): store the event in the session transcript without a turn. Optional.
- Also verify in P0 whether `_invoke_hook` runs sync on the gateway loop (`run_inbound.py:49`); if so the 400 ms budget needs a thread hop.

## Phases

- P0 inventory and replay. Enumerate per-adapter admission sites and the event fields each fills; build the offline corpus; add the 13-style replay. Accept: a table of adapter → `admitted_by` source; replay prints per-label counts and a confusion matrix.
- P1 shadow. Plugin loaded, hook returns `None`. Accept: a gateway run over a recorded channel produces one row per admitted group message and dispatch is byte-identical to a run without the plugin.
- P2 labels. Implied-silence labels plus `/addressing-label`. Accept: a turn that ended `[SILENT]` shows as an implied `ambient` row, tagged `implied`; the CLI adds a `human` row; they are reported separately.
- P3 apply in allowlisted channels. Accept: with `apply_labels: [ambient]` and one channel, an ambient message returns skip and creates no turn; a message in another channel and every DM dispatch normally.
- P4 observe action (H1b) and rooms follow-up, after the gate.

## Tests

New `tests/test_addressing_classifier.py`, real native loader, loopback fake `urlopen`:
- Rule floors: DM, own-reply, command and internal events never reach the classifier (zero fake requests).
- Reply-to-other-human rule shortcut fires without a call.
- Shadow returns `None` for every verdict; apply returns skip only for an allowlisted channel and label.
- Redaction: a token and a user name are absent from the transmitted text.
- Failure: stalled service, unknown label, two labels each yield `None` and dispatch continues.
- Cooldown: after one failure no second request is sent within `cooldown_s`.

## Risks & open questions

- A wrong skip silently drops a user's request. The gate and the allowlist exist for this; shadow data must show hard-negative recall before any apply.
- Free-response channels that rely on the agent joining naturally may expect chatter responses; `apply_channels` is opt-in per channel.
- `skip` removes context. H1b fixes it; until then apply stays narrow.
- Name replacement may confuse messages that address the bot by an alias; aliases come from `mention_patterns` (`config_loader.py:197`) and are read at registration.
- Hosted-room round 0 (`hosted_room_discussion.py:305`) is untouched; whether per-member "pass" decisions as in Grok Bot's `PASS` outcome (`backend-api.md:357`) belong here is open.

## Non-goals

- Replacing or loosening `require_mention`, `free_response_channels` or any adapter gate.
- Deciding who may talk to the bot (auth, pairing, allow-lists).
- Hosted-room member selection.
- Multi-bot arbitration (`allowOtherBots`, `exclusive_bot_mentions`).
- Applying any verdict before the promotion gate is met.
