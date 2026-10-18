# 32 notification-urgency-classifier

Status: proposed · Owner: — · Depends on: 01 (shared classifier client, label store); one host seam (H1: `pre_notification_delivery`); optional H2 (plugin send path) for P4

## Goal

Agent-to-user notifications (cron results, background process and delegation completions, kanban events, `send_message` pushes, team-bot posts) all arrive the same way: a push to the user's channel as soon as they exist. A label classifier ranks each one as `interrupt`, `digest` or `drop`, so a failed deploy still pings, a routine "all good" report waits in a digest, and a "nothing new" run never reaches the user. Producer-declared silence (`[SILENT]`) stays authoritative; the classifier extends the idea to producers that cannot declare it. Shadow first.

## Evidence

- Grok Bot changelog announcements carry `relevance_guidance` and a `delivery_window` (`NEXT_WORKING_WINDOW`, …) plus max offers and an eligibility gate (`grokbot-re/re/docs/backend-api.md:341,354` [decoded schema]). Relevance and timing are first-class fields, not afterthoughts.
- Grok Bot agents receive subscription notifications as a typed action: `SubscriptionNotificationAction` with `notifications` and `send_to_interaction_listener` (`re/docs/agent-protocol.md:468` [decoded]). Whether delivery is ranked server-side is not decoded [inferred: no].
- Droid ships classifier decisions that default to observe-only and measure first; only one changes behaviour (`/var/lib/hermes/projects/droid/notes/arch/classifiers.md` §0, §1 [note-sourced]). We copy that posture.
- Hermes cron already ships a classifier-gated delivery pattern at script level: only items at or above `--threshold` are printed; empty stdout suppresses delivery (`cron/scripts/classify_items.py:6-9` [source]).
- Classifier client pattern: `POST {text, schema}`, `None` on failure (`plugins/meta-classifier/client.py:33-55` [source]).

## Current state

Existing urgency mechanisms in Hermes core:
- Cron: the job saves its output first (`save_job_output`, `cron/scheduler.py:2655`), then decides delivery. `d.should_deliver = bool(deliver_content.strip()) and not _silent_alert` (`:2674`). Silent results are matched by the loose autonomous matcher (`_is_cron_silence_response`, `cron/scheduler.py:476-488`, delegating to `gateway/response_filters.py:59`); the marker is defined at `cron/scheduler.py:473` and requested in the job prompt (`cron/scheduler_prompt.py:196-199`). A failure notice is held while the model is unreachable and a retry is queued (`:2675-2684`, `cron/unreachable_retry.py`). Delivery itself is `_deliver_result` (`cron/scheduler_delivery.py:1679`), called for successes at `cron/scheduler.py:2709` and for failures at `:2798`.
- Live gateway replies: exact-match silence markers (`gateway/response_filters.py:12-56`); narration-only replies are dropped (`gateway/delivery.py:34-37`).
- Background processes: `display.background_process_notifications` is one of `concise` (default), `all`, `result`, `error`, `off` (`gateway/run_notifications.py:1721-1724,1735-1739,1776-1780`; loader at `gateway/run_config_loaders.py:383`). `off` also drops the queue event unless the agent is notified (`run_notifications.py:958-962`). Completions are batched and coalesced (`run_notifications.py:1367`) and formatted by `_format_gateway_process_notification` (`gateway/run.py:2938-2963`).
- Kanban: `kanban_task_completed` (adds `summary`) and `kanban_task_blocked` (adds `reason`) observer hooks (`hermes_cli/plugins.py:175-179`).
- Agent-initiated pushes: `send_message` (`tools/send_message_tool.py:33,202`).
- Background shell work: `terminal(background=True)` without `notify_on_complete=true` completes silently (`tools/terminal_tool_background.py:16-21`, hint applied `:160-161`); `_apply_async_support` (`:98-113`) drops the notify flags on finite sessions. Spawned waiters (`tools/bot_mode_dm.py:573-574`, `tools/bot_relay.py:316-318`) use `notify_on_complete`. Delegated children's completions reach the parent via a shared `completion_queue` (`tools/delegate_tool_registry.py:21-24`), so they need delegation attribution.
- Hosted-room Bot posts are room-log events (`gateway/hosted_rooms.py:1-4`); `on_room_member_activity` (`plugins.py:170`) reports member runtime frames only.

The gap: urgency is binary and set by whoever produced the message. There is no ranking across producers, no digest (the nearest thing is `all`/`result` modes and coalescing), no recipient-state input, and no `drop` for producers that never emit `[SILENT]` (process watchers, delegations, kanban, room bots). No hook sits between a producer and delivery.

## Design

Plugin `urgency-classifier`, plus H1.

