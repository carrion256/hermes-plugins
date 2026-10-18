# 21 compaction-keepset-classifier
Status: proposed · Owner: — · Depends on: plan 01 (shared `/classify` client, replay, label conventions); plan 06 (summary path; prefire fingerprint must include the keep-set digest); host seams H21a/H21b below

## Goal
When Hermes compacts a conversation, the summariser decides alone what survives. A user constraint stated in turn 4 ("never touch `generated/`") competes with 300 lines of `ls` output for the same summary budget. This plan adds a labels-only classifier that tags each compressible transcript segment as `decision`, `constraint`, `user-preference`, `dead-end` or `noise`. The tags become a keep-set: an advisory hint block appended to the summary prompt, so the summary spends its budget on what must survive. It starts in shadow, measures whether the existing summary already preserves keep-labelled segments, and only advises after a promotion gate. It never deletes or rewrites transcript content; the classifier can add emphasis, never remove material.

## Evidence
- **Grok Build [source/decoded]**: the summariser is one prompt (`re/data/prompts/grok-build/compaction-structured-summary.md:3-19`) emitting a single `<summary>` with 9 numbered sections, every heading mandatory (`grok-build-harness.md:717`): Primary Request and Intent, Key Technical Concepts, Files and Code Sections, Errors and Fixes, Problem Solving, All User Messages, Pending Tasks, Current Work, Optional Next Step. Section 6 says "List ALL messages from the user that are not tool results" (`:14`); section 1 says to "Preserve nuance and any constraints, scope boundaries, or stated preferences" (`:9`). The strategy is exhaustiveness by template, not selection. No classifier exists in that path.
- **Grok Build, out-of-band memory [source]**: compaction also writes `compaction/segment_NNN.md` plus `compaction/INDEX.md` (`grok-build-harness.md:723`), so discarded detail stays retrievable. Hermes has no such store.
- **Grok Build, carry-forward [source]**: a prior summary is "authoritative for the early history" and carried forward (`compaction-structured-summary.md:5`). Same idea as Hermes `_previous_summary`.
- **droid [observed]**: no compaction classifier. Compaction is a tool-less summary call (`DEEP-FINDINGS.md:124-127`, via plan 06 Evidence).
- **Grok Bot [unknown]**: summariser prompts are server-side (`grok-build-harness.md:874`). Nothing to copy.
- **Classifier pattern [source]**: label heads with one-line definitions sent to a `/classify` service (`plugins/meta-classifier/schemas.py:329-336`, `MEMORY_SCHEMA`). Failure to `None` and a 45 s cooldown (`client.py:30-56`).

## Current state
- Hermes summary template is 11 literal `##` headings in `agent/context_compressor.py:3432-3472`, plus two constant headings `HISTORICAL_TASK_HEADING` (`:3429`) and `_PRUNED_SKILLS_SECTION_HEADING` (`:3475`): Historical Task Snapshot, Goal, Constraints & Preferences, Completed Actions, Active State, Blocked, Key Decisions, Errors & Fixes, Resolved Questions, Relevant Files, Critical Context. The template is byte-pinned (`:1617` comment), so this plan never edits it. Constraints and decisions have a slot but nothing tells the model which turns hold them.
- Prompt assembly: `_build_summary_prompt` (`:3352-3407`). Provider context enters only as the sanitized `memory_context` block (`_memory_provider_section`, `:1585`). `focus_topic` is appended last (`:3401-3406`) and reweights to 60-70 % of the budget.
- Window selection in `compress()` (`:4626`): prune old tool results (`:4656`), `_compress_window` (`:4664`), `turns_to_summarize` after `_scan_window_handoffs` strips already-summarised handoff rows (`:4678-4679`), then `_summarize_window` (`:4701`). Tool groups are not split (`_align_boundary_backward`, `:3938`).
- Existing pre-compress seam: memory provider `on_pre_compress` (`agent/conversation_compression.py:2610-2647`). Only one external provider can be active (`plan 16`, `memory_manager.py:283`), and it returns a string, not segment labels. It is not a plugin hook.
- `VALID_HOOKS` (`hermes_cli/plugins.py:110-221`) has no compaction hook and no post-compress hook (confirmed by reading the full set; plan 06 reached the same conclusion).
- **Gap**: no component judges what is worth keeping, and nothing measures what a summary lost.

