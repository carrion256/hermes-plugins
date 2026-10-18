# 23 outbound-content classifier
Status: proposed · Owner: — · Depends on: 01 (shared client, label store, replay), 02 (approve path); coordinates with 24, 25
Labels used below: **Observed** = read in source/docs this session; **Decoded** = from RE notes; **Inferred** = design judgement.

## Goal
Before anything leaves the machine, screen the outbound payload and label it `contains-secret`, `pii`, `internal-only` or `ok`. "Leaves the machine" covers messages (`send_message`), web requests that carry data (URLs with query strings, `curl`/`wget` bodies), email, gists and other publish commands run through `terminal`, and MCP tool arguments. A deterministic scrub does the exact work (known secret shapes, registered vault values, PII shapes); the classifier covers what regexes cannot: internal hostnames, customer data, private-looking prose. In shadow it only records. Once promoted it escalates a flagged call to the human gate. It never rewrites the payload.

## Evidence
- **Droid Shield** (Observed, `/var/lib/hermes/projects/droid/notes/arch/classifiers.md` §3): runs only before `git commit`/`git push`, after a deterministic scanner. Findings go to a `shield-downgrade` head ("clear false positive?"); secret-named lines with no finding go to `shield-risk` ("did the regex miss one?"). Output `{verdict: S|B}`, thresholded on logprobs (downgrade P(B) ≥ 0.3775 blocks, risk ≥ 0.95 warns). Downgrade failure blocks; risk failure is silent. Input is a scrubbed window `{extension, lines, focus_line}`. Droid has no classifier for non-git egress.
- **Hermes redaction** (Observed, hermes-agent): `agent/redact.py:807-819` `redact_for_egress` is "the one scrub for text leaving the process", fails closed to `[redaction-unavailable]`. `redact.py:66-76` `redact_registered_vault_values` is an exact-substring registry of vault secrets (profile-scoped, max 64 per profile, memory only). `agent/monitoring/redaction.py:16-33` `redact_for_export` adds PII shapes (email, phone, UUID). `plugins/platforms/a2a/security.py:159` `redact_outbound` is a third copy of the same idea for A2A peers.
- **Grok Bot** (Decoded): egress is a policy object fetched per box (`GetGrokBotEgressPolicy`, `re/docs/backend-api.md:113`); sends to a recipient go through the `send-imessage` action with per-recipient consent (`re/docs/local-exec-daemon.md:220,358`). No content classifier is decoded.
- **Grok Build** (Source, `re/docs/grok-build-platform.md:218,792`): the auto-mode classifier lists "push to a remote … carrying secrets" as hard-wait (block-class). Its egress proxy is an HTTP/1.1 forward proxy plus CONNECT that relays opaque bytes with no TLS interception (line 792). The conclusion that content screening therefore happens only in the tool layer is Inferred.
- **Existing WIP** (Observed): `plugins/meta-classifier/__init__.py:276-300` `shield_contexts` already derives "findings" as lines that native redaction changes, and `text.py:12-18` `egress()` raises when the scrubber cannot vouch.

## Current state
- `send_message` (`tools/send_message_tool.py:33,202`) resolves the target and sends. It has a relay-target authorization (`:66`) but no content check.
- Gateway and A2A paths scrub with `redact_for_egress`; the tool path does not scrub the model's message text. Gap: a secret the model pastes into `send_message`, a `curl -d`, a gist or an MCP argument is not screened. Whether `terminal` output redaction covers the *command* text is unverified (P0).
- `meta-classifier` Shield screens only `git commit|push` (`_GIT_WRITE`, `__init__.py:55`). Its `_SKIP` set includes `web_extract` (`:43`), so URL-borne data is unscreened.
- `pre_tool_call` can return `block` or `approve` (`hermes_cli/plugins.py:1876-1886`). There is no allow, and arg rewriting is not assumed.
- The gap: one egress screen across surfaces, with a label that is not secret-only.

## Design
**Plugin shape (decision):** a new addon `egress` in `plugins/meta-classifier/addons.py`, next to `injection`. Addons share the classifier, the signals log and in-process state (needed so plan 25 can taint a session; plugins have no cross-import path, plan 01 §Current state). It is off until named in `addons`.