- One call per notification, async relative to the producer: the delivery path waits only up to `timeout_ms` and otherwise proceeds as it does today.
- The plugin keeps a bounded `dedup_key` history (default 200 keys, 24 h) to supply `repeat_count`.
- Vendors the byte-identical shared `client.py` from plan 01 P1.

Config (`config_schema`):
- `endpoint` / `api_key_env` as siblings; `mode: shadow` (`shadow` | `apply`); `timeout_ms: 2500`; `cooldown_s: 45`.
- `apply_producers: []` (allowed: `cron`, `process`, `delegation`, `kanban`, `send_message`, `room_bot`; default none).
- `digest_max_age_hours: 24`; `quiet_hours: ""` (optional `22:00-07:00`, local); `log`, `log_path` (`$HERMES_HOME/logs/urgency-verdicts.jsonl`), `max_rows: 5000`.

Rule floors (before any call, never overridable):
1. A failed run (`success` false), a blocked kanban task, an approval or clarification prompt, and any security or account notice are never `drop`; the minimum is `interrupt`.
2. A producer-declared silence stays silence. The classifier cannot revive a suppressed message, and its `drop` is consulted only when the producer did not decide.
3. Output stays saved locally whatever the verdict (cron already does this, `:2655`); `drop` means no push, not no record.
4. `quiet_hours` demote `interrupt` to `digest` only for non-floor items.

## Labels

Schema: `{"urgency": {"labels": {...}}}`, single-label.

| Label | Definition | Positive example | Negative example |
|---|---|---|---|
| `interrupt` | The user must see it now: it needs a decision, or something they depend on broke. | "Deploy job failed: migration error, prod unchanged." | "Daily summary: 12 items processed." |
| `digest` | Useful, no action needed soon. Batch it. | "Weekly report ready: 3 PRs merged." | "Disk at 97 %, next write fails." |
| `drop` | No new information for the user: unchanged state, a repeat, or narration. | Watch job: "no changes since last run" (third time today). | "Build failed on main." |

## Input contract

- Hook: new `pre_notification_delivery` (H1). Until it exists, P1 observes only the existing hooks: `kanban_task_completed` / `kanban_task_blocked` (`plugins.py:179`) and `post_tool_call` for `send_message` (`plugins.py:131`; kwargs not verified here, P1 checks them).
- Fields, one text block of ≤ 2000 characters:
  - `producer`, `title`, `outcome` (`success|failure|blocked|info`), `duration_s`;
  - body text: first 1000 and last 500 characters (the tail usually holds the result), joined with an elision marker;
  - `repeat_count` (same `dedup_key` in the last 24 h) and `since_last_s`;
  - `recipient_state`: seconds since the user's last message and `quiet_hours` flag.
- Truncation: per-field caps, then the 2000 cap; clipped, never refused.
- Redaction: body and title through `agent.redact.redact_for_egress` (`agent/redact.py:807`). Host formatters have already applied `_redact_gateway_user_facing_secrets` (`run_notifications.py:1369,1706`); the plugin still redacts again. Paths and recipient names are not sent.

## Decision wiring

