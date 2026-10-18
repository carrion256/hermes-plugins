# 30 voice-speak-classifier

Status: proposed · Owner: — · Depends on: 15 (voice-relay: evidence queue H2, `stay_silent` gate H3), 13 (watch-stream frames), 01 (shared classifier client, label store); one new host seam (H1 below)

## Goal

On a live voice call, decide for every piece of landed work whether the voice model should say it now, say nothing, or wait for a pause. Plan 15 makes the voice model speak whatever becomes `work_landed` and refuses `stay_silent` while news is owed. That rule is correct but blunt: a result that lands while the caller is mid-sentence is spoken over them, and a landed item that adds nothing (a duplicate, a no-op) still occupies a spoken turn. A small label classifier picks the timing and relevance. It never writes the words. The spoken text stays the host's redacted evidence, and the classifier cannot make the agent say anything it did not produce.

## Evidence

- Grok Bot taxonomy: `work_landed` (speak), `work_overheard` (never speak), `user_message` (echo). Delivered as `async_update.add` topics, with a synthetic `function_call` fallback (`grokbot-re/re/docs/tools-and-prompts.md:97,102` [decoded]).
- Grok Bot `stay_silent` is server-gated by the client (`staySilentPlan`). Refuse silence if the caller just spoke, if work news is owed, or if a receipt is owed; otherwise comply (`tools-and-prompts.md:107` [decoded]). The gate is rule-based; no classifier is evidenced there. Grok Bot has no timing decision such as "defer until pause" in anything decoded. That label is ours [inferred design].
- Grok Bot ships finished calls as one JSON per call under `voice-calls/` (`tools-and-prompts.md:109` [decoded]). It shows the shape of a replay corpus, not data we own.
- Plan 15 already fixes the evidence model and the gate inputs: `work_landed`, `work_overheard`, `user_message` (`15-voice-relay.md:52-56`), and a pure-function gate over "evidence queue + last caller turn" (`:72`).
- Classifier service pattern: `POST {text, schema}` to `/classify`, Bearer key from the environment, label dict back, `None` on any failure (`plugins/meta-classifier/client.py:33-55` [source]; `subagent-effort/__init__.py:152-166` [source]).

## Current state

- `tools/voice_live.py` opens the GPT-Live session and sends no tools (`build_session_config`, `:147-159`). It carries recent spoken context into a delegation turn (`voice_live_turn_note`, `:84-90`). There is no evidence queue, no `work_landed` concept and no `stay_silent` tool in Hermes (symbol check on `voice_live.py`; plan 15 records the same gap at `15-voice-relay.md:30-34`).
- Plan 15 is proposed, not built. Its H3 gate is a pure function and decides only refuse-or-permit for the voice model's own `stay_silent` call. It does not decide when to inject an item.
- `on_room_member_activity` (`hermes_cli/plugins.py:170`) carries tool and message frames for hosted rooms. Plan 15 uses it as the evidence source.
- Gap: nothing scores a landed item for relevance or timing. Every `work_landed` is spoken at the next turn boundary regardless of what the caller is doing.

## Design

Plugin `voice-speak-classifier`, plus one host seam. The plugin owns the verdict and its log. Plan 15 owns the queue, the gate and the injection.

- Runs once per `work_landed` or `work_overheard` entry as the entry is enqueued, off the audio path. It is a bounded daemon worker with a semaphore, like `subagent-effort` (`__init__.py:145-147`).
- A verdict is stored on the queue entry: `{label, source: classifier|rule|none, ts, latency_ms}`. The entry is delivered by plan 15's normal path when no verdict arrives.
- Vendors the byte-identical shared `client.py` from plan 01 P1.

Config (`plugin.yaml` `config_schema`):
- `endpoint` default `http://192.168.50.149:8787`; `api_key_env` default `CLASSIFIER_API_KEY` (same as siblings).
- `mode: shadow` (`shadow` | `apply`). `shadow` records and never changes delivery.
- `timeout_ms: 600`; `cooldown_s: 45`.
- `defer_idle_ms: 1200` (caller silence required before a deferred item is injected); `defer_max_hold_ms: 15000` (then speak regardless).
- `log: true`; `log_path: ""` (default `$HERMES_HOME/logs/voice-speak-verdicts.jsonl`); `max_rows: 5000`.