**Boundary with plan 01 (decision):** Shield keeps `git commit|push` diffs. This addon skips any command matching `_GIT_WRITE` and handles everything else. One classifier call per tool call, never two.

**Stage 1, deterministic (no model):** build the outbound text per surface (below), then compute `scrubbed = egress(text)` (`text.py:12`). If `scrubbed != text` → floor label `contains-secret`, with no model call. Add `redact_registered_vault_values`. An unavailable scrubber means "unscreened" (never `ok`).

**Stage 2, model:** only when stage 1 found nothing. Send the scrubbed, windowed text to one head `egress_class` with the four labels. The service returns labels only, no probabilities (`schemas.py:78-81`, per plan 01 Risks), so there is no threshold to port from Shield.

**Surface map (Inferred; verify in P0):**
| Surface | Hook | Text screened |
|---|---|---|
| `send_message` | `pre_tool_call` | `args.message` (+ MEDIA file names only) |
| `terminal` with `curl`/`wget`/`http`/`gh gist`/`gh issue|pr create`/`mail`/`scp`/`rsync` | `pre_tool_call` | command string + `@file` / `-F x=@file` / `gist create <file>` targets, first `egress_file_bytes` of each file inside the workdir |
| `web_extract`, `browser_navigate` | `pre_tool_call` | URL query and fragment only |
| `mcp_*` | `pre_tool_call` | JSON args, values only |
`terminal` command detection is a small regex list in config (`egress_commands`), not a parser.

**Config keys (defaults):** `egress: shadow` (`off|shadow|escalate`), `egress_tools` (list above), `egress_chars: 8000` (head+tail window via `text.window`), `egress_file_bytes: 16384`, `egress_escalate: ["contains-secret","pii"]`, `egress_log` (reuses `signals_path`).

**Records:** `{addon:"egress", tool, surface, label, stage: "deterministic"|"model", textDigest, chars}`. No payload text is stored (the digest is of the already-scrubbed window).

## Labels
Head `egress_class` (one label per call; precedence `contains-secret` > `pii` > `internal-only` > `ok`):
- `contains-secret` — credential-shaped value: API key, token, private key, password, connection string with a password, session cookie. Pos: `curl -H "Authorization: Bearer sk-live-…"`. Neg: `export API_KEY=<your key>` in a README snippet.
- `pii` — a real person's contact or identity data: email, phone, postal address, government id, health or financial detail about a named person. Pos: "Call Dana Ruiz at +1 415 555 0134 about the diagnosis." Neg: `noreply@example.com`, a UUID, a vendor support address.
- `internal-only` — material that is private to the user's org or machine: internal hostnames and IPs, private repo paths, unreleased plans, customer lists, local filesystem paths. Pos: "db-prod-3.corp.internal is out of disk; see /home/ana/clients/acme/contracts". Neg: a public docs URL, a generic stack trace.
- `ok` — nothing above. Pos: "Build passed on main; PR #412 is ready." Neg: (any of the above).
`internal-only` is a user-policy label. The definition text is configurable (`egress_internal_hint`) so a user can name their own domains; it is not inferred.

## Input contract
- Hook: `pre_tool_call` (`VALID_HOOKS`, `hermes_cli/plugins.py:110-111`), kwargs `tool_name`, `args`, `session_id`, `tool_call_id`.
- Fields: the surface text above only. No transcript, no tool results, no system prompt.
- Budget: ≤ `egress_chars` (8000) after `text.window` head+tail; service limit is 100000 (`client.py:17`).
- Redaction before send: `egress()` (always), then registered vault values. A text the scrubber cannot vouch for is not sent (unscreened).
- The check runs on the classifier worker pattern of `subagent-effort` (bounded concurrency, no unbounded backlog) only in shadow; in `escalate` mode it is inline with the 2.5 s client timeout.