- Consumers, once H1 exists: the cron tail at `cron/scheduler.py:2674-2692` (`d.should_deliver`), its success delivery at `:2709` and failure delivery at `:2798` (both `_deliver_result`, `cron/scheduler_delivery.py:1679`); the process watcher send at `gateway/run_notifications.py:1779-1780` (`_send_watcher_message`, `:1656`); async delegation delivery (`gateway/run.py:2961-2963`); and `send_message` (`tools/send_message_tool.py:202`). Each would call one shared helper that fires H1 and applies the result. `notify_on_complete` completions use the watcher path (`gateway/run_notifications.py:1732`); P2 confirms this before relying on it.
- H1 returns `{"action": "deliver"|"digest"|"drop", "reason", "digest_key"?}`. Default `deliver` (today's behaviour).
- Shadow (default): the plugin logs `{ts, producer, dedup_key, verdict, rule_floor?, applied: "deliver"}` and returns `deliver`.
- Promoted, per producer in `apply_producers`:
  - `interrupt`: deliver unchanged.
  - `digest`: not pushed. The row goes into the plugin's `ctx.state`, is injected through `pre_llm_call` into the user's next turn as "while you were away" and listed by `/digest`. A digest row older than `digest_max_age_hours` is delivered as a plain notification (needs H2).
  - `drop`: suppressed; the saved output and the log row remain.

## Data & evaluation

- Labelled data:
  1. Cron outputs, already on disk (`save_job_output`, `scheduler.py:2655`). Suppressed `[SILENT]` runs are explicit `drop` positives; failed runs are explicit `interrupt` positives. No new collection is needed to start.
  2. Replay of gateway completion events from the session store and kanban events from the board.
  3. Human labels: `/urgency-label <row> interrupt|digest|drop` and `hermes urgency-label`, in the plan 01 label shape (`01-meta-classifier.md:111`, `kind: urgency`). The false-positive report path follows plan 02 D6 (`02-approval-grants.md:46`): "you buried something I needed" and "you pinged me for nothing".
  4. Implied labels (reported apart, never used for the gate): the user replied within 10 minutes of a delivered notification, or left a digest row unread.
- Metric: per-label precision and recall; headline errors are a real item dropped or digested.
- Promotion gate, on at least 300 labelled notifications, at least 60 per label and at least 50 failures or blocked items:
  - `drop` precision ≥ 0.98, and zero floor violations in the log;
  - `interrupt` recall ≥ 0.95;
  - `digest` precision ≥ 0.80.
- Latency: p95 ≤ 2.5 s. This is off the user's latency path; delivery proceeds unchanged on a late verdict.

## Failure policy

- Timeout, no key, cooldown, transport error, unusable body: verdict `None`, action `deliver` (today's behaviour). A dead service must never silence the agent.
- Unknown label or more than one label: `None`, never mapped to a nearest label.
- Low confidence: the service returns labels, not scores (`schemas.py:78-81`). An empty reply is `None`.
- Verdict after the producer already delivered: logged `late`, not applied.
- Digest store unwritable: the notification is delivered normally, with a warning.
- Never fabricated: an absent verdict is recorded absent, never as `interrupt`.

## Host changes

H1 `pre_notification_delivery` (add to `VALID_HOOKS`, `hermes_cli/plugins.py:110`), directive-returning, bounded by a host timeout like other directive hooks. Fired by one shared helper called from the four sites named under Decision wiring. Kwargs: `producer`, `session_key`, `platform`, `chat_id`, `title`, `text`, `outcome`, `duration_s`, `dedup_key`, `producer_declared_silent`. Why no existing hook suffices: `post_tool_call` and the kanban hooks see producer events but cannot gate the cron, process and delegation delivery paths, and `transform_llm_output` (`plugins.py:133`) covers model text only.

H2 (P4, optional) a plugin send path for digest flush. `tools/send_message_tool.py:590` (`_send_to_platform`) is tool-internal; exposing it as a `ctx` method is a separate, small upstream change.

## Phases

- P0 offline replay. Label 100 saved cron outputs; run the classifier offline. Accept: a confusion matrix against `[SILENT]` and failed-run labels; no change to any live path.
- P1 shadow on existing hooks (kanban, `send_message`). Accept: one row per event; delivery unchanged; `repeat_count` increments on a repeated key.
- P2 H1 seam, observer-only, at all four sites. Accept: a cron run, a process completion and a delegation completion each produce one row, and delivery is byte-identical to a run without the plugin.
- P3 apply `digest` for cron successes only, behind `apply_producers: [cron]`. Accept: a successful job lands in `/digest` and not in the channel; a failed job still delivers; saved output exists in both cases.
- P4 apply `drop` and H2 flush, after the gate. Accept: a `drop` verdict leaves a saved output and a log row but no push; a digest row past `digest_max_age_hours` is sent once.

## Tests

New `tests/test_urgency_classifier.py`, real native loader, loopback fake `urlopen`:
- Floors: failed run, blocked task and producer-declared silence are never `drop` and never revived.
- Shadow: the handler always returns `deliver`.
- Each label parses; unknown or multi-label replies yield `None` and `deliver`.
- Redaction: a bearer token in the body is absent from the transmitted text.
- Dedup: three identical keys give `repeat_count` 3 in the third request.
- Digest: a `digest` verdict stores a row and `pre_llm_call` injects it once.
- Failure: stalled service and cooldown give `deliver`.

## Risks & open questions

- A dropped notification the user needed is the costly error; this is why `drop` is gated hardest and failures can never be dropped.
- Digest surfaces only when the user next writes, so an absent user sees nothing. `digest_max_age_hours` and H2 bound this; without H2, P4 does not ship.
- Cron output is a plausible `drop` corpus, but jobs whose prompt never says `[SILENT]` produce no negatives. Their labels come from human labelling only.
- Digest and the existing `concise|all|result|error|off` modes overlap for processes; the plugin sits on top and does not replace the setting.
- Quiet hours need the user's timezone; the plugin reads it from config only and does not infer it.

## Non-goals

- Changing `[SILENT]`, the `display.background_process_notifications` setting or per-job `deliver` routing.
- Rewriting or summarising notification text.
- Approval and clarification prompts; they are interactive, not notifications.
- Hosted-room membership or addressing (plan 31).
- Applying any verdict before the promotion gate is met.
