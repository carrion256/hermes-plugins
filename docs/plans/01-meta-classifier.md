# 01 meta-classifier

Status: proposed · Owner: — · Depends on: none (no host seam; declared-risk uses the existing `register_tool(override=True)` consent path)

## Goal

Finish the untracked `plugins/meta-classifier/` WIP into a tracked plugin that is the single classifier-backed risk gate for Hermes. It covers:

- droid-style Execute risk tiers,
- Droid Shield secret triage on `git commit` / `git push`,
- opt-in local signals,
- the agent-facing `decision_model` tool,
- a replay CLI.

Add what the WIP lacks:

- droid's self-declared `summary` + `riskLevel` on the shell tool, measured against an independent classifier (calibration only, never enforcement);
- local false-positive / miss labels;
- replay over those labels, so thresholds are recalibrated from evidence rather than copied from droid.

Retire `auto-review-classifier`. `subagent-effort` and `auto-answer-classifier` keep their logic but adopt the same client code.

## Evidence

Droid (sourced from notes under `/var/lib/hermes/projects/droid/notes/`; source files not re-read unless marked):

- Six classifier decisions; only Shield changes behaviour by default (`arch/classifiers.md` §0).
- Execute risk, Jev: "the label is only observed and never enforced" (`classifiers.md:29`, citing `packages/common/src/feature-flags/constants.ts:2596-2603`, not re-read).
- Observation path `RiskClassificationService.ts:122-218` called fire-and-forget from `apps/cli/src/agent/tools.ts:1632-1644` (`classifiers.md:30`, not re-read).
- Jev runs only when the model supplied `riskLevel` (`classifiers.md:31`).
- Metric `COMMAND_RISK_CLASSIFICATION_OBSERVATION_COUNT {existingRiskLevel, independentRiskLevel, failureStage, autonomyLevel, model}`.
- Execute contract: `summary` (2–8 words) and `riskLevel {value: low|medium|high, reason}` required; missing or invalid means HIGH; denylisted commands forced HIGH (`DEEP-FINDINGS.md` §6, read lines 131-158).
- luna alternative: four tiers tied to autonomy levels (`classifiers.md` §2; `RiskClassificationService.ts:49-54`, `sonnetRiskClassifier.ts:102-204`, not re-read).
- Droid Shield thresholds: downgrade P(B) ≥ 0.3775 blocks, else warns; secret-named line without a finding gives risk P(B) ≥ 0.95 warn; downgrade failure blocks, risk failure is silent (`classifiers.md` §3).
- Warn dialog offers Proceed / **Report false positive** / Cancel (`apps/cli/src/agent/tool-confirmation.ts:162-186`, read). The report is detached and content-free (`apps/cli/src/tools/executors/client/shell/command-hooks.ts:213-243,281-287`, read 215-290). The option is withheld in Airgap Mode (notes).
- Local Signals parsing is lenient: garbage replies persist plausible defaults (`classifiers.md` §5-7). The WIP deliberately differs.
- Replay: `tooling/classifier-replay.mjs` (134 lines, read in full this session) builds 13 payloads: 2 Jev (`:45-48`; `expect` git-status→low, git-push→high), 1 luna (`:51-58`; `expect` riskLevel high at `:55`), 6 Shield (`:66-69`; one per line of the 6-line capture), 3 signals (`:72-106`), 1 router (`:111-113`). Dry run prints `{id, expect, textChars, schema}` (`:119-122`); verified 13 lines. `--run` POSTs `{text, schema}` to `$CLASSIFIER_URL/classify` via `fetch` with no timeout, retry, or cooldown (`:123-134`) and prints `x-classify-ms` (`:133`).

Grok Bot:

- Server-side shadow exists via gate `smart_mode_classifier_shadow_mode` and user `autoReviewInstructions` (`re/docs/local-exec-daemon.md` §5.1, line 354). Gate name only decoded; classifier is server-side `[inferred]`.

## Current state

WIP (untracked; verified this session):

