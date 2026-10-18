# 22 Memory-worthiness classifier
Status: proposed · Owner: — · Depends on: 01 meta-classifier (client), 16 memory-v2 (inbox), 17 mock-harness (soft)

## Goal
Memory writes are cheap and permanent. The agent writes a `memory` entry for a passing remark, a one-off path, or a pasted token, and the entry is then injected into every future turn. Plan 16 adds a typed inbox that captures candidates from transcripts, so the volume rises further. This plan adds a labels-only gate in front of both entry points: direct builtin `memory` writes and plan 16 inbox captures. The gate tags each candidate as `durable-fact`, `preference`, `procedure`, `ephemeral` or `secret`. The gate never edits content. It either lets the write proceed, holds it for review, or blocks it. `secret` is decided deterministically, never by the classifier, so a classifier outage cannot let a credential into memory.

## Evidence
- **Hermes builtin memory [source]**: the tool is named `memory` (`tools/memory_tool.py:263`) with actions `add|replace|remove` and a batch `operations` form (`:323-326`). Every write passes `_gate_or_stage` (`:104`, `:107`). That gate fails open when `tools.write_approval` cannot be imported (`:69-70`), and otherwise blocks or stages the write for approval (`:72-78`).
- **Hermes content scan [source]**: `tools/memory_tool_store.py:26` `_scan_memory_content` runs `first_threat_message(content, scope="strict")` (`:29`) on the write paths at `:242` (add), `:266` (replace) and `:331` (batch). `memory_tool.py:43-44` re-exports it. Load-time sanitize at `:117-124` skips `[BLOCKED:` entries.
- **Hermes scan patterns [source]**: `tools/threat_patterns.py:133` `first_threat_message` returns the first finding. `hardcoded_secret` (`:89`, scope `strict`) matches `(api[_-]?key|token|secret|password)` followed by a quoted 20+ character value. `read_secrets` (`:77`) matches `cat` on `.env`, `.netrc`, `.pgpass` and similar.
- **Duplicate scan copy [source]**: `tools/cronjob_prompt_scan.py:121` `_scan_cron_prompt` is a second strict scan with its own invisible-unicode pre-check (`:92`, `:110`). Any change to secret rules must be checked against both copies.
- **Hook surface [source]**: `pre_tool_call` is in `VALID_HOOKS` (`hermes_cli/plugins.py:111`). A `block` directive vetoes the call. A callback that times out fails closed with a block (`:1719-1722`, `plugins.hook_callback_timeout`, default 30 s). `post_tool_directive` (`:112-117`) is observer-only.
- **Provider hook [source]**: `on_memory_write(action, target, content, metadata)` is a `MemoryManager` method (`agent/memory_manager.py:720`), dispatched by `notify_memory_tool_write` (`:772`) after the builtin write. It is a provider method, not a plugin hook, and it runs after the write, so it cannot block.
- **Existing classifier [source]**: `plugins/meta-classifier/schemas.py:329-336` `MEMORY_SCHEMA` has head `durable` with `decision`, `preference`, `project_fact`, `none`. `addons.py:195-203` runs it in `post_llm_call` (read-only, untracked WIP, not edited here).
- **Plan 16 capture limits [source]**: `16-memory-v2.md:9` cites `v2_capture.rs:199-224` for type `user|feedback|project|reference`, statement ≤1024 B, body ≤8 KiB, ≤128 observations. The inbox is written by the capture pass and consolidated by Dream (`:10`).
- **Egress redaction [source]**: `agent/redact.py:807` `redact_for_egress` fails closed to `REDACTION_UNAVAILABLE`.

## Current state
- No memory-worthiness gate exists. A write is accepted if the strict regex scan passes and the approval gate allows it. There is no notion of "worth remembering".
- The regex scan catches only known secret shapes. A token in an unfamiliar format passes it.
- Plan 16 inbox capture has no gate; every typed observation reaches the inbox (`:8-9`).
- The meta-classifier `MEMORY_SCHEMA` (`schemas.py:329`) labels memory-worthy turns, but only as a read-only `post_llm_call` observer. It cannot block a write.
- No `VALID_HOOKS` entry is needed. `pre_tool_call` covers direct writes, and the plan 16 provider calls the gate directly during capture.

## Design
Plugin `plugins/memory-worthiness/` (`plugin.yaml` `manifest_version: 2`, `api_version: 1`, `provides_hooks: [pre_tool_call]`). It vendors the byte-identical `client.py` from plan 01 (no cross-plugin import). It exposes one function, `judge(content, target, origin) -> Verdict`, used by two entry points:
- **Entry A, direct writes.** `pre_tool_call` where `tool_name == "memory"`. Each `add`, `replace`, or batch op is judged before the builtin runs. Returns a `block` directive for `secret`, or for `ephemeral` when promoted. Otherwise returns nothing.
- **Entry B, plan 16 captures.** Plan 16's capture step calls `judge` on each candidate before the inbox append. Held candidates are written to a review queue, not dropped.