## Design
Plugin `plugins/compaction-keepset/` (`plugin.yaml` `manifest_version: 2`, `api_version: 1`, `provides_hooks: [pre_compress_window, post_compress_window]`) plus host seams H21a/H21b. It vendors the byte-identical `client.py` from plan 01 P1 (no cross-plugin import path).

**Segments.** The host supplies them (so indices agree with what the summariser sees): consecutive messages from one user message up to the next, tool call and result pairs never split, handoff/summary carriers excluded. Text per segment: role-tagged, reasoning blocks and images dropped, tool output clipped to head 2500 + tail 1500 chars with a declared `[...clipped N chars...]` marker.

**Which segments are classified.** Largest first, only those ≥ 400 chars, at most `max_segments` (64). Everything else is `keep` by rule, because small and user-authored segments are cheap and high-value. A segment carrying a user message is never eligible for a `noise` hint, whatever the classifier says.

**Labels.** One multi-label head `keep` (see Labels). Rule: any non-`noise` label means keep. `noise` only counts when it is the sole label. Precedence when several labels are returned: `constraint` > `user-preference` > `decision` > `dead-end` (strongest instruction wins in the hint).

**Hint block (host-built, additive).** The plugin returns only `{"keep_hints": [{"segment": id, "label": str}]}`. The host validates ids and labels against a fixed enum, caps at 256 hints, and builds the prompt text itself from each segment's first 80 redacted characters as the anchor:
```
KEEP-SET HINTS (advisory; the turns above remain the source of truth):
- segment starting "never touch generated/ ..." [constraint]: keep the user's wording
- segment starting "pytest: ImportError ..." [dead-end]: one line on what was tried and why it failed
- segment starting "total 4096 drwxr-xr ..." [noise]: at most one line
```
Because the plugin cannot supply prompt text, a plugin or a poisoned classifier response cannot inject instructions into the summariser. The block is placed after the turns and before `focus_topic`, so `focus_topic` still takes precedence (`:3401`).

**Mapping to sections** (how the labels meet the pinned templates):

| label | Hermes section (`context_compressor.py`) | Grok Build section (`compaction-structured-summary.md`) |
|---|---|---|
| constraint | Constraints & Preferences (`:3435`) | 1 Primary Request and Intent (`:9`) |
| user-preference | Constraints & Preferences | 1 and 6 All User Messages (`:9`, `:14`) |
| decision | Key Decisions (`:3458`) | 5 Problem Solving (`:13`) |
| dead-end | Errors & Fixes (`:3461`), Blocked | 4 Errors and Fixes (`:12`) |
| noise | folded into one Completed Actions line | no equivalent (Grok keeps every file and user message) |

**Survival log (shadow, P0).** After summarisation, H21b receives the summary text. For every keep-labelled segment the plugin extracts deterministic anchors (file paths, quoted strings, numbers, `-flag` tokens, error class names) and records `{segment, label, anchors_total, anchors_in_summary}`. This measures how much the current summariser already keeps. No model judgment is involved.

**Config** (`config_schema`): `endpoint`, `api_key_env` (as other classifiers), `mode: shadow` (`off|shadow|advise`), `max_segments: 64`, `min_chars: 400`, `segment_chars: 4000`, `workers: 4`, `timeout_s: 2.5`, `deadline_s: 3`, `log_path: $HERMES_HOME/logs/compaction-keepset.jsonl`, `log_text: false`.

**State.** Append-only JSONL, one row per compaction: `{ts, session_id, window, n_segments, n_classified, labels_hist, deadline_hit, survival[], digest}`. No segment text unless `log_text: true`; then only the redacted anchor snippet.

**Failure modes.** See Failure policy.

