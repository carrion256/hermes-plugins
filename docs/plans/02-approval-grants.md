# 02 Approval grants: approval-as-data and permission options

Status: proposed · Owner: — · Depends on: none (uses existing `tools/approval*.py` gate and `pre_tool_call`/`post_approval_response` hooks)

## Goal
Give Hermes plugins a durable, auditable way to (a) escalate a tool call to a human or the smart guardian, (b) record the answer as a grant with explicit scope, TTL, and retirement, and (c) keep operator denylists and floors above every grant. Adopt two patterns from the references: Grok Bot's approval-as-data (answers bound to a request id, cards that expire) and Droid's permission option set (`proceed_always_file`, `proceed_report_false_positive`). Map both onto the existing `tools/approval*.py` gate. Do not build a parallel approval system.

## Evidence
Read in this pass (hermes-agent unless noted):
- `tools/approval.py:139-172` `resolve_gateway_approval`: FIFO pop by default; `request_id` targets one entry; `resolve_all` for `/approve all`; sets `entry.result`, optional `entry.reason` (the `/deny <reason>` text), calls `entry.event.set()`; returns the count resolved.
- `tools/approval.py:1001-1024` `request_tool_approval`: `rule_key` defaults to `tool_name:` plus a 12-char sha256 prefix of `reason`; `pattern_key=plugin_rule:{rule_key}`; `display_target=<tool> (plugin approval rule)`; `fail_closed_when_no_human=True`. Same human gate as Tier-2 dangerous shell patterns.
- `tools/approval.py:939-957` `_should_skip_container_guards` (skips dangerous-command prompts for isolated backends, except docker with host bind mounts); `_user_deny_block` enforces `approvals.deny` before the container fast path and is not bypassable by yolo or mode=off; `_floor_block` runs before yolo, mode=off, and cron approve-mode.
- `tools/approval_smart.py:68-100`: `_get_smart_policy` reads `approvals.smart_policy`; `_smart_approve` returns approve/deny/escalate; operator policy goes in the system prompt only, marked trusted, never adjacent to the untrusted `<command>` block; timeout via `_get_task_timeout("approval")`.
- `tools/approval_floors.py:23-53`: floor definitions (read earlier this session; not re-read in this pass).
- Hook names `pre_approval_request` and `post_approval_response` appear in `tools/approval*.py` (grep-confirmed). They are observers: the comment at `plugins.py:160-163` says approval observer returns are ignored and to use `pre_tool_call` instead. Payloads not yet read (P0).
- `hermes_cli/plugins.py:1881-1886` `_get_pre_tool_call_directive_details`: directives are `block` (veto; message becomes the tool result) or `approve` (escalates any tool to the human gate; `rule_key` selects the always-allowlist grain). The first valid block wins regardless of registration order; `approve` is honored only when no block exists. There is no `allow`.
- `plugins.py:2013-2025`: if `modify` changed args, the allowlist key appends `#exec:<sha256[:16]>`, so an always grant for the original command cannot authorize the modified one. `plugins.py:2028-2042`: an `approve` whose gate errors, denies, or times out is blocked. `model_tools.py:750-773`: `pre_tool_call` fires once per execution.
- Grok Bot `re/docs/agent-protocol.md:69-70`: `ResolveGrokBotLocalToolPermission` keyed by `request_id` with resolution ALLOW_ONCE=1 / DENY=2 / ALWAYS=3 / NEVER=4; `ResolveGrokBotAutoReviewApproval` keyed by `request_id` with APPROVED / DENIED / ALWAYS.
- Grok Bot `re/docs/agent-protocol.md:76`: `SAND_DEFAULT_LOCAL_TOOL_PERMISSION="ask"`, `SAND_LOCAL_TOOL_ASK_TTL_MS=600000`; an unanswered card expires after 10 min with `ask.status="expired"`. Default permission `needs_approval`.
- Grok Bot grant TTL: none documented. The `expires` grep of `agent-protocol.md` finds only the card expiry at `:76`. `[INFERENCE]` Grok Bot grants are not TTL-bounded; D3 uses Grok Build's TTLs only.
- Grok Build `re/docs/grok-build-platform.md:754-784` (§2.6.3, read this pass): a grant has scope Call/Session/Workspace (Global only when `unattended_allowed`), TTLs `[3600, 86400, 604800]` s capped at `MAX_PERSISTED_TTL_SECONDS` (7 d) per the card-ceiling note at `:781`; a grant buys exactly one re-dispatch (`ReplayBudget::One`, `:780`); no hub, transport error, cancel, or unrecordable grant leaves the denial standing (`:783`); card deadline is the hub backstop, about 10 min (`:781`).
- Droid `e2e/daemon.md:34` (note-sourced; `daemon.md:30-35` read this pass): `ToolConfirmationOutcome` = `proceed_once`, `proceed_always`, `proceed_always_file`, `proceed_report_false_positive`, `cancel`; result envelope `{selectedOption, comment?, sessionId}`. The source enum file was not located on disk, so this is note-sourced until P0 confirms it.
- Droid `apps/cli/src/agent/tool-confirmation.ts:100-200` and `:162-186` (read earlier): warn dialog for the report option. `command-hooks.ts:213-243, 281-287` (read earlier): detached report path.