**Label set** (multi-label head `memory`; one label per candidate in practice):
- `durable-fact`: a stable fact about the project, environment or user that a future session would need. Pos: "the NFS mount breaks SQLite rollback journals". Neg: "I'll read config.py next".
- `preference`: a user instruction about how to work. Pos: "always use jj, not git, in this repo". Neg: "that looks fine".
- `procedure`: a reusable multi-step method. Pos: "to rebuild the wheel: `uv build`, then `twine check`". Neg: "the build failed once".
- `ephemeral`: a state, path or observation valid only for this session. Pos: "the build is currently red on job 12". Neg: "the repo root is `~/projects/hermes-plugins`".
- `secret`: a credential or key material. Never a classifier output (see Input contract). Pos: a `sk-…` key. Neg: "rotate the key every quarter".

**Mapping to existing schema.** `durable-fact` ↔ `project_fact` plus `decision`; `preference` ↔ `preference`; `ephemeral` ↔ `none`. `procedure` and `secret` have no equivalent in `MEMORY_SCHEMA`. This plugin vendors its own schema and does not reuse `durable`, because the meta-classifier is still WIP and its head set is not stable.

## Labels
Each label has a 1-line definition and positive and negative examples (see Design). The costly error is a `durable-fact` or `preference` candidate labelled `ephemeral` and held. Losing a real preference is worse than storing a passing remark, so `ephemeral` must not hold anything until its promotion gate passes (Data & evaluation).

## Input contract
- **Entry A hook:** `pre_tool_call`. Fields: `tool_name`, `args` (`action`, `target`, `content`, `old_text`, `operations`). Only `tool_name == "memory"` with `action` in `add|replace` or a batch is judged. `remove` is not judged, since removing an entry cannot add a bad one.
- **Entry B input:** the plan 16 candidate: `type`, `statement`, `body`, `topic_hint`. The gate judges `statement` plus `body`.
- **Truncation:** the classifier sees at most 4000 characters (`segment_chars`, as plan 21). Content longer than that is judged on its first 4000 characters only. The deterministic scan still covers all of it (`MAX_SCAN_CHARS`, `threat_patterns.py:17`).
- **Redaction before egress:** `agent.redact.redact_for_egress` runs on the text before it is sent (`redact.py:807`). If redaction changed the text, the candidate is `secret` by the deterministic path, and the classifier is not called. If redaction returns `REDACTION_UNAVAILABLE`, the candidate is held for review and not sent.
- **Why `secret` is not a classifier output:** the classifier must never receive a credential. Recall for secrets is therefore bounded by the redaction and threat-scan pattern sets. That is the honest ceiling, and the plan does not claim more.

## Decision wiring
- **Core scan first:** `_scan_memory_content` (`tools/memory_tool_store.py:26`) and the approval gate (`memory_tool.py:104`) run as today. The plugin runs before the builtin body (`pre_tool_call`), so its block precedes any staging.
- **Deterministic `secret`:** any redaction delta or strict scan hit yields `secret`. The write is blocked with a fixed message that does not echo the content.
- **Advise mode (`mode: advise`):** `secret` blocks. `ephemeral` holds nothing until promoted. Other labels proceed. A held candidate goes to the review queue, never the inbox.
- **Shadow mode (`mode: shadow`, default):** the plugin classifies, logs the label to `memory-worthiness.jsonl` (content-free: anchor hash, label, deadline outcome) and returns nothing. The write is byte-identical to today. The `secret` path is shadow-logged as well, but the core scan still blocks known shapes.
- **Fail policy on the direct path:** the plugin's own deadline is 2.5 s, well under the host's 30 s `pre_tool_call` timeout, so the plugin decides the outcome itself. On classifier failure, a non-secret direct write is held with a message ("memory write held: classifier unavailable; retry or review"). It is not silently allowed and not silently dropped.
- **`_gate_or_stage` fail-open is not inherited:** if `tools.write_approval` cannot be loaded, the core gate allows the write (`memory_tool.py:69-70`). The plugin's `secret` block does not depend on that module.
- **Post-write:** plan 16's provider implements `on_memory_write` (`memory_manager.py:720`) to record the outcome. It is not a gate.

