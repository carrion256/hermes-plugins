# 24 recipient-novelty-classifier (seen-before / new-recipient / new-app / new-domain)
Status: proposed · Owner: — · Depends on: 02 (approval grants: standing-grant applicability), 23 (outbound content screen, shares the destination key), 01 (meta-classifier host). Labels: **Observed** = read in source this session; **Decoded** = from grokbot-re notes; **Inferred** = design judgement or unverified.

## Goal
Plan 02 issues standing grants ("always allow sending to X") and needs to know, at each outbound action, whether the destination is one the user has already sent to, or a new one that must fall back to a human prompt. This plan defines the **novelty label** that answers that question for `send_message`, network-bound terminal commands, browser navigation, and MCP calls with a destination argument. It also decides which parts are a deterministic store lookup and which, if any, need a model.

## Evidence
- **Observed** `tools/send_message_tool.py:43-52` `_resolve_tool_target` splits `platform:ref`, calls `resolve_send_target(platform, ref, pass_unresolved_references=…)`, and returns `(platform, chat_id, thread_id, error)`. A bare `platform` returns `chat_id=None`, and the caller falls back to the home channel.
- **Observed** `tools/send_message_tool.py:303-307` `_home_chat_id` resolves the home channel via `config.get_home_channel(platform)`. So "send to telegram" carries a *resolved* id, not the literal word `telegram`. Novelty must key on the resolved id.
- **Observed** `tools/send_message_tool.py:226-227` sets `used_home_channel = not chat_id` before home-channel resolution. This flag is available for audit, but the classifier must see the resolved id.
- **Observed** `tools/send_message_tool.py:250-253` (`_authorize_relay_target` with `native_token`) runs the relay egress guard *after* Slack user→DM resolution (comment at 240-247 says the position is load-bearing). The novelty key must be computed at the same point, from the resolved `chat_id`, or it will compare handles against conversation ids and mark every Slack DM as new. This is the bug the comment records.
- **Observed** `tools/send_message_tool.py:66-150` `_authorize_relay_target` already fails closed for relay targets via `gateway.relay.egress.authorize_relay_target`. It is an *attestation* check (is this destination provably the user's), not novelty. Plan 24 must not replace it; novelty only feeds plan 02 grants.
- **Observed** `tools/send_message_tool.py:673-675` the `target` schema accepts `platform`, `platform:#name`, `platform:chat_id[:thread]`, `user_name:@handle`-style forms. One recipient therefore has several surface spellings (`#bot-home` vs the numeric id). Normalisation is required.
- **Decoded** grokbot-re `re/docs/agent-protocol.md:139` `messages_op`: a send may return `messages_consent_result{verdict: STANDING|ASK|REFUSED, reason?}`. Consent is a three-way verdict, not a boolean. Plan 24 emits the input to that decision; it does not implement the server's verdict.
- **Observed** `hermes_cli/plugins.py` `pre_approval_request` / `post_approval_response` are observers; their return values are ignored (`plugins.py:160-163`, per plan 12 evidence). Novelty must therefore be computed in a `pre_tool_call` directive path or in the approval gate itself, not in an observer.
- **Inferred** No persisted "recipients this user has sent to" store was found in hermes-agent during this session. Gateway session metadata and the channel directory (`gateway.channel_directory.format_directory_for_display`, referenced at `send_message_tool.py:57-58`) are candidates for the seen-set source. Unverified until plan 02's grant store is read.

## Current state
| Piece | Where | Status |
|---|---|---|
| Target resolution | `send_message_tool.py:43-52`, `send_message_targets.resolve_send_target` | exists, returns resolved id |
| Home-channel fallback | `send_message_tool.py:303-307` | exists |
| Relay attestation | `send_message_tool.py:66-150` | exists, fail-closed, not novelty |
| Seen-before / novelty | none found | **missing** |
| Standing grant store | plan 02 | proposed |
| Destination key for shell/browser/MCP | none | **missing**; only `send_message` has a resolved target |

## Design
**Decision 1 — novelty is a store lookup first.** Maintain a per-profile seen-set of `(tool_family, destination_key)` rows, where `destination_key = sha256(platform_or_host | normalised_id)[:16]`. `seen-before` is membership. `new-recipient` is `tool_family == send_message` and no row. `new-app` is for `computer_use`/`launch` style actions (uses plan 12's app identity, not a model). `new-domain` is for `browser_navigate`/`web_extract`/terminal `curl|wget|git push` URLs: the registrable domain (eTLD+1) not in the seen-set. No model is needed for any of these four labels. Only alias collapse uses a model (decision 3).

**Decision 2 — the key is the resolved destination, computed at the same point as the relay guard.** For `send_message`, compute the key after `resolve_send_target` and after Slack user→DM resolution, mirroring `send_message_tool.py:236-253`. Keys from the literal `target` string are rejected: they would make `telegram` and `telegram:-1001234567890` look different and make a home-channel send look new.

**Decision 3 — model only for alias collapse, behind a deterministic normaliser.** Normalise first: lowercase, strip `@`/`#`, strip `.app`/`www.`, reduce URLs to eTLD+1 for domains, and map platform-native ids through the channel directory. Only residual pairs the normaliser cannot equate (e.g. the same person under two handles) go to the classifier head `same_recipient: yes|no|unknown`. `unknown` counts as *not same* (fail toward a prompt). This head must never be used to *grant* novelty-seen status; it can only merge two keys that the user has already approved.

**Decision 4 — labels are facts, not risk.** The classifier only reports novelty. Whether a new recipient is risky is plan 23's job (content) and plan 02's job (grant policy). Do not fold risk into this label.

## Labels
- `seen-before`: destination key present in the seen-set for this profile and tool family.
- `new-recipient`: `send_message` destination not in the seen-set.
- `new-app`: `computer_use` target app identity (pid/exe per plan 12 Decision 2) not in the seen-set.
- `new-domain`: outbound host eTLD+1 not in the seen-set.
- `unresolved`: destination could not be resolved to a key (no id, unknown platform, unparseable URL). Treated as **new** for grant purposes.

The label set is enforced by a `NOVELTY_SCHEMA` in the meta-classifier schema module (new head, plan 01 convention). Labels are exhaustive; an unexpected string is `unknown`.

## Input contract
- `tool_family`: one of `send_message`, `computer_use`, `browser`, `network_cmd`, `mcp`.
- `destination`: the **resolved** key material: `platform`, `chat_id`, `thread_id` for send; `app_identity` for computer use; `host` for network. Never the raw argument string.
- `profile_id`, `session_id`: scope the seen-set.
- `alias_candidates` (optional): up to 3 `(key_a, key_b)` pairs the normaliser could not merge. Only sent when the normaliser returns `unresolved`.
No free text from the agent or from the destination's messages is included. The novelty classifier must not see message content, so it cannot be steered by it.

## Decision wiring
- **Shadow first.** The novelty label is written to the audit log next to the plan 23 label. It affects nothing.
- **Promotion path.** Plan 02 reads the label when evaluating a standing grant: a standing grant applies only when the label is `seen-before`. `new-*` and `unresolved` fall back to the ASK path. Plan 02 must still run the plan 23 egress screen; a standing grant never skips the content screen (plan 02 D5/R7).
- **Recording.** A row is added to the seen-set only after a send succeeds (`send_message` returns `success`, or the terminal/browser action exits 0). Refusals and failures do not add rows, so a blocked send cannot make its destination "seen".
- **Hook.** The check runs in the `pre_tool_call` directive path (`hermes_cli/plugins.py:111`, the `"pre_tool_call"` entry in `VALID_HOOKS` at `:110`) as an `approve` annotation at most; it never issues `block` on its own, because novelty is not a violation. The annotation is `rule_key = novelty:<tool_family>:<label>:<destination_key>`, so plan 02 can bind the approval to the destination.

## Host changes
1. `tools/send_message_tool.py`: after line ~253 (after relay authorization succeeds), emit the destination key for the audit trail. Do not move the relay call.
2. New store: `plugins/meta-classifier/novelty_store.py` (a single SQLite table `seen(profile_id, tool_family, destination_key, first_seen, last_seen)`). Chosen over a JSON file for transactional upserts and per-profile row isolation; `sqlite3` is stdlib, so no new dependency. No existing grant or audit store in the repo uses SQLite (checked `tools/approval.py`, `agent/monitoring/`, `plugins/meta-classifier/`).
3. Schema: `NOVELTY_SCHEMA` added next to `INJECTION_SCHEMA` in `plugins/meta-classifier/schemas.py` (plan 25 uses the same convention).
4. Plan 02 consumes the label; the consumer change is owned by plan 02.

## Phases
1. **P0 (no model):** store, key normaliser, `send_message` key computed after resolution. Labels written to the audit log only. Unit tests on normaliser and store.
2. **P1 (shadow, all families):** add `computer_use`, `browser`, and `network_cmd` keys (domains and app identity). Measure how often `unresolved` appears.
3. **P2 (alias head):** enable `same_recipient` only if P1 shows a real unresolved rate that matters.
4. **P3 (promotion):** plan 02 consumes the label for standing grants, behind a config flag default off.

## Tests
- Normaliser: `telegram`, `telegram:-1001234567890`, and the home channel resolve to the same key for the same profile. A Slack `user_name:@handle` and its resolved `D…` id resolve to one key only after resolution.
- A failed send does not create a seen row.
- A relay-denied send does not create a seen row. Design requirement: the store write must come after `_authorize_relay_target` returns None (call sites `tools/send_message_tool.py:183` and `:250`, both before the send) and after send success. The store does not exist yet, so this is a constraint on P1, not an observed ordering.
- `new-domain` fires for a second URL on the same eTLD+1 only if the host was never seen (subdomain alone is not a new domain by the eTLD+1 rule; verify in test).
- Store isolation: a row for profile A does not make profile B `seen-before`.

## Data & evaluation
- Sources: (1) the seen-set itself, replayed from the audit log: every successful outbound action in the last 30 days, re-keyed with the current normaliser; (2) hand-labelled alias pairs for the `unresolved` bucket (target ≥ 50 pairs before P2).
- Metric: per-label counts per tool family on the replay set; the gate for P3 is that `new-recipient` false positives (a known recipient labelled new) stay ≤ 2% after a normaliser change. A deterministic store has no model metric; its correctness is covered by the tests above.
- The store is verified by replay, not by a classifier score: a replay diff that changes any label on the same log is a normaliser regression, and the gate fails.

## Failure policy
- Store unavailable (SQLite error, locked, corrupt file) → label `unresolved`, the audit row records `novelty-store-unavailable`. Never `seen-before` on error; a standing grant then falls back to ASK.
- Model alias head (P2 only) timeout, no key, or unusable reply → `unresolved`. No alias is merged on a failed call.
- Unknown label string from the alias head → `unresolved`, counted, never mapped to `seen-before`.
- Low confidence is not observable for the store; for the alias head, a merge is accepted only when the reply names an existing key from `alias_candidates`, otherwise `unresolved`.
- Destination cannot be resolved (`resolve_send_target` error or bare platform with no home channel) → `unresolved`. Plan 02 then asks.

## Risks & open questions
- The seen-set can grow without bound. Bound it per profile and expire rows after a configurable window, or the store becomes an unbounded log. Upgrade path: TTL column.
- Eligibility of the channel directory as a stable seen-set source is **unverified**; it may change ids when a channel is renamed.
- `new-app` depends on plan 12's target identity, which is itself Inferred on Linux.
- Whether `resolve_send_target` is deterministic for the same input over time is unverified; if not, keys drift and produce false `new-recipient`.
- Plan 02's grant store format is not yet frozen; the `rule_key` shape above must be aligned with it.

## Non-goals
- Deciding whether a destination is trustworthy or allowed (relay attestation, plan 02 policy).
- Reading message content or scanning for secrets (plan 23).
- Replacing `_authorize_relay_target`.
- Any model call on the grant path for `seen-before`.
