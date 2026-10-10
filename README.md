# hermes-plugins

Coding-oriented Hermes plugins derived from [dsh-plugins](https://github.com/carrion256/dsh-plugins) and [oh-my-pi](https://github.com/can1357/oh-my-pi). Plugins remain independently installable and reuse native todo, verification, review, MCP, Covey and papercuts stores.

**This upgrade requires the accompanying Hermes host changes.** The base revision `b141dd1881499a98df0853d500e4d666c394a971` alone is insufficient. Local implementation and qualification do not install plugins, restart services or change an active profile.

## Inventory

| Plugin | Implemented contract |
|---|---|
| `continuation` | Explicit requirement declarations and owned check observations bind native evidence IDs to source hashes, execution workspace and task. Independent review must assess coverage, not just notice a passing test. |
| `prewalk` | Only `prewalk_ready` performs the model handoff, after a native todo plan and evidence-grounded execution brief. Scratch writes do not signal readiness. Optional replanning is bounded and off by default. |
| `covey-tasks` | Native durable worklists, fenced claims, compact checkpoint/resume handoffs and native discovery diagnostics. Optional linked-worktree ownership guards prevent cooperative workers sharing one workspace. |
| `rules` | Scoped rule delivery plus `rules_explain`: applicability, provenance, precedence, explicit conflicts, omitted entries and check references. Guidance is never executable authorization. |
| `browser-bladebro` | Native MCP browsing and bounded, approval-aware runs. Local self-contained HTML scenarios can bind immutable artifact bytes and observed interactions to native verification evidence. Ordinary HTTP browsing is not build attestation. |
| `loop-guard` | Bounded structured-error, failed-verification and short-cycle recovery. Delivered interventions, exact next-call hypotheses and matching recovery are distinguished from suppression or exhausted budgets. |
| `auto-review-classifier` | Advisory screening of operation effects, resolved targets and missing context, with redacted egress and operation-specific native approvals. Selected risky approvals revalidate scope after consent. |
| `tool-fff` | **Retired.** Use native `search_files`; no override consent, extra search registration or claimed acceleration. |
| `tool-docling` | Content-addressed Python Docling conversion, source/converter provenance, selective retrieval and explicit missing extraction metadata. Publication is locked and atomic. |
| `subagent-effort` | Shadow telemetry and separate paired experiments. Explicit effort choices remain unchanged; automatic routing is **NOT_QUALIFIED**. |
| `auto-answer-classifier` | Evidence-supported ranking of all eligible conventional choices, scoped human-approved preferences, and per-question timeout provenance. Human answers and explicit skips remain authoritative. |
| `keyword-steering` | Explicit configured modes reference existing skills/rules, expire with the turn, expose status and withhold conflicting guidance. No semantic classifier or implied effort switch. |
| `papercuts` | Typed, redacted reports and native recurrence/triage. Opt-in authoritative tool-failure capture has a cross-process quota; the native binary continues owning the store. |

## Native prerequisites

The matching host source supplies these shared contracts; there is no substitute scheduler or evidence database in this repository:

- `agent.verification_evidence`: explicit row lookup, external evidence, source/artifact metadata, terminal evidence IDs, disabled-ledger refusal and a durable edit watermark. A later check cannot revive pre-edit evidence.
- `hermes_cli.plugins` / `plugins_dispatch`: all callbacks run; block wins over approval; delivery/suppression receipts use unique directive identities. `post_tool_call_settled` fires exactly once after every `post_tool_call` dispatch, including when bounded observers are suppressed or time out, so call-observation reservations settle without being wedged forever. Modified operations are presented at approval. A selected internal `revalidate(final_args)` must return exactly `True` after consent or the operation is vetoed.
- Native final-output transforms compose rather than overwrite each other's warnings; the original streamed text remains available for reconciliation.
- `tools.clarify_tool`: each question reports `answered`, `skipped` or `timed_out` with its `qid`.
- `PluginContext.call_mcp(..., replay_on_failure=False)`: trusted out-of-band no-replay policy and a host-minted token identifying the exact MCP connection. Remote result fields are not connection authority.

Install the complete corresponding host patch before enabling consumers, then construct new agents. Updating source or configuration does not hot-reload already imported modules. The implementation checkout used for qualification is `/home/common/projects/hermes-agent`; the installed host and active profiles were not modified.

### Installation

Install/link each selected directory containing `plugin.yaml` into the intended profile's `$HERMES_HOME/plugins/`, then use that profile's normal enable/configuration commands. For an unmanaged profile **after installing the required host**:

```sh
hermes config set agent.verify_on_stop true
hermes plugins enable continuation
hermes plugins enable prewalk
```

Use `hermes -p <profile>` or an explicit `HERMES_HOME`. Managed installations must change their authoritative managed configuration instead. Classifier keys come from the environment named by `api_key_env`, never from manifests. Missing services/credentials do not fabricate verdicts.

## Requirement-bound completion

1. Create native todos for the actual requested outcomes.
2. Use `acceptance declare` before verification. Each criterion identifies its observable, check kind/identity, relevant source paths and optional references. Match native todo IDs; every required completed todo needs a declaration.
3. Run the declared check through native tool execution. Bind its actual `verification_evidence.id` with `acceptance bind`. Terminal command targets and execution cwd must match; a passing sibling package or unrelated lint run cannot substitute.
4. Inspect `acceptance status`. Blocked/cancelled dispositions remain explicit; cancellation is not completion.
5. Independent native `/review` receives the actual todo content/structure, criterion mapping, source/evidence identity and a labelled partial task/history capture. It must challenge relevance and return coverage for every criterion:

```json
{"review_verdict":"clean","findings":[],"coverage":{"criterion-id":"covered"}}
```

Other verdicts are `findings` and `unverified`; coverage entries may be `gap` or `cancelled`. A finished reviewer process, malformed response, missing declaration or absent evidence is not a clean result. Source, task, todo or evidence changes invalidate prior review, including changes while a synchronous reviewer runs. Native compaction preserves same-agent declarations without accepting foreign-session observations.

Native `agent.verify_on_stop` must be enabled, the cwd must be a recognized coding project, and terminal commands must qualify for the native ledger. For example, this host recognizes `python3 -m pytest`; an absolute interpreter path is not necessarily recognized as the same check. Browser evidence requires an actually observed owned `browser_capture`, not a caller-supplied row alone.

`max_review_rounds` and native `agent.max_verify_nudges` bound intervention. Exhaustion releases the turn with **Completion unverified**, not a fabricated success. Review children/detached forks cannot recursively trigger reviews.

Terminal `check_identity` is the **exact raw executed command**, including its launcher, selectors and options; native heuristic/full-suite classification cannot broaden it. Declarations pin the execution workspace, so moving to another task cwd requires redeclaration and fresh checks. Review mapping must fit the native 12,000-character message limit (including its prefix); overflow remains unverified rather than truncating away requirements.

## Explicit planning handoff

Set `plugins.entries.prewalk.settings.target_model` to an execution model served by the planner's **same provider/endpoint**. Empty means inactive. Call `prewalk_arm`, populate native todos, then call `prewalk_ready` with:

- Located workspace-relative files and relevant symbols/callers. Files must be confined regular files; no callers requires a concrete `callers_absence_reason`.
- Constraints, non-goals, chosen approach and rejected alternatives.
- Falsification checks, replanning triggers and acceptance IDs; optional rule references.

The tool validates the brief and invokes native `switch_model`; it does not claim to prove that every model-authored explanation is correct. `prewalk_status` and `prewalk_reset` expose state. `replan_enabled` defaults to false; when enabled, `prewalk_replan` allows one return to the original model with a concrete failed assumption/check.

These tools may be deferred behind Hermes's native catalog. Discover the actual schema via `tool_search`/`tool_describe`; the bridge uses `{"calls":[{"name":"prewalk_arm","arguments":{}}]}`, not a bare `tool_name`.

## Recoverable Covey work

`covey_tasks` keeps durable shared work separate from native conversation todos. Its actions are `status`, `lists`, `show`, `create`, `add`, `start`, `finish`, `retry`, `fail`, `release`, `abandon`, `checkpoint`, `resume`, `candidates`, `availability` and `stuck`. Native diagnostics explain eligibility; this plugin does not schedule work. `finish` remains direct completion, **not governed review/apply, landing or verification**.

Configure an explicit capable binary and absolute local DB:

```yaml
plugins:
  entries:
    covey-tasks:
      settings:
        binary: /home/common/projects/covey/target/debug/covey
        db_path: /absolute/local/path/covey.db
```

A version string alone is insufficient: capability probes reject stale binaries. Build from the qualified Covey checkout with `cargo build --locked --bin covey` if needed. There is no ambient `COVEY_BIN`/legacy-config fallback or default DB. Reads do not initialize a missing database; explicit `create` does.

Create a finite worklist and add known tasks before starting. `start(subtask_id)` claims one task per live agent. Optional `workspace` must be a real linked Git worktree, not the main checkout. A lock in the resolved Git metadata directory reserves that workspace across processes. Native file/patch/terminal/process mutation guards enforce the live claim and task cwd. Release restores previous cwd state; a successful subsequent unbound start clears the obsolete workspace guard. This is **cooperative isolation, not a hostile-shell sandbox**.

Change an agent's DB configuration only while its tool and claim operations are quiescent, preferably in a new session. Sequential DB selection restores cwd without transferring or releasing another DB's claims. Concurrent hot-switching for the same agent is not invocation-scoped cwd isolation; fixed-configuration parallel agents remain separately owned.

`checkpoint` atomically saves a compact `covey-handoff.v1` receipt and retries/releases the task. Its handoff contains objective, changes, verification references, failed approaches and remaining work; workspace identity is attached by the runtime. `resume` obtains a fresh claim and returns `validated_handoff`: structurally checked recovery context, **not renewed proof that old checks remain valid**. Reverify current source. Native evidence requirements apply to checkpoint as well as finish.

Mutations expose a non-secret `request_id`. `outcome_unknown` is not rollback: inspect the native task, reconcile, and explicitly reuse the same request ID if retrying. Leases last 300 seconds and renew every 60 seconds only while a claim exists. Normal teardown releases ownership; after process loss, the lease expires and a newer fence is required.

Session tokens are cooperative handles, visible to same-user processes through CLI argv and the DB; redaction does not make them a security boundary. Back up an existing DB with SQLite's backup API before migration, not a copy that omits WAL sidecars. Disable the plugin/start a new session to roll back integration; never downgrade a migrated DB in place.

## Rules and operating modes

Rules use `rule_dirs` (default `~/.hermes/rules`) plus ancestor `.cursor/rules`, `.windsurf/rules` and `.claude/rules` directories. Nearer project rules override same-name ancestors/global rules. Existing scopes combine with AND semantics; path-specific guidance never becomes unconditional prompt text.

```markdown
---
id: python-contract
description: Exercise changed Python behavior
agents: [sisyphus]
tools: [write_file, patch]
globs: ["src/*.py"]
check_refs: ["tests/test_contract.py"]
constraints:
  api_compatibility: preserve
---
Preserve the public contract and exercise the changed behavior.
```

`rules_explain` exposes matching reasons/sources, precedence, explicit contradictory constraint values, check/verification references and budget omissions. Conflicts are declared metadata/content conflicts, not semantic understanding of arbitrary prose. References are data: neither prose nor check references execute commands or grant permission.

`keyword-steering.settings.modes` maps each mode to `refs`, optional `triggers` and `conflicts_with`. References point to your existing skills/rules rather than copied prompts. Genuine prose triggers activate only for the current user turn; code, quoted text, paths and identifiers are excluded. Conflicting modes are both withheld, and `steering_status` explains requested/active/withheld state. No keyword changes model effort or permits deployment. Legacy `keywords` prompt-copy configuration is rejected rather than silently reinterpreted.

## Failure-aware recovery and risk screening

`loop-guard` records bounded exact call/result history, structured error identities and native failed checks. It detects repeated failures and short cycles without guessing that similar-looking edits are equivalent. Read-only polling is excluded. A blocker retains the failing operation: succeeding at an unrelated check does not clear it.

`loop_recovery status` shows the blocker/history/budget. A hypothesis names one exact next operation intended to distinguish explanations; it never bypasses native approval. Only delivered interventions spend the budget; suppressed/diagnostic directives do not. Exhaustion labels the final answer with the unresolved blocker. Mid-stream model cancellation is still outside this plugin's capabilities.

`auto-review-classifier` screens actual resolved targets/workspace, effects and unavailable context. Recognized credential arguments, flat assignments and nested secret-valued metadata are masked before native redaction and HTTP egress; unsupported nested or unparsed credential-bearing commands refuse screening before HTTP. This is bounded recognition, not a guarantee of detecting every arbitrary secret. Unsafe redaction or unavailable context is reported as unknown/un-screened, not safe. Approval scope includes the operation and target identity; retargeting after consent fails revalidation. This is an **advisory screen**: classifier failure remains deliberately fail-open, while deterministic host permissions and native approval remain authoritative.

`auto-answer-classifier` provides `clarification_defaults recommend`, `record` and `list`. Recommendation uses explicit bounded repository evidence and one all-choice ranking; it never treats option zero as truth. Supported ordinary inputs are unambiguous repo-wide `.editorconfig` settings and supported top-level TOML/JSON scalar conventions. Conditional/conflicting/unset values, ambiguous domains and secret-bearing evidence cause abstention. File hashes and ownership are checked again after ranking and before application.

A recorded convention requires an observed native human choice and a separate native **remember** consent. Only these approved scoped conventions can avoid the prompt outright. Repository recommendations stay advisory until a genuine eligible timeout; each derived answer retains provenance without overwriting human responses. Explicit skips, material choices, destructive decisions and approvals remain human-owned. `dry_run` never applies a default. Legacy batch results lacking per-row provenance remain advisory only.

## Browser acceptance

Configure the native MCP server and explicitly grant its name in `plugins.entries.browser-bladebro.mcp_allowlist`; `settings.server` defaults to `bladebro`. Access is denied by default. The old CLI/opaque argument wrapper is gone.

Tools are `browser_open`, `browser_close`, `browser_run`, `browser_scenario`, `browser_capture` and `browser_screenshot` (renamed to avoid colliding with the host's built-in `browser_vision`; the plugin's version is Bladebro-backed and supplementary). Ordinary browsing remains available for HTTP sites, including authenticated surfaces, but login walls and inaccessible state are not success. Mutating nested runs retain native approval, bounded branching/iteration, atomic target selection/URL guarding and no automatic replay after an uncertain outcome. Host connection identity fences tab ownership across reconnects.

For certifiable local scenarios, declare sources, a confined self-contained HTML artifact, initial/final predicates and the intended DOM interaction **before** execution. Direct HTML must itself be declared source. Generated artifacts require a real foreground native build and its passing owned evidence row. Capture transports exact producer-hashed bytes with a restrictive CSP into a fresh owned tab, then observes the declared sequence through native MCP. Source/artifact drift, missing/truncated framing, failed guards, missing connection identity or uncertain dispatch cannot mint passing evidence. The external ledger records both source revision and transported artifact digest; continuation independently checks the capture observation and binds the actual evidence ID.

**Qualification boundary:** this is local self-contained-artifact proof, not attestation of arbitrary HTTP deployments, origin-dependent applications, multi-file dependency graphs or reproducible toolchains. The restrictive CSP is evidence invalidation, not a browser sandbox: page script can attempt side effects and the final URL guard invalidates evidence rather than preventing them. A byte-identical rebuilt artifact is reported `newly_generated=False`; qualifying a second build requires genuinely fresh output. A stale/degraded scenario is terminal and refuses rebuild — use a fresh scenario. Backend DOM/keyboard/accessibility/error observations must be described according to what was actually returned; text success and screenshots alone do not establish general accessibility or visual correctness. Unsupported predicates remain unverified rather than becoming model-declared passes.

## Traceable documents and friction reports

`docling_convert` uses **Python Docling**, qualified with CLI 2.137.0, not docling.rs. Set `tool-docling.settings.binary` to the intended executable. The source is resolved through native task cwd and sensitive-file policy. Identity includes source contents, input suffix (which can select a different parser), converter executable/version and options; source/converter drift refuses publication. Per-key locking, unique staging and atomic markers prevent partial or concurrent cache corruption. Format-unbound older cache entries are retained on disk but reconverted, not silently reused.

Converter identity retains the complete bounded version report, including reported Docling component versions, rather than its platform footer. A reported package upgrade invalidates cache entries even if the Python launcher is unchanged. This is version-report provenance, not a hash of every transitive dependency. New artifacts use cache-owned `document.md`/`document.json` names distinct from the `artifact.json` marker; cache hits return the adopted marker's filenames.

Results expose source hashes, Markdown/structured JSON artifacts, available `self_ref`/parent/page/section/table provenance and bounded page/section selection. HTML genuinely lacks page provenance: it is reported missing, never synthesized. Cache hits avoid reconversion but still validate identity. Failed conversions—including exit-zero runs without a valid artifact pair—do not overwrite good artifacts. Extracted document contents remain **untrusted source material**, not instructions or authorization.

`papercuts` uses typed fields for category/severity, expected versus observed behavior, version/invocation/result context, reproduction and references. Native deduplication owns recurrence counts and statuses; list/stats/ack/resolve remain native operations. Every string is redacted before filing. Automatic capture defaults off and uses authoritative host failures/delivered loop blocks, not arbitrary text containing “error”; its 20/day quota is updated atomically only after a successful filing. It does not publish externally.

Optional `time_lost_seconds` records **reported observed loss**, not an inferred duration. Native deduplication accumulates the known subtotal and `timed_report_count`; untimed reports remain unknown/partial. `list` with `sort: impact` ranks the complete backlog by severity, recurrence, known reported loss, timing coverage and stable ID before applying `limit`. An observed zero is distinct from missing timing. This is a transparent priority order, not a weighted score or monetary estimate.

Concurrent writers additionally require the **patched native papercuts binary**, not merely the plugin quota lock. The isolated source snapshot is `/home/common/projects/papercuts-hermes-integration`, with sealed `preimage/` files and `lock.patch`. Set `papercuts.settings.binary` explicitly to that snapshot's `papercuts` when evaluating it. Its report/dedup/ack/resolve read-modify-write transactions share a POSIX file lock. Non-POSIX locking failure is explicit. The installed `/home/common/projects/papercuts/papercuts` was deliberately left unchanged; installing the native patch is a separate operator action.

## Paired effort experiments

`subagent-effort` remains shadow-only. Classification, lifecycle completion, actual wire controls, native verification, measured repair cycles and independent deterministic oracle results are separate observations. Missing prices are unknown, not zero. There is no automatic apply/escalation policy.

```sh
# Offline baseline/oracle checks; no model calls:
python evaluation/evaluate_paired.py --output /tmp/paired-baseline
# Explicit live run in an environment with Hermes and required case dependencies:
python evaluation/evaluate_paired.py --run --efforts medium,xhigh --repeats 2 --seed 20261010 --output /tmp/paired-live
```

Each pair starts from identical source, uses the same numeric seed, and has isolated workspace/HOME/HERMES_HOME. Seeds vary across repeats; arm order alternates within each case. The corpus carries six frozen cases (suffix removal, page count, JSONL record parsing, CSV catalog aggregation, cross-file memo state, ledger batches) plus four additive cases (YAML service config, SQLite migration, path-security asset fetch, slug control). The v4 run used a six-case plan; the current design plans all ten (40 arms per two-effort, two-repeat run). Public and hidden oracles have reference-positive and broken/mutant-negative controls. The runner retains failures, timeouts, sources, transmitted controls, cumulative execution/repair usage and deterministic assessment cost; it does **not** claim an independent model code review or known monetary total.

The 2026-10-10 v4 run attempted 24 arms. One timeout pair and one mixed-effort-transmission pair were excluded, leaving 10 eligible comparisons. A broken path-security oracle and incomplete schema-rollback oracle were found during review; original artifacts remain unchanged. `evaluation/reassess_paired_v4.py` replays corrected oracles against hash-verified frozen candidates, in a separate output directory, without new model calls. These results are explicitly **retrospective**, not a preregistered rerun:

| Eligible-arm observation | medium | xhigh |
|---|---:|---:|
| Correct under corrected deterministic oracles | 10/10 | 10/10 |
| Cumulative native execution seconds | 305.16 | 405.19 |
| Native input + output tokens | 445,385 | 596,349 |
| Native API calls | 65 | 79 |

Lilith transmitted `chat_template_kwargs.reasoning_effort`, budget fields 8192/65536 and scaffold flags false/true. The client profile sets `enable_thinking: true`; an executed sandboxed render of the deployed tokenizer's hash-matched template confirms that medium and xhigh produce distinct prompt prefixes. The same render proves that budget/scaffold fields do not change this template; their intended SGLang behavior is **not qualified on the deployed vLLM fork**. Runtime reasoning-budget enforcement remains unknown. Earlier v3 missing-effort observations were an instrumentation omission, not evidence of unsupported transmission. Durable receipts are under `/home/common/projects/hermes-plugins-qualification/2026-10-10/evidence`: `effort-template-support.json`, `paired-v4-closure/` and the unchanged `paired-live*` attempts. The closure receipt verifies all 24 arm provenance chains and 136 unchanged historical files. These small-sample descriptive totals establish neither general quality equivalence nor monetary savings. **Automatic routing remains NOT_QUALIFIED.**

The earlier fixed-xhigh six-case observation remains frozen in [evaluation/results/2026-10-09](evaluation/results/2026-10-09). It is historical evidence, not a paired effort comparison.

## Configuration cutover

- Remove `tool-fff` installations/settings and any search override consent; native search retains backend isolation, credential filtering and bounded results. Native cross-file content offset paging is not an exhaustive/stable census.
- Replace `rules_dir` with `rule_dirs`; use rule/check references, not argument rewriting.
- Replace first-write prewalk assumptions with explicit `prewalk_ready` and its complete handoff.
- Add explicit acceptance declarations and binding. Old clean review snapshots without requirement coverage are not completion authority.
- Migrate steering `keywords` to `modes` referencing existing guidance.
- Use typed browser/papercuts operations; old free-form CLI argument strings are not supported.
- Remove obsolete continuation steering settings and experimental effort apply settings. No `register_goal_provider` API is assumed.

## Verification

Run with the patched Hermes source and a Python environment containing pytest and the case dependency PyYAML. Do not alter the Nix store or a live profile:

```sh
export HERMES_SOURCE=/home/common/projects/hermes-agent
export COVEY_TEST_BIN=/home/common/projects/covey/target/debug/covey
export PAPERCUTS_BINARY=/home/common/projects/papercuts-hermes-integration/papercuts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERMES_SOURCE${PYTHONPATH:+:$PYTHONPATH}" python -m pytest -p no:cacheprovider tests -q
```

The suite uses real native loaders/dispatchers and disposable profiles/databases, isolating external network/model boundaries. The actual Hermes CLI workflows exercise explicit planning, edits, failed verification, loop intervention/recovery, native evidence binding and independent review through a loopback model boundary. This proves integration, not model coding skill. Covey modules require the explicit capable binary rather than silently skipping.

Real backend qualification is separate: native MCP browser scenarios, actual Docling conversion, native papercuts subprocesses/concurrent writers, live classifier protocol and retained paired-model experiments. Unit-test doubles do not establish backend compatibility. The matching host's verification/arbitration/output/clarify/MCP tests must also pass after installing its source changes.

Validate every remaining plugin manifest in a disposable Hermes home with the patched host. The native loader warns about `provides_middleware` although the native validator requires it; retain the declaration rather than conceal middleware. This known loader/validator inconsistency does not grant capabilities.

## Original-feature boundaries

- `dsh-jump` GUI navigation and separate settings panels are not ported; use native configuration surfaces where available.
- Child prewalk is part of `prewalk`, not a second plugin.
- True mid-stream model cancellation still requires a supported host cancellation seam.
- DSH browser/search/Docling backends are not interchangeable with OMP's native implementations.

## Attribution

Derived from dsh-plugins by carrion256 (MIT), itself porting features from [oh-my-pi](https://github.com/can1357/oh-my-pi) (MIT © Mario Zechner, Can Boeluek, Stencil Labs). Each port inherits the upstream MIT grant.
