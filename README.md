# hermes-plugins

Coding-oriented Hermes plugins derived from [dsh-plugins](https://github.com/carrion256/dsh-plugins) and [oh-my-pi](https://github.com/can1357/oh-my-pi). The integration target is the installed Hermes plugin SDK, not an approximation of its hooks.

## Coding stack

| Plugin | Implemented behavior |
|---|---|
| `tool-fff` | Optional `search_files` override delegates directly to Hermes's native handler: task CWD/backend, credential filtering, redaction, structured results, pagination and result cap are preserved. No fff acceleration is claimed; the unsupported CLI integration was removed. Disabled override leaves native search untouched. |
| `rules` | Global/project YAML-frontmatter rules, agent/profile and path scopes, deterministic precedence, a discoverable rulebook, and one-time targeted guidance in the originating tool result. Never appends prose to executable arguments or file contents. |
| `keyword-steering` | Configured prose keywords supply turn-local context. Fenced/inline code, quoted text, paths and identifiers do not activate keywords. |
| `auto-answer-classifier` | Low-risk defaults are labelled `automatic_answers` in the original clarification result. Human responses are unchanged, each suggestion names its question, and approval/destructive questions remain human-owned. Multi-question defaults stay advisory: the native batch result cannot distinguish an explicit blank skip from a timeout. No injected user message or cross-session queue. |
| `auto-review-classifier` | Advisory risk screening uses native `pre_tool_call` block/approve directives. Mutating `process_manage` actions are screened; read-only polling is exempt. `ask` requests real human approval. Classifier outages remain deliberately fail-open: this is not an authorization boundary or a code-quality reviewer. |
| `loop-guard` | Repeated calls must return identical results before the next retry is blocked. Read-only polling is exempt and real final answers break tool streaks. Repeated final answers produce next-turn context, never a new parent/gateway task. All state is fenced to the native bound agent. |
| `continuation` | Native todo criteria → fresh verification evidence → independent native `/review` → actionable findings → fixes/re-verification → clean review on current evidence. Uses same-turn `pre_verify` directives, not queued user messages. |
| `prewalk` | Explicit parent arming and optional child prewalk; a real nonempty todo plan must precede successful edit/write handoff. The arming result contains the same-turn planning nudge. Native model switching rebuilds provider settings; no provider-ready request rewriting. Status/reset tools make the one-way handoff inspectable. |
| `subagent-effort` | **Shadow only.** Records classification, the child's actual model/provider/effort, supported-effort information, latency, token usage, native estimated cost, verification observations and repeated-mutation counts. Never changes an explicit or inherited effort choice. `/subagent_effort` displays bounded, persisted observations. |
| `covey-tasks` | One `covey_tasks` tool for durable finite worklists and fenced task ownership over the Covey CLI (explicit local DB and binary per profile). Native `todo_list` is left untouched; Covey completion is not native verification or review. See [Durable worklists](#durable-worklists-covey-tasks) and [setup](#covey-tasks-setup). |

The remaining adapters are included in the inventory but were **not repaired by this coding-stack change**:

| Plugin | Current scope / known qualification gap |
|---|---|
| `browser-bladebro` | CLI wrapper for see/act/state/vision. Positional argument translation and stderr handling still need repair; native binary behavior is not qualified. |
| `tool-docling` | Document conversion wrapper. Task-CWD handling and same-basename artifact collisions still need repair; this is not the DSH docling.rs implementation. |
| `papercuts` | Native friction-report CLI/tool wrapper. Quoted arguments, severity examples and option forwarding still need repair. |

### Completion is evidence-bound

`continuation` reuses Hermes's verification ledger, bounded verify-on-stop policy and independent review engine. It does not create a second reviewer framework.

- `agent.verify_on_stop: true` enables native verification. The workspace must be recognized as a coding project and the executed check must qualify as native verification evidence. For example, this host recognizes `python3 -m pytest`, but not an absolute interpreter path with the same arguments. A markerless scratch directory is not automatically a coding project.
- The reviewer receives the actual pre-LLM task/history excerpt, candidate response, changed paths and native verification evidence. This is labelled as a **partial capture**, not a fabricated full transcript.
- The native reviewer must return a final canonical JSON object: `{"review_verdict":"clean","findings":[]}`, `{"review_verdict":"findings","findings":["actionable finding"]}`, or `{"review_verdict":"unverified","findings":["missing evidence"]}`.
- A completed reviewer process alone is not a clean verdict. Missing/malformed verdicts, unavailable ledgers, pending work and findings keep completion unverified.
- Changed evidence or a new user task invalidates the prior review. Findings remain visible until a clean renewed review.
- `max_review_rounds` bounds independent reviews; native `agent.max_verify_nudges` bounds continuation. Exhaustion releases the turn with an explicit **Completion unverified** label, not a claim of success.
- Detached forks and review children cannot recursively start completion reviews. Native empty-response and trailing-intent recovery remain Hermes's responsibility.

### Plan before execution

Configure `plugins.entries.prewalk.settings.target_model` to a model served by the planning model's **same provider/endpoint**. Leave it empty to keep handoff inactive; status reports this explicitly.

Call `prewalk_arm`, create a nonempty native `todo_list`, then perform the first successful `patch` or `write_file`. The handoff uses Hermes's native `switch_model`. Failed/blocked/malformed writes and terminal commands do not trigger it. `prewalk_status` reports state; `prewalk_reset` explicitly permits re-arming. `child_prewalk` controls inherited child planning.

Hermes may defer these tools behind its native tool catalog. Use `tool_search`/`tool_describe`, then the advertised `tool_call` schema. The current schema is `{"calls":[{"name":"prewalk_arm","arguments":{}}]}`; a bare `tool_name` field is not valid.

### Scoped rules

Global directories come from `plugins.entries.rules.settings.rule_dirs` (default `~/.hermes/rules`). Project discovery walks from the session's own workspace through ancestor `.cursor/rules`, `.windsurf/rules` and `.claude/rules` directories. A nearer project rule overrides an ancestor/global rule with the same filename stem.

Rules are `.md` or `.mdc` with safe YAML frontmatter, for example:

```markdown
---
description: Python changes require behavioral verification
agents: [sisyphus, sisyphus-junior]
tools: [write_file, patch]
globs: ["src/*.py"]
---
Preserve the public contract and exercise the changed behavior before finishing.
```

`alwaysApply`, `enabled`, `description`, `agents`, `tools`, `globs` and `condition` are supported; legacy `applies: always` remains a rule-file dialect. Present scopes combine with AND semantics. Path-scoped rules never become unconditional prompt text. Invalid metadata is skipped rather than applied broadly. Tool guidance blocks once with a normal tool-error envelope; the model reads it and retries without modified arguments. Prompt budgets include complete entries and identify omitted rulebook entries for discovery.

### Effort calibration

Classification is observation, not evidence that a task was solved correctly. The shadow report distinguishes lifecycle completion from native verification observations. Repeated file mutations are a **proxy**, not measured rework. Per-child cost is captured from the live child's native accounting before close; `estimated`/`partial` provenance is retained, not represented as an actual invoice. Missing or unproven cost and unsupported provider capability tables stay `unknown`.

Collect representative real tasks and compare correctness/verification, repeated work, latency and cost before enabling any automatic effort policy. This implementation intentionally has **no apply mode**. Tests establish data-path correctness with a controlled classifier, not classifier accuracy or a performance win.

`evaluation/evaluate_shadow.py` provides a separate, explicit live evaluation. Without `--run`, it uses only the standard library to prove all six initial cases fail their public and held-out checks; it does not load credentials or call a model.

```sh
python evaluation/evaluate_shadow.py --output /tmp/shadow-baseline
# Use an environment containing both Hermes and pytest for the live run:
python evaluation/evaluate_shadow.py --run --output /tmp/shadow-live
```

The live runner reads only `CLASSIFIER_API_KEY` from `~/.hermes/.env`, passes it through a native-scrubbed auxiliary environment name, and isolates each child's Hermes home. It freezes the corpus/order and source hashes, preserves explicit `xhigh` effort for every child, and never gives the child the held-out oracle. Failed coding outcomes remain in the results; provider/runtime failures are recorded separately and prevent a complete evaluation. Rework means an observed failed native verification followed by a later source change—not merely repeated edits. Missing verification or monetary-cost evidence remains unknown.

The defaults target the qualified local Qwen endpoint; `--model`, `--base-url`, and `--env-file` select another explicitly configured route. The backend must support `xhigh`; the runner does not silently change effort. This is a six-case observational study, not a matched lower-versus-higher-effort experiment or a basis for enabling automatic routing.

#### Live observation: 2026-10-09

The frozen run used Winnow-12B `b6ac22b0d51b` for classification and the locally served `qwen3.8-flash-next` for coding. [Raw outcomes and source snapshots](evaluation/results/2026-10-09/results.json), [runtime/corpus provenance](evaluation/results/2026-10-09/manifest.json), and [independent replay/accounting checks](evaluation/results/2026-10-09/verification.json) are retained.

| Task | Classifier label | Held-out checks | Child latency | Input + output tokens | Verified corrective revisions |
|---|---|---|---:|---:|---:|
| Record-line parsing | simple | Passed | 7.05 s | 33,283 | 0 |
| Atomic ledger batches | complex | Passed | 12.21 s | 45,935 | 0 |
| Exact suffix removal | simple | Passed | 5.22 s | 24,685 | 0 |
| Per-key single-flight memo | complex | Passed | 147.97 s | 195,083 | 0 |
| Integer page count | simple | Passed | 7.02 s | 31,863 | 0 |
| CSV catalog and CLI | complex | Passed | 24.59 s | 78,504 | 0 |

Every child kept the fixed `xhigh` setting, and its public test stayed unchanged. Tokens are accumulated native usage across requests, not a monetary price. The local backend reported **unknown monetary cost for every case**. The memo task's repeated mutations targeted its scratch verification script, illustrating why that proxy must not be equated with production-code rework.

**Decision: automatic routing remains NOT QUALIFIED.** This small sample distinguishes observed task workloads, but it does not establish that lower effort would preserve correctness or save money. The native capability table also reported `unknown` for this route. The [native pilot](evaluation/results/2026-10-09/qualification.json) succeeded with `xhigh` and was rejected with `high`; the endpoint advertised `medium` and `low` as its other supported values, but those settings were not exercised. No gateway configuration or effort policy was changed by the evaluation.

### Durable worklists (`covey-tasks`)

One tool, `covey_tasks`, backed by the Covey CLI. It does not replace or modify native `todo_list`. There are two kinds of todos: native todos are the conversation's own execution steps (and still feed `prewalk` and `continuation`); Covey tasks are durable shared work that survives sessions and processes. Covey reads and writes never create native plan items or native verification evidence, and a Covey `finish` is not review, landing or native verification.

Actions (the full set; there is no update, delete or reopen): `status`, `lists`, `show`, `create`, `add`, `start`, `finish`, `retry`, `fail`, `release`, `abandon`. `lists` and worklist `show` page with `limit` (1–500, default 50) and `offset`; `show` takes exactly one of `meta_task_id` or `subtask_id`, and task `show` rejects pagination arguments.

Workflow:

1. `create(prompt)` a finite worklist, then `add(meta_task_id, title, priority)` **every known task before starting any**. Priority is an integer from 0 to 1000 (default 100), matching Covey's domain. A worklist closes when all its tasks are terminal; a completed worklist is history (add is refused), so create a new one.
2. `start(subtask_id)` claims and starts that exact task (one active claim per live agent). Do the work and run real checks with native tools.
3. `finish(subtask_id, summary[, evidence_digest])`, or `retry`/`fail` with `failure_code`, or `release`/`abandon`. A task whose persisted policy requires evidence is rejected without `evidence_digest`; governed tasks are outside this tool. State names are Covey's: closed is not all-success, so inspect `show` counts for failed/abandoned tasks.

Mutations return a non-secret `request_id`. An `outcome_unknown` result (e.g. timeout after a possible commit) is **not** a rollback: `show` the task, reconcile, then repeat explicitly with the same `request_id` if needed.

Ownership leases last 300 s. While, and only while, an agent holds a claim, a background thread heartbeats and renews every 60 s. Child stop, session reset/finalize and unload release or retire ownership within a bounded teardown deadline; after a crash the lease simply expires and a fresh claim (strictly newer fence) is required. An old owner cannot commit an outcome.

Limits: session tokens are cooperative coordination handles, not a security boundary. They pass in the Covey CLI argv and live in the DB, so a same-user process can read them. The plugin redacts `session_…` tokens from tool results and logs but cannot hide them from `ps` or the DB file.

## Installation and configuration

Use a Hermes Python environment with these native SDK modules available. This work was exercised against Hermes `b141dd1881499a98df0853d500e4d666c394a971`; no host/core patch is required.

Each directory in `plugins/` is independently installable. Install/link selected directories into the intended profile's `$HERMES_HOME/plugins/`, then enable them using that profile's normal plugin configuration. Do not silently install the unqualified adapters merely to enable the coding stack.

For an unmanaged, runtime-owned profile:

```sh
hermes config set agent.verify_on_stop true
hermes config get agent.verify_on_stop
hermes plugins enable continuation
hermes plugins enable prewalk
```

Target a named profile using the existing `hermes -p <profile>` mechanism, or the service's explicit `HERMES_HOME`. Managed installations must change the authoritative managed configuration instead. Persistent configuration is read by newly constructed agents; changing source/configuration does not hot-reload already imported plugin modules or restart an in-flight turn.

The optional native search override requires operator capability consent (`tools.override`, or the host's legacy `plugins.entries.tool-fff.allow_tool_override` setting) plus `override_search_files: true`. Without it, the native built-in remains available unchanged.

Classifier plugins read the environment variable named by `api_key_env` at call time. Never put credentials in plugin manifests. Their default endpoint is the existing local classifier service; absence/unavailability never fabricates a verdict.

### covey-tasks setup

Prerequisites: a Covey binary built from the Covey checkout that supports `meta list` and subtask finish, and a Hermes host with the bundled bridge (`covey`/lifecycle support) available. An older binary with the same version string is **not** acceptable: the plugin probes capabilities and refuses operations on a stale one, so an installed `covey` on `PATH` may be unusable. Build and point at an explicit path:

```sh
(cd /home/common/projects/covey && cargo build --locked --bin covey)
```

Install or link `plugins/covey-tasks` into the profile's `$HERMES_HOME/plugins/`, then configure exactly these two settings (no others exist; there is no ambient `COVEY_BIN`/legacy-config fallback and no default DB):

```yaml
plugins:
  entries:
    covey-tasks:
      settings:
        binary: /home/common/projects/covey/target/debug/covey   # explicit path
        db_path: /absolute/local/path/covey.db                   # absolute, no ".."
```

Enable per profile only, explicitly (`hermes -p <profile> plugins enable covey-tasks`). Native `todo_list` and the older `covey`/`todo` plugins are unchanged. Reads never create a missing database: `status` reports configuration/capabilities, while `lists` and `show` return `not_initialized`; only an explicit `create` creates it.

**Backup before first use.** A newer binary may migrate an existing DB when it opens it. Take a consistent copy with SQLite's backup API, not by copying the live file alone (WAL sidecars `-wal`/`-shm` would be missed or inconsistent):

```sh
sqlite3 /absolute/local/path/covey.db ".backup '/absolute/backup/covey-$(date +%F).db'"
```

**Rollback.** Remove or disable only `covey-tasks` for that profile (`hermes -p <profile> plugins disable covey-tasks`) and start a new session. Native todos and other plugins keep working. Owned claims release on normal unload; after a crash they recover when the lease expires. Never downgrade a migrated DB in place; restoring a verified backup is an operator action.

### Configuration cutover

- `tool-fff`: removed `binary`, `fff_search` and the unsupported fff CLI. Use native `search_files`; consumers retain its native arguments/result shape.
- `rules`: replace the old single `rules_dir` setting with `rule_dirs`. Remove any expectations of tool-argument rewriting.
- `continuation`: remove `max_steers_per_session`, `empty_stop` and `unexpected_stop`. Use `max_review_rounds` plus native verification/continuation settings.
- `subagent-effort`: remove obsolete `subagent_prefixes` and any experimental apply-mode setting. The plugin is shadow-only.
- No `register_goal_provider` SDK API is assumed. Middleware is declared with `provides_middleware` for the native validator; see the pinned-host loader/validator caveat below.

## Verification

Run in a Python environment containing the **real Hermes package and pytest**:

```sh
COVEY_TEST_BIN=/home/common/projects/covey/target/debug/covey python -m pytest tests -q
```

The suite loads plugins through Hermes's native manifest loader and manager in isolated profiles. It covers actual hook producers/consumers, parent/child identity, clarification ownership, scoped/remote search boundaries, generated `hermes_tools` consumers, model switching, verification freshness and durable review results. External classifier/model/backends are isolated; no production credentials or services are required.

`tests/test_workflow_cli.py` additionally launches the actual Hermes CLI against a loopback HTTP model boundary. The real run arms prewalk through the deferred-tool bridge, creates todos, writes code/tests, consumes scoped rules, switches models, runs pytest, records native evidence, obtains an independent native review and finishes. This is an integration proof, not a benchmark of model coding ability.

#### covey-tasks verification

Resolve the SDK interpreter from the installed Hermes wrapper (the Nix store path changes between builds), add pytest to a disposable target rather than the store or global Python, and build Covey first:

```sh
HERMES_PYTHON=/path/to/hermes-agent-env/bin/python3   # current SDK interpreter
TEST_DEPS=$(mktemp -d)
uv pip install --python "$HERMES_PYTHON" --target "$TEST_DEPS" pytest
(cd /home/common/projects/covey && cargo build --locked --bin covey)
export COVEY_TEST_BIN=/home/common/projects/covey/target/debug/covey
PYTHONPATH="$TEST_DEPS" "$HERMES_PYTHON" -m pytest tests -q
```

`COVEY_TEST_BIN` is a prerequisite only for `tests/test_covey_integration.py` and `tests/test_covey_cli.py`; those modules fail clearly without it rather than skipping. These tests use temporary databases and a loopback model boundary: no production DB, credentials or paid model call. Tests for other plugins need no Covey binary, e.g. `PYTHONPATH="$TEST_DEPS" "$HERMES_PYTHON" -m pytest tests/test_workflow_cli.py -q` or any single non-Covey module.

Manifest check, with a temporary home so no real profile is touched: `HERMES_HOME=$(mktemp -d) hermes plugins validate --json plugins/covey-tasks`.

Structural validation is useful but not a substitute for these checks:

```sh
for d in plugins/*/; do
  hermes plugins validate --json "$d"
done
```

On the pinned Hermes build, validation rejects `tool-fff`'s deliberately declared `search_files` override as a built-in collision even when runtime override consent is configured. The runtime accepts this supported override; the integration suite exercises it. The same host's loader also warns about `provides_middleware`, although its validator requires that declaration. These are host validator/loader inconsistencies, not reasons to conceal plugin capabilities or remove operator consent.

Native content search also does not guarantee cross-file ordering: `order` is ignored for content, and independent offset pages can overlap or omit matches when file traversal order changes. The adapter preserves that host contract rather than inventing stable pagination. Do not treat a set of cross-file offset pages as an exhaustive census; use bounded single-file searches when stable advancement is required.

## Original-feature boundaries

- `dsh-jump`: GUI navigation hints remain out of scope; Hermes lacks the required UI element API.
- `dsh-settings-panels`: use Hermes's native `config_schema` surfaces instead of a separate settings UI framework.
- `dsh-task-prewalk`: not a separate implemented plugin; child planning is part of `prewalk`.
- Mid-stream thinking/header-loop abort and retry still need a supported host cancellation seam; observers do not provide it.
- DSH's browser/fff/Docling integrations are not interchangeable with OMP's native browser/search/Markit implementations.

## Attribution

Derived from dsh-plugins by carrion256 (MIT), itself porting features from [oh-my-pi](https://github.com/can1357/oh-my-pi) (MIT © Mario Zechner, Can Boeluek, Stencil Labs). Each port inherits the upstream MIT grant.