## Decision wiring
- Shadow (default): record only, return `None`. Native approvals and floors are untouched.
- After promotion: for labels in `egress_escalate`, return `{"action":"approve","message":…,"rule_key":"egress:<tool>:<label>:<sha256[:16]>"}` from `pre_tool_call` (`plugins.py:1876-1886`). The human sees the label and surface, never the payload. The gate fails closed on error or timeout (`plugins.py:2028-2042`). `internal-only` stays record-only unless the user lists it.
- Plan 02: a human `deny` writes an implied label; a "report false positive" choice (plan 02 D6) writes a `false_positive` row to plan 01's label store. A standing grant never skips this screen: grants are consulted after floors and plugin blocks (plan 02 D5, R7), and `rule_key` carries the label so an `always` for `egress:send_message:ok` cannot authorise a `contains-secret` send.
- The label from stage 1 is deterministic and may be a hard `block` for `contains-secret` when the user sets `egress_secret_action: block`; default is `approve` (escalate), because a scrub match can be a documented example.

## Data & evaluation
- Sources: (1) replay of real sessions: extract every outbound call from stored transcripts and relabel by hand (target ≥ 300 calls across the four labels); (2) false-positive and miss labels from plan 01's store; (3) synthetic: seeded secrets from `agent/redact.py` fixtures and a PII generator, with negatives drawn from public docs.
- Metric: per-label precision and recall on the replay set; the gate is the `approve` rate on clean traffic.
- Promotion gate to `escalate`: `contains-secret` recall ≥ 0.95 and precision ≥ 0.90; `pii` precision ≥ 0.85 at recall ≥ 0.70; `ok`-labelled calls escalated ≤ 2% over 14 days of shadow; N ≥ 300 labelled cases with ≥ 30 per non-`ok` label. The numbers are starting values, to be re-set from P1 evidence (plan 01 does the same for Shield).
- Latency: stage 1 ≤ 20 ms; stage 2 p95 ≤ 2.5 s (the client timeout). Clean tool calls without an egress surface add nothing.

## Failure policy
- Timeout, no key, endpoint down, cooldown armed (`client.py:36`), or unusable reply → the call proceeds and the record says `unscreened`. No label is invented.
- Unknown label string from the service → `unscreened`, counted, never mapped to `ok`.
- Low confidence is not observable (labels only); a stage-2 `ok` is accepted as given.
- Redaction unavailable → `unscreened`, and the text is not sent to the classifier.
- The deterministic floor is fail-safe in the other direction: a scrub match always yields `contains-secret` even when the model is down.

## Host changes
None. Arg rewriting and an "allow" directive are not needed; both are out of scope.

## Phases
- **P0 — verify surfaces (read-only).** Confirm tool names and arg keys for `send_message`, `terminal`, `web_extract`, `browser_navigate`, MCP; confirm whether command text is redacted anywhere before dispatch. Accept: a table of tool → arg keys with file:line, committed in this plan.
- **P1 — shadow addon.** Stage 1 + stage 2, record only. Accept: loopback test sends a staged secret in `send_message` and the row is `contains-secret`, stage `deterministic`, with no secret bytes in the file.
- **P2 — replay and labels.** Replay CLI over relabelled transcripts; sweep. Accept: report with per-label precision/recall for N ≥ 300.
- **P3 — escalate.** Enable `approve` behind the gate in Data & evaluation. Accept: test where an `always` grant for `ok` does not authorise a secret send.

## Tests
Extend `tests/test_meta_classifier.py` with the real native loader and a schema-dispatching fake `urlopen` (`tests/conftest.py:27-30` blocks real sockets). Cases: each surface extracts the right text; `git push` is skipped (Shield owns it); deterministic floor without a model call; scrubber-unavailable → unscreened; shadow returns `None`; `escalate` returns `approve` with the rule key; payload never appears in the log.

## Risks & open questions
- `internal-only` is subjective; wrong defaults train users to click through. Mitigation: record-only until the user supplies a hint.
- Command-string parsing is shallow: `python -c "requests.post(...)"` and base64-wrapped payloads evade it. Stated ceiling; this is advisory, not a DLP boundary.
- Reading `@file` targets reads user files into a classifier request. Bounded to the workdir and scrubbed; the user must opt in via `egress_file_bytes > 0`.
- Overlap: a2a `redact_outbound` and gateway scrub already cover some paths. P0 must list them so the addon does not double-handle.
- Open: should `send_message` to the user's own home channel be exempt? Proposed: yes by default (`egress_exempt_home: true`).

## Non-goals
Rewriting or redacting the payload; model replies to chat platforms (gateway scrub owns them); git diff scanning (plan 01 Shield); blocking domains (plan 24 and native egress policy); a DLP guarantee.