Unverified and excluded from the design: Grok Bot per-recipient/per-app grant keying and machine-bound ids. These came from earlier notes that did not re-verify in this pass. Treat them as hypotheses until P0 confirms them.

## Current state
Hermes already has most of the mechanics:
- Human gate with once / session / always / deny choices, a gateway pending queue, and fail-closed timeout (`request_tool_approval`, `resolve_gateway_approval`).
- Plugin escalation path: a plugin `pre_tool_call` returns `{"action":"approve","rule_key":...}` to route to the same human gate (`plugins.py:1881-1886`); returning `block` vetoes; returning nothing proceeds.
- Smart guardian returning approve / deny / escalate (`approval_smart.py`).
- Operator denylist and floors that run before yolo and mode=off (`approval.py:939-957`).

Gaps (candidates; confirm each in P0, do not treat as facts yet):
- G1. No plugin-visible grant object with scope, TTL, retirement, and replay budget. "Always" is stored as a plain string in config `command_allowlist` (`approval.py:392-437`); it has no expiry, scope, or source fields. Verified.
- G2. No false-positive channel. Droid's `proceed_report_false_positive` has no Hermes equivalent: a repo-wide grep of `hermes-agent` for `false_positive` (2026-10-18) finds only unrelated test names and pentest-skill verdicts, nothing in the approval path. Verified.
- G3. The `rule_key` digest is truncated to 12 hex chars (`approval.py:1014`), which invites collisions for standing grants. Verified.
- G4. The smart guardian has no user allow/block lists as data; only `approvals.smart_policy` text. `[INFERENCE]`