Rule floors, evaluated before any classifier call and not overridable by a verdict:
1. `turn_failed`, a blocked or approval-needed state, or an error frame is always `speak-now`.
2. An entry the caller explicitly asked about (an open `send_task` receipt with no result yet) is never `stay-silent`.
3. `work_overheard` never becomes speech. The classifier may not promote it.
4. The H3 refusals from plan 15 (caller just spoke and is unanswered) keep precedence over `stay-silent`.

## Labels

Schema sent to the service: `{"speak": {"labels": {...}}}`, single-label (label-dict form used by `plugins/meta-classifier/schemas.py:69-73`).

| Label | Definition | Positive example | Negative example |
|---|---|---|---|
| `speak-now` | The caller is owed this item and can hear it now. | Caller asked "open a PR"; item: "PR 12 opened, tests green." Caller is idle. | Item: "grepping src/ for the symbol." |
| `stay-silent` | Nothing honest or useful to say aloud: a duplicate, a no-op, or chatter already covered. It is still mirrored to chat. | Item repeats the result spoken 20 s ago ("tests green" again). | Item: "build failed with exit 2." |
| `defer-until-pause` | A real result, but the caller is mid-thought or on another topic. Hold until a gap. | Item lands while the caller is mid-sentence on an unrelated question (caller speaking). | Item lands in a 3 s silence right after the caller asked for it. |

## Input contract

- Source: plan 15's evidence queue (H2), fed from `on_room_member_activity` frames (`plugins.py:170`). The new seam H1 delivers each entry to the plugin; there is no existing hook at queue time. Until plan 15 lands, P0 runs offline over recorded frames only.
- Fields sent as one text block, 2000 characters at most:
  - `kind` (`work_landed` | `work_overheard`) and the host-derived summary of the entry (≤ 600).
  - `caller_state`: `speaking` | `idle` and `idle_ms`.
  - `last_caller_turn` (≤ 300) and `owed` (bool: an open receipt for this work).
  - `recent_spoken`: the last three voice-model turns (≤ 400 total), as in `voice_live.py:84-90`.
  - `pending_count`.
- Truncation: each field is clipped to its cap, then the whole is clipped to 2000. Over-cap input is clipped, not refused; the client refuses only above `MAX_TEXT = 100000` (`client.py:17,38`).
- Redaction: the summary is host evidence and has already passed `redact_for_egress` (plan 15, `15-voice-relay.md:62`). The plugin runs `agent.redact.redact_for_egress` over the whole block again before egress. File paths and tool names are replaced by `<path>` / `<tool>` tokens; the classifier judges relevance, not content. The raw tool output is never an input.

## Decision wiring

- Consumer: plan 15's `stay_silent` gate (H3, `15-voice-relay.md:72`) and the `async_update` injection step (`:58`). H1 adds one observer-first seam, `pre_voice_speak`, fired when an entry is enqueued, kwargs `session_id`, `entry`, `caller_state`, `recent_spoken`. A return of `{"action": "speak"|"silent"|"defer", "reason"}` is honoured only in `apply` mode.
- Shadow: the plugin records `{ts, session_id, entry_seq, kind, verdict, rule_floor?, applied_delivery}` where `applied_delivery` is what plan 15 actually did. Delivery is untouched. Agreement and the two error directions come from this log.
- After promotion (`mode: apply`):
  - `speak-now` injects at once (the plan 15 default).
  - `defer-until-pause` holds the entry until `defer_idle_ms` of caller silence or `defer_max_hold_ms`, then injects. The held entry still counts as "news owed" for the H3 refusal, so the voice model cannot go silent over it.
  - `stay-silent` demotes a `work_landed` to `work_overheard` and mirrors it to chat as plan 15 V3 leftovers (`15-voice-relay.md:89`). It is never discarded.

## Data & evaluation

- Labelled data:
  1. Replay of real calls. Needs the plan 15 V0 evidence log (`15-voice-relay.md:86`) plus caller-activity timing. No such corpus exists today.
  2. Human labels through `/voice-speak-label <seq> speak-now|stay-silent|defer-until-pause` and `hermes voice-speak-label`. Rows go to the plan 01 label file shape (`01-meta-classifier.md:111`), with `kind: voice` added.
  3. False-positive reports in the plan 02 sense: "you stayed silent but I wanted that" and "you spoke over me" (`02-approval-grants.md:46`, D6 label event).
  4. Synthetic cases written from the Grok Bot taxonomy, used only for smoke tests, not for the gate.