## Labels
Multi-label head `keep`; a segment may carry several labels.
- `decision`: a design, tooling or process choice with its reason that later work must honour. Pos: "keep WAL mode; the NFS mount breaks rollback journals". Neg: "I'll read config.py next".
- `constraint`: a limit or requirement from the user or environment that later work must respect. Pos: "do not touch `generated/`; CI diffs it". Neg: "ok thanks".
- `user-preference`: a lasting stylistic or workflow preference stated by the user (user-authored only). Pos: "always show me the diff, not the whole file". Neg: assistant text "I prefer pytest" (assistant-authored, not a user preference).
- `dead-end`: an approach tried, abandoned or proven not to work, with the evidence, so it is not retried. Pos: "pinned lib 2.0, still `ImportError: cannot import name X`; reverted". Neg: a passing `git status` listing.
- `noise`: bulk output or chatter with nothing later work depends on. Pos: 400 lines of `ls -R` never referenced again. Neg: a tool result whose one failing test name is used in the next turn.

## Input contract
- Hook: **H21a** `pre_compress_window` (new `VALID_HOOKS` entry), fired inside `ContextCompressor.compress()` after `turns_to_summarize` is final (`context_compressor.py:4679-4682`) and before `_summarize_window` (`:4701`). Kwargs: `session_id`, `trigger` (`auto|manual`), `focus_topic`, `segments` (`[{id, start, end}]`), `messages` (the window, read-only copies).
- Per call to the service: one segment, `{text, schema}` with text ≤ `segment_chars` (4000) and always under `MAX_TEXT` (100000).
- Redaction: `agent.redact.redact_for_egress` per segment (`agent/redact.py:807`; fails closed to `REDACTION_UNAVAILABLE`). On `REDACTION_UNAVAILABLE` the segment is not sent and is treated as unclassified (keep).
- Privacy: hook kwargs carry unredacted message text, like other plugin hooks. The plugin must redact before any egress or log.

## Decision wiring
- Consumer: `ContextCompressor._build_summary_prompt` (`context_compressor.py:3352`). H21a's validated hints reach it through a new `keep_hints` argument threaded from `compress()` (`:4626`) to `_summarize_window` (`:4701`). No existing plugin or core code consumes labels today.
- Shadow (`mode: shadow`): the plugin classifies in a background thread, writes the log, and returns `None`. The host ignores hook returns while `compression.keep_hints` is `off` (default). Compaction output is byte-identical to today.
- Advise (`mode: advise` and `compression.keep_hints: on`): the host appends the hint block. Classification runs concurrently under `deadline_s`; segments unfinished at the deadline are unclassified (keep). Promotion needs the gate below.
- Plan 06: the prefire cache fingerprint must include the keep-set digest, or a cached summary built without hints would be reused with hints on. A hint-induced prompt change is a cache miss, not a bug.

## Data & evaluation
- **Sources.** (1) Human labels on real compaction windows: `hermes compaction-keepset-label <compaction-id> <segment> <label>`, appended to `compaction-keepset-labels.jsonl` (content-free, anchor hash only, same convention as plan 01 labels). (2) Replay of real sessions that compacted at least twice: the second compaction's `_previous_summary` (`context_compressor.py:3373`) shows what the first one dropped. (3) Implied misses: a user restating an earlier constraint after compaction is a candidate `constraint` miss. These are `source: implied` rows, reported separately and never used to set thresholds (plan 01 rule). (4) Synthetic windows with planted constraints for regression only.
- **Metrics.** Per label precision and recall; the costly error is a keep segment labelled `noise`, so keep-recall is primary. Survival rate from the shadow log. A/B on replay: same window summarised with and without hints, anchors scored deterministically.
- **Promotion gate** (all required): ≥ 300 human-labelled segments with ≥ 40 per label; keep-recall (any non-noise label recovered for gold keep segments) ≥ 0.97; precision of `noise` ≥ 0.90; A/B anchor survival for gold keep segments ≥ +5 points absolute with no label class regressing; summary length within +10 %; ≥ 20 real compactions in shadow.
- **Latency budget.** Per segment ≤ 2.5 s (client timeout). Whole window ≤ `deadline_s` = 3 s in advise mode; partial results are used. Shadow never blocks compaction.