- Files: `plugins/meta-classifier/__init__.py` (604 lines, `wc -l` this session), `client.py` (55), `schemas.py`, `replay.py`, `plugin.yaml`; test `tests/test_meta_classifier.py` (about 324 lines).
- `register(ctx)` at `__init__.py:379`.
- Shield `shield()` at `:401`; Execute gate `gate()` at `:451`; `decision_model()` at `:594`.
- Hooks: `pre_tool_call` → `gate` at `:522`; `pre_llm_call`, `on_session_end`, `on_session_finalize`, `on_session_reset` at `:588-591`.
- Tool `decision_model` registered at `:609-633`.
- CLI `meta-classifier-replay` at `:649-652`.
- Gate (`:451-520`): redacted, bounded `pendingCommand` plus transcript tail → one `/classify` call → tier compared with `approve_from` (`:490`). At or above threshold returns `{"action": "approve"|"block", "rule_key", "revalidate"}` (`:516-519`), with target-identity revalidation. Classifier failure returns metadata-only `risk_review{status:"unknown"}` (fail-open; `README.md:194`, not re-read).
- Shield (`:401-448`): a "finding" is an added line that native `redact_for_egress` changes; a "risk context" is a secret-named line unchanged by redaction. Thresholds apply as multi-label `cls_threshold` (`schemas.py:97-103`, read earlier), uncalibrated. Downgrade failure blocks; risk failure is silent.
- Signals (`:525-565`): off by default; JSONL under `$HERMES_HOME/logs`; unusable replies recorded as `error`.
- `client.py`: `Classifier.classify` with a 45 s dead-endpoint cooldown and no 429/503 handling.

Overlap and gaps:

- `auto-review-classifier` gates the same hook with a binary `{"risk": ["safe","risky"]}` head; its request is `urlopen` at `__init__.py:257-258` (10 s-class `_CLASSIFY_TIMEOUT`). Two enabled gates double-call the service.
- `subagent-effort` (`__init__.py:162`, 10 s timeout, no cooldown, no retry) and `auto-answer-classifier` (`__init__.py:90`, 2.5 s timeout, one retry on 429/503 at `:92-95`) each carry a private `urllib` client. `meta-classifier/client.py:47` is a third copy (2.5 s default, 65536-byte read cap, 45 s cooldown).
- No cross-plugin import path. Each plugin loads as its own module from its manifest; `tests/conftest.py:27-33` provides the loader fixture, and `:27-30` replaces `socket.socket.connect` with a guard.
- Hermes `terminal` has no `summary` or `riskLevel` parameter: `TERMINAL_SCHEMA` at `hermes-agent/tools/terminal_tool.py:1271-1312`, handler `_handle_terminal` at `:1375`. The self-declaration half of droid's calibration does not exist.
- Hermes approval observers see `once|session|always|deny|timeout|smart_*` with no "report false positive" choice (`hermes_cli/plugins.py:149-171` hook list; `register_approval_transport` at `:457`).
- The labels and calibration keys are absent from `plugin.yaml` `config_schema` `[inferred from WIP; not re-verified]`.

## Design

Plugin only. No host change.

Sibling relationship (decision):

| plugin | fate | how |
|---|---|---|
| `auto-review-classifier` | retired (matches `tool-fff` precedent, `README.md:18`) | `register` becomes a no-op that logs "retired: use meta-classifier"; `endpoint`, `api_key_env`, `policy` keys carry over; its test cases migrate into `tests/test_meta_classifier.py` |
| `subagent-effort`, `auto-answer-classifier` | kept | adopt a byte-identical vendored `client.py` (superset: cooldown, one bounded 429/503 retry, size limit); a parity test asserts equal SHA-256 across all three copies |
| meta-classifier | owns `decision_model` and replay | not a backend for siblings (no import path) |

Configuration (new keys, defaults):