- Metric: per-label precision and recall; `stay-silent` false rate (a wanted item silenced) is the headline error because it hides a result.
- Promotion gate, on at least 300 labelled entries with at least 80 per label and at least 40 failure or blocked entries:
  - `stay-silent` precision ≥ 0.97 (a wrongly silenced result is the costly error), and zero rule-floor violations in the log;
  - `speak-now` recall ≥ 0.95;
  - `defer-until-pause` precision ≥ 0.85, and the median added delay ≤ `defer_max_hold_ms`.
- Latency budget: p95 ≤ 600 ms end to end per entry; a verdict that is late is simply not applied (the entry already went out under plan 15's default).

## Failure policy

- Timeout, transport error, no key, or an unusable body: verdict `None`, row marked `unavailable`, plan 15's default behaviour (speak `work_landed` at the next turn). The 45 s cooldown stops a dead service from taxing every entry.
- A label outside the three, or more than one label: treated as `None`. Never mapped to a nearest label.
- Low confidence: the service returns labels, not scores (`schemas.py:78-81`). An empty reply is `None`. Verdicts below any future `cls_threshold` are reported as `None`, not coerced.
- Verdict arrives after the entry was delivered: logged as `late`, never applied.
- Never fabricated: a missing verdict is recorded as missing, not as `speak-now`.

## Host changes

H1: `pre_voice_speak` hook (add to `VALID_HOOKS`, `hermes_cli/plugins.py:110`). Fired from plan 15's evidence queue on enqueue. Observer in P1; directive-returning in P3. Why no existing hook suffices: `on_room_member_activity` reports frames, not queue entries or caller state, and `pre_llm_call` fires per model turn, not per evidence item. Depends on plan 15 H2/H3 existing.

## Phases

- P0 offline replay. Build the corpus from plan 15 V0 logs and hand-label 100 entries. Accept: the replay CLI prints per-label counts and a confusion matrix against the human labels; no network call to production.
- P1 shadow. Plugin registered, seam H1 observer-only. Accept: with `mode: shadow`, a recorded run produces one row per entry; delivery is byte-identical to a run without the plugin.
- P2 labels. `/voice-speak-label` and the CLI. Accept: a labelled entry shows in the replay set; the label file holds no raw tool output.
- P3 apply for defer only. `defer-until-pause` honoured; `stay-silent` still logged only. Accept: an item landing while the caller speaks is injected after `defer_idle_ms`; at `defer_max_hold_ms` it is injected regardless.
- P4 apply stay-silent, after the gate. Accept: a demoted entry appears in chat and never as speech; rule floors hold under a forced `stay-silent` verdict.

## Tests

New `tests/test_voice_speak_classifier.py`, real native loader, loopback fake `urlopen` (network is blocked by `tests/conftest.py`):
- Each label parses to a verdict; an unknown or multi-label reply yields `None`.
- Rule floors beat a forced `stay-silent` for failure, open-receipt and overheard entries.
- Redaction: a bearer token in the entry never appears in the transmitted text (assert on the serialized request).
- Shadow mode: the return value of the seam handler is always `None`.
- Timeout and cooldown: a stalled fake service returns `None` and arms the cooldown.
- Defer timing: with a fake clock, an item is held until idle or max hold.

## Risks & open questions

- Plan 15 is unbuilt and its injection support for GPT-Live is `[INFERENCE]` (`15-voice-relay.md:102`). P1 cannot start before plan 15 V0.
- Caller-speaking state must come from the client (renderer audio activity); `voice_mode.py` was not read here. If the host cannot see it, `defer-until-pause` has no input and drops out; the other two labels still work.
- Added delay can make a call feel unresponsive; `defer_max_hold_ms` caps it, but 15 s may be too long in practice. Tune from P2 data.
- A silent verdict is the costly error; the stay-silent gate is therefore the strictest. If the corpus cannot reach 40 failure cases, P4 does not ship.
- Label balance: most entries are `speak-now`; the corpus will be skewed and the other two labels may need synthetic oversampling for training review only, never for the gate.

## Non-goals

- Writing or paraphrasing what the voice model says.
- Deciding whether to hand work to the agent (`send_task`); that is plan 15.
- Promoting `work_overheard` to speech.
- Any audio-level turn detection or barge-in handling.
- Applying a verdict before the promotion gate is met.