## Data & evaluation
- **Sources.** (1) Human labels: `hermes memory-worthiness-label <candidate-id> <label>`, appended to `memory-worthiness-labels.jsonl` (content-free, anchor hash only, as plan 01). (2) Replay of past `memory` tool calls from real sessions, and the plan 16 inbox after a Dream pass. Dream's kept and dropped items are the best ground truth. (3) False-positive and false-negative reports per plan 02.
- **Metrics.** Per label precision and recall. The costly errors are `durable-fact` or `preference` held (recall on gold keep) and `secret` missed by the deterministic path (zero tolerance, measured on the redaction fixture set).
- **Promotion gates.** Each action is promoted on its own gate:
  - `secret` block: deterministic path only; zero false negatives on the secret fixture set; false-positive rate of redaction-delta blocks measured and below 2 % on real sessions.
  - `ephemeral` hold: ≥ 300 human-labelled candidates with ≥ 40 per label; `durable-fact`+`preference` recall ≥ 0.97 (never hold a durable item); `ephemeral` precision ≥ 0.90; ≥ 20 real sessions in shadow.
- **Latency budget.** Classifier deadline 2.5 s (plan 01 client default). Cooldown 45 s after a failure (`client.py:30-56`). Shadow must not add latency to the write path.

## Failure policy
- No key, endpoint, timeout, transport error, malformed body or unknown label: classifier output is absent. A non-secret direct write is held; a plan 16 candidate is queued for review. Nothing is written automatically on uncertainty.
- The classifier never produces `secret`. A credential is blocked by the deterministic path or not at all, never by a timeout or a guess.
- Low confidence: the service returns labels, not scores. The label is absent below `cls_threshold` (recalibrated by replay). An absent label means no verdict, and the candidate is held for review, not invented as `ephemeral`.
- Redaction unavailable: held, not sent.

## Host changes
- **None for Entry A.** `pre_tool_call` already vetoes with a directive (`plugins.py:111`, `:1719-1722`).
- **Entry B:** plan 16's capture step calls `judge` before the inbox append. This is a plan 16 change, not a new `VALID_HOOKS` entry.
- No new hook names. The set comment in `plugins.py` forbids inert entries, so none are added.

## Phases
- **P0 shadow measure.** Plugin, `judge`, shadow log, redaction and deterministic `secret` path. Accept: with the plugin enabled in shadow, a fixed session's memory writes are byte-identical to a run without it; a dead endpoint writes `classifier: unavailable` and the write proceeds unchanged in shadow.
- **P1 labels and eval.** Label CLI, replay over past `memory` calls, threshold sweep. Accept: a labelled fixture reproduces the documented precision and recall; stored rows contain no candidate text.
- **P2 advise, `secret` only.** Enable the `secret` block in advise mode. Accept: a planted `sk-…` token in an `add` is blocked with no echo of the value; the same write with the service down is still blocked.
- **P3 `ephemeral` hold.** Only after the promotion gate for `ephemeral` passes; numbers recorded in this file.

## Tests
- `tests/test_memory_worthiness.py`: loopback `/classify` stub, real `pre_tool_call` dispatch. Cases: `remove` is never judged; a batch op with one secret blocks the whole batch; a redaction delta produces `secret` and never calls the service; a `REDACTION_UNAVAILABLE` candidate is held and not sent; the classifier never returns `secret` even when the stub does; a timed-out classifier yields a hold for a non-secret write and a block for a secret; `mode: shadow` leaves the write unchanged.
- A test that removes the deterministic `secret` branch makes the planted-token fixture fail. The test is required, not optional.

## Risks & open questions
- **Dream bypass.** Plan 16 Dream consolidates the inbox into curated notes. An item that passes capture reaches curated notes with no second gate. Open: whether Dream must re-check `procedure` and `ephemeral` labels.
- **Redaction false positives.** A redaction-delta `secret` block fires on things that are not secrets. The 2 % gate is a guess until measured.
- **Held-write ergonomics.** Holding a write on classifier outage may frustrate the agent. Open: route holds through the core staged-approval queue (`_gate_or_stage` staging, `memory_tool.py:76`) rather than a plugin queue. The plugin cannot call that queue directly today.
- **Duplicate scan copies.** `memory_tool_store.py:26` and `cronjob_prompt_scan.py:121` both scan. Any new secret rule added to `threat_patterns.py` must be checked against both call sites.
- **Schema drift.** `MEMORY_SCHEMA` (`schemas.py:329`) is WIP. If it stabilises, the vendored schema here should be replaced by a shared one; until then the two can diverge.

## Non-goals
- No edit to `plugins/meta-classifier/` or its tests (untracked WIP, read-only).
- No change to memory content: the gate never rewrites, truncates or redacts what is stored.
- No change to the core threat-scan patterns or `_gate_or_stage` in this plan.
- No Dream consolidation policy (a plan 16 concern; see Risks).