- `risk_mode: enforce` — `enforce` (preserves auto-review behaviour on cutover) or `shadow` (classify and record; always return `None`).
- `declared_risk: off` — `off` or `shadow`; `shadow` requires the user's `allow_tool_override` consent for `terminal`.
- `labels: true`, `labels_path: ""`, `labels_max_rows: 5000`, `labels_store_payload: false`.
- `risk_log: true`.
- `shield_thresholds: {downgrade: 0.3775, risk: 0.95}` — droid's values as documented, uncalibrated defaults.

Declared-risk shadow:

- The plugin re-registers `terminal` with `override=True`. Its schema is the live `TERMINAL_SCHEMA` plus optional `summary` (string) and `risk_level` (`{value: low|medium|high, reason}`).
- The handler strips both keys and calls the native `_handle_terminal`.
- `pre_tool_call` reads the declaration from `args`, then removes `summary` and `risk_level` from the copy sent to the classifier. The classifier judges the command, not the description.
- A missing or invalid declaration is recorded as `declared: null`. It is never coerced to HIGH or any default. Droid's HIGH rule belongs to its enforcement, which we do not copy.
- Declared risk never alters the gate.

Calibration log:

- Path: `$HERMES_HOME/logs/meta-classifier-risk.jsonl`.
- Row: `{ts, session_id, tool_call_id, tool, scale, declared|null, independent|null, failure_stage|null, approval: {choice, decided_by}|null, outcome}`.
- Agreement is computed only for `risk_scale: jev`. luna rows record both raw values; any `off → low` mapping is `[inference]` and not applied.
- `hermes meta-classifier-calibration` prints a declared × independent confusion matrix, per-tool agreement, and unscreened / unavailable counts.

Approval outcome linkage:

- An observer on `post_approval_response` matches `pattern_keys` containing `meta-classifier:<tool>:<digest>` (the `rule_key` form emitted at `:517`).
- It stamps `approval` onto the row via a bounded in-memory digest → row map (at most 256 entries).
- The key form the host actually stores must be verified in P3 before relying on it.

Labels:

- Path: `$HERMES_HOME/logs/meta-classifier-labels.jsonl`. Append-only, content-free.
- Row: `{label_id, kind: risk|shield|signals, ref: {session_id, tool_call_id, location, window_sha256}, given, label: false_positive|false_negative|correct, source: human|implied, ts}`.
- `implied` rows come only from approval outcomes. A human `once|session|always` after a block-tier verdict is a candidate false positive; `deny` is a candidate true positive. They are reported separately and never used to move thresholds.
- `human` rows come from `/classifier-label <ref> false_positive|miss` (`register_command`) and `hermes meta-classifier-label`.
- Nothing is uploaded. Secrets are never stored: Shield refs hold `path:line` and a hash of the already-redacted window.
- Rotation: oldest rows dropped beyond `labels_max_rows`.

Replay:

- Existing dry run and `--run` remain (`:649-652`, `replay.dry_run` / `replay.run`).
- Add `--strict`: exit 1 when any `expect` yields CHECK or the service is unavailable.
- Add `--labels`: re-run labelled payloads only when `labels_store_payload: true`. The stored payload is the redacted state.
- Add precision / recall and a threshold sweep over the Shield heads. The sweep recommends values; applying them is a manual config edit.

Failure modes (explicit, none fabricated):

- No key, endpoint, transport error, or unusable reply: risk → `risk_review.status = unknown`; Shield downgrade → block; Shield risk → silence; signals → `error` row.
- Redaction unavailable: egress refused (existing `_egress`).
- Target or context changes during review: unscreened (`:486-487`).
- Label store unwritable: warning logged; gate unaffected.

## Host changes

None.

Considered and rejected:

- A host-side "report false positive" approval choice. It would require prompt and transport changes; `register_approval_transport` (`hermes_cli/plugins.py:457`) is the nearest surface. The `/classifier-label` path gives the same data without host change.
- A tool-schema extension seam for `summary` / `risk_level`. The consented `terminal` override suffices and is reversible by unloading the plugin.

## Phases