## Design
D1. One gate, one ledger. Plugin approvals keep going through `request_tool_approval`. Grants are records consulted before the human gate, not copies of allowlist entries (no second source of truth). `post_approval_response` (observer) writes grants; the gate consults them after the terminal floors and `approvals.deny` (P2), never in `pre_tool_call`. The observer never gates.
D2. Grant record (plugin-owned JSON store under the plugin's state dir): `id` (full sha256 of tool + normalized args + scope key), `action` (tool name), `scope` (call | session | workspace | path), `subject` (rule_key or resolved path), `source` (human | smart | operator), `created_at`, `expires_at`, `replay_budget` (1 for call scope), `retired_at`, `retire_reason`.
D3. TTLs. Card (unanswered prompt) expires at 600 s, matching `SAND_LOCAL_TOOL_ASK_TTL_MS`. Standing grants offer 1 h / 1 d / 7 d, capped at 7 d (Grok Build's set).
D4. Retirement. A grant retires on: expiry; explicit revoke; policy-hash change (operator denylist or floors changed); replay budget exhausted (call scope). Retirement is an audit event, not a delete.
D5. Ordering. Floors, then `approvals.deny`, then grants, then smart guardian, then human. A grant can never allow a floor- or deny-blocked command, including under yolo and mode=off. Test this order explicitly.
D6. Droid option mapping. `proceed_once` = call grant. `proceed_always` = session grant. `proceed_always_file` = path-scoped workspace grant keyed by the resolved path. `proceed_report_false_positive` = no grant; it writes a false-positive label event consumed by plan 01's label store (separate label set from the WIP labels; do not merge them). `cancel` = deny.
D7. Answers bind to request ids. Gateway answers match by `request_id`; a stale id returns 0 and touches nothing (existing `resolve_gateway_approval` behavior; keep and test it).
D8. Smart guardian lists. `approvals.smart_allow` and `approvals.smart_block` (user-owned) are passed as data in the system prompt alongside `smart_policy`, never next to `<command>`. The allow list cannot bypass floors or deny; the block list can only tighten.

## Host changes
- H1 (required): neither hook payload carries `request_id`, `rule_key`, or `reason`. CLI path: `approval.py:843-848` (`command`, `description`, `pattern_key`, `pattern_keys`, `session_key`, `surface`; `choice` on post only). Gateway path: `approval_gateway_wait.py:121-127` builds the same keys, and `request_id` is only on the queued entry (`:32`, set by `setdefault`); the hook at `:151` and `:73` never sees it. Add `request_id`, `rule_key`, and `reason` to the payloads on both paths (observer only; it records grants and never decides).
- H2: none. `pre_tool_call` has no allow directive and returning nothing already proceeds, so a grant hit needs no host change. Do not add an allow return; no caller needs it.
- No change to floors, `approvals.deny`, or the human gate.

## Phases
P0. Verify contracts (read-only). Established: `pre_tool_call` has no allow directive (`block` or `approve` only; returning nothing proceeds). Established: CLI and gateway hook payloads lack `request_id`, `rule_key`, and `reason` (H1); the gateway queue entry has `request_id` (`approval_gateway_wait.py:32`). Established: `_pre_dispatch_guards` (`model_tools.py:750-773`) runs `pre_tool_call` at dispatch (`model_tools.py:926`), before the terminal handler. Established: the production terminal floor call is `terminal_tool.py:857` via `_check_all_guards` (`terminal_tool.py:146-151`), which runs after `pre_tool_call`. Established: `check_dangerous_command` (`approval.py:975-986`) orders floor, then yolo, then permanent allowlist, then pattern gate. Consequence: a grant-hit proceed in `pre_tool_call` would skip the terminal floors, so P2 must not return proceed for terminal calls; the grant may only suppress the human prompt inside the gate after floors. Permanent allowlist: a config string list with no expiry (`approval.py:392-437`).
P1. Grant store and TTL/retirement logic in the plugin, with a fake clock. No hooks yet.
P2. Wire grant lookup into the gate after the terminal floors and deny (not in `pre_tool_call`, which runs before the floors; see P0); enforce replay budget.
P3. Droid option mapping (D6) and the false-positive event to plan 01's label file.
P4. Smart allow/block lists (D8), placed as trusted system-prompt data.
P5. Gateway request-id binding check (D7) and operator docs for the new config keys.

## Tests
- Grant past `expires_at` is ignored and escalates to the gate (fake clock).
- Floor-blocked command with a live standing grant stays blocked; repeat with yolo on and mode=off.
- `approvals.deny` beats a grant.
- Call-scope grant: second use re-prompts (`ReplayBudget::One`).
- Policy-hash change retires all grants with reason recorded.
- `resolve_gateway_approval` with an unknown `request_id` returns 0 and leaves other queued entries untouched.
- `proceed_report_false_positive` writes a label and creates no grant.
- Smart prompt assembly: lists appear in the system block, never adjacent to `<command>`.
- A grant hit, checked in the gate after floors and deny, lets the call proceed; a miss returns `approve` carrying `rule_key` from `pre_tool_call`; a competing plugin `block` still wins over a grant hit.
- An exception in `post_approval_response` leaves no grant; the next identical call re-prompts.
- Args changed by `modify` do not match a grant made against the original args (args digest in grant id; `plugins.py:2013-2025`).
- One runnable check per non-trivial logic unit (the store and the ordering function), kept small.

## Risks
- R1. Two sources of truth for "always" (allowlist entries and plugin grants). Mitigation: D1, consult-only.
- R2. Truncated 12-hex `rule_key` collisions could cross-grant (confirmed at `approval.py:1014`, G3). Mitigation: full digest in the grant id.
- R3. Droid option names come from notes, not source; the enum path was not found. Re-verify against the binary or client schema before P3 ships.
- R4. Grok Build figures are grep excerpts; re-read `grok-build-platform.md:770-790` before citing them externally.
- R5. Replay semantics depend on how Hermes re-dispatches a call; if Hermes cannot replay, scope the budget to re-prompt only.
- R6. Overlap with plan 04 (tool steering) and plan 12 (computer-use guard), which may also touch pre-tool decisions. Reconcile with those plan owners before P2. Their contents were not read here.
- R7. `pre_tool_call` directive semantics. The first valid `block` wins regardless of registration order, and `approve` is honored only when no block exists (`plugins.py:1881-1886`, `:2028-2042`). Grants are consulted in the gate after the terminal floors and `approvals.deny` (P2), so a grant hit cannot skip a plugin `block` or a terminal floor. The design relies on that ordering.
- R8. Observer failure. `pre_approval_request` and `post_approval_response` cannot gate. If the observer fails to write a grant, no grant exists and the next identical call re-prompts. This is the fail-safe direction.

## Non-goals
- No smart-guardian auto-approve that skips floors or deny.
- No changes to Grok Bot's server-side approval flow or its RPCs.
- No UI work on approval prompts.
- No cross-plugin imports; the grant store is self-contained in this plugin.
- No edits to sibling plan files (01, 04, 08–13, 16, 17); reference by id only.