## Failure policy
- No key, endpoint, timeout, transport error, malformed body, unknown label or empty label set: that segment is **unclassified**. Unclassified means keep, never `noise`, and is logged as unclassified. No verdict is invented.
- Low confidence: the service exposes labels, not probabilities (`plan 01` Risks), so confidence is expressed through `multi_label` and a `cls_threshold` per head, recalibrated by replay. Below threshold the label is absent; a segment with no label is unclassified.
- Dead service: 45 s cooldown from the shared client; the window proceeds without hints.
- Hook exception or host-side validation failure: hints dropped, compaction unchanged, one warning per compaction.
- Redaction unavailable: segment not sent.

## Host changes
- **H21a** `pre_compress_window(session_id, trigger, focus_topic, segments, messages) -> None | {"keep_hints": [...]}`; fire-site `agent/context_compressor.py` inside `compress()` between `:4682` and `:4698`. Host validates ids, labels and count, and builds prompt text itself. No existing hook fits: `on_pre_compress` returns a provider string and only one provider is active; nothing else sees the segmented window.
- **H21b** `post_compress_window(session_id, window, summary_text, hint_digest)`, observer, fired after `:4711` (also on the fallback summary path, `:4709`). Plan 06 deliberately added no post-compress hook; this one is observer-only and only needed for the survival log.
- Config `compression.keep_hints: off|on` (default `off`) gates whether returns are honoured.
- Add both names to `VALID_HOOKS` (`hermes_cli/plugins.py:110-221`) with fire-sites in the same change.

## Phases
- **P0 shadow measure.** Seams, plugin, survival log, `mode: shadow`. Accept: with `compression.keep_hints: off` compaction output is byte-identical on a fixed transcript; a loopback service run writes one log row per compaction with `labels_hist` and `survival`; a dead endpoint writes `n_classified: 0` and compaction still succeeds.
- **P1 labels.** CLI and label store, replay command reusing plan 01 replay, threshold sweep. Accept: a labelled fixture reproduces the documented precision/recall; stored rows contain no segment text.
- **P2 advise.** Enable hints behind the gate. Accept: gate numbers recorded in this file; with hints on, a planted constraint in turn 4 appears in the summary in an A/B replay; with the service down, output equals the no-hint output.
- **P3 prefire interlock.** Digest in the plan 06 fingerprint. Accept: a cached summary built without hints is discarded when hints become non-empty.

## Tests
- `tests/test_compaction_keepset.py`: native loader, loopback `/classify`, real `ContextCompressor` with a fake summary callable. Cases: segmentation never splits a tool pair; user segments never get a `noise` hint; unknown ids and labels are dropped by the host; plugin cannot inject text (a hostile `reason` field never reaches the prompt); unclassified means keep; deadline returns partial hints; `keep_hints: off` leaves the prompt unchanged byte for byte; survival log counts anchors correctly on a fixture summary; no segment text in the log by default.
- Tests also assert that removing the `noise`-only-when-sole rule makes the "decision plus noise" fixture fail.

## Risks & open questions
- Hint wording can shift summary style. The A/B length cap is a gate, but style drift is only observable by review.
- Anchors are the first 80 redacted chars; two segments with the same prefix produce an ambiguous hint. Open: add a short ordinal ("2nd of 3 starting with ...") if replay shows collisions.
- The service returns labels, not scores, so `cls_threshold` semantics are unverified until the first live run (`plan 01` Risks).
- 3 s of classification on the compaction path is a latency regression unless prefire (plan 06) hides it. Open: run the classifier from the prefire thread in P2.
- Implied-miss labels can reflect user forgetfulness, not summariser loss, so they are advisory.
- H21b contradicts plan 06's "no post hook" choice. The seam is observer-only; flagged for upstream review.

## Non-goals
- No deletion, reordering or rewriting of transcript messages; hints affect only the summary prompt.
- No change to `_SECTION_INSTRUCTIONS`, `_summary_template_sections` or the summary model.
- No segment store like Grok's `compaction/segment_NNN.md` (a separate plan).
- No keep-set for tool-result pruning (plan 05), and no use of labels to choose a model.
- No memory writes from labels (plan 22 owns capture gating).