P0 — land the WIP.
- Track the directory; bump manifest to `0.2.0`; add README inventory row and cutover note.
- Convert `auto-review-classifier.register` to the retired no-op; port its scope tests.
- Accept: `PYTHONPATH=$HERMES_SOURCE pytest tests/test_meta_classifier.py -q` green; loading `meta-classifier` logs the risk-scale line; loading the retired plugin registers no hook.

P1 — shared client.
- Add one bounded retry on 429/503 (`Retry-After` ≤ 1 s) and a `None`-on-anything contract to `client.py`.
- Vendor the identical file into `subagent-effort` and `auto-answer-classifier`; replace their private clients.
- Accept: parity test (three SHA-256 equal); existing `test_subagent_effort_runtime.py` and `test_auto_answer_authority.py` unchanged and green.

P2 — declared-risk shadow and calibration.
- Implement the `terminal` override, risk log, and `meta-classifier-calibration`.
- Accept (loopback classifier test):
  (a) `summary` / `risk_level` absent from the transmitted state;
  (b) row written with declared + independent;
  (c) `declared: null` for a call without declaration;
  (d) native `terminal` behaviour and result unchanged;
  (e) override without `allow_tool_override` leaves the native tool and logs the missing key.

P3 — labels.
- `post_approval_response` linkage, `/classifier-label`, `hermes meta-classifier-label`, rotation.
- Accept: a block-tier approval answered `once` yields one `implied` row; the CLI adds a `human` row; the calibration command reports them separately; a Shield row contains no secret bytes (test greps the file for the staged secret).

P4 — replay and live qualification.
- `--strict`, `--labels`, threshold sweep.
- Accept: dry run lists 13 payloads (2 Jev + 1 luna + 6 Shield + 3 signals + 1 router) from `notes/captured/classifiers`; verified this session (shield capture = 6 lines; replay dry run = 13 lines).
- `--run --strict` against the live service is a separate manual qualification (README: backend qualification is separate). Its PASS/CHECK table is kept as evidence, not asserted in CI.

## Tests

Extend `tests/test_meta_classifier.py`. Use the real native loader and a schema-dispatching fake `urlopen`; no network (`tests/conftest.py:27-30` fails any real connect).

Cases:
- declared-risk shadow (P2 a–e);
- label linkage, rotation, redaction (P3);
- calibration CLI output;
- threshold override plumbing into `cls_threshold`;
- `risk_mode: shadow` never returns a directive;
- retired `auto-review-classifier` registers nothing.

New `tests/test_classifier_client_parity.py`:
- SHA-256 of the three `client.py` copies are equal;
- 429 then 200 returns the body once;
- 503 twice returns `None` and arms the cooldown;
- text longer than `MAX_TEXT` returns `None`.

## Risks & open questions

- The service exposes labels, not probabilities (`schemas.py:78-81`; `decision_model` note at `:605-606`). Droid's thresholds cannot transfer. Labels plus replay exist to recalibrate. Whether `cls_threshold` semantics match is unverified until the P4 live run.
- Overriding `terminal` couples us to its handler signature. Reading the live schema at registration limits drift, but a native refactor can still break the wrapper. The test pins the `_handle_terminal` call shape. Fallback: leave `declared_risk: off`.
- Roughly 2.5 s per-call latency on a LAN service sits on the tool path. The cooldown bounds a dead service. Declared-risk rows add no extra calls, since the gate already classifies.
- `implied` labels can encode user fatigue, not truth. They are never used for thresholds.
- Retiring `auto-review-classifier` changes the `rule_key` prefix (`auto-review:` → `meta-classifier:`). Existing `command_allowlist` entries under the old prefix stop matching. Harmless but must be stated in the cutover note.
- Shield "finding" is native redaction diff, not droid's scanner (`gitDiffScanner.ts`). Recall on bare `aws_access_key_id = …` lines relies on the risk head (`classifiers.md` §3).

## Non-goals

- Factory Router / `-m auto` (poor fit for a label head; `README` forbids choosing a model from classifier output).
- Uploading reports to any backend.
- Emulating Jev server behaviour.
- Automatic threshold adjustment.
- Any enforcement from declared risk.
- Making `subagent-effort` apply efforts.
