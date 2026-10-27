# hermes-plugins

Coding-oriented Hermes plugins derived from [dsh-plugins](https://github.com/carrion256/dsh-plugins) and [oh-my-pi](https://github.com/can1357/oh-my-pi). Plugins remain independently installable and reuse native todo, verification, review, MCP, Covey and papercuts stores.

**This upgrade requires the accompanying Hermes host changes.** The base revision `b141dd1881499a98df0853d500e4d666c394a971` alone is insufficient. Local implementation and qualification do not install plugins, restart services or change an active profile.

## Inventory

| Plugin | Implemented contract |
|---|---|
| `continuation` | Explicit requirement declarations and owned check observations bind native evidence IDs to source hashes, execution workspace and task. Independent review runs asynchronously and releases the turn while pending; its binding and verdict still gate the next completion. Reviewer coverage applies to declared criteria, not undeclared todo bookkeeping. |
| `prewalk` | Only `prewalk_ready` performs the model handoff, after a native todo plan and evidence-grounded execution brief. Core located context is required; falsification/replan/acceptance lists are optional. Scratch writes do not signal readiness. Optional replanning is bounded and off by default. |
| `covey-tasks` | Native durable worklists, fenced claims, compact checkpoint/resume handoffs and native discovery diagnostics. Optional linked-worktree ownership guards prevent cooperative workers sharing one workspace. |
| `rules` | Scoped rule delivery plus `rules_explain`: applicability, provenance, precedence, explicit conflicts, omitted entries and check references. Guidance is never executable authorization. |
| `browser-bladebro` | Drop-in bladebro backend for Hermes' built-in `browser_*` tools (same names, schemas and results; needs `allow_tool_override`). Also provides `browser_run` batches and certified local HTML scenarios, which bind immutable artifact bytes and observed interactions to native verification evidence. Ordinary HTTP browsing is not build attestation. Tabs are per task. `on_session_finalize`/`on_session_reset` close a session's tabs and clear its state, and `subagent_stop` closes a child's tabs. Per-turn `on_session_end` is deliberately not used, so tabs survive across turns. |
| `loop-guard` | Bounded structured-error, failed-verification and short-cycle recovery. Delivered interventions, exact next-call hypotheses and matching recovery are distinguished from suppression or exhausted budgets. |
| `auto-review-classifier` | Advisory screening of operation effects, resolved targets and missing context, with redacted egress and operation-specific native approvals. Selected risky approvals revalidate scope after consent. |
| `meta-classifier` | Fork of `auto-review-classifier` merged with droid 0.244.0's reverse-engineered classifier decisions: Jev LOW/MEDIUM/HIGH or luna off/low/medium/high execute-risk tiers with redacted egress and operation-bound approvals, Droid Shield secret triage before `git commit`/`git push` (fail-closed like droid), Factory-Judge local-signals analytics (opt-in JSONL), an agent-facing `decision_model` tool and `hermes meta-classifier-replay`. `browser_decision` ports browser-use/jev-ultrafast's action space: one classifier request returns an operation (click/type/select/scroll_*/wait/done/blocked) plus the compatible element index (and option index for dropdowns) from an observed table only — a suggestion the caller executes and verifies itself. Nine opt-in addons (injection fence, command outcome/secret scrub, completion nudge, subagent report quality, request shape, per-failure friction, memory candidates, test value, delegation complexity) are metadata-first and off by default. Run it instead of, not alongside, `auto-review-classifier`. |
| `tool-fff` | **Retired.** Use native `search_files`; no override consent, extra search registration or claimed acceleration. |
| `tool-docling` | Content-addressed Python Docling conversion, source/converter provenance, selective retrieval and explicit missing extraction metadata. Publication is locked and atomic. |
| `subagent-effort` | Shadow telemetry and separate paired experiments. Explicit effort choices remain unchanged; automatic routing is **NOT_QUALIFIED**. |
| `auto-answer-classifier` | Evidence-supported ranking of all eligible conventional choices, scoped human-approved preferences, and per-question timeout provenance. Human answers and explicit skips remain authoritative. |
| `keyword-steering` | Built-in OMP-compatible magic keywords (`ultrathink` raises the turn's reasoning effort to `ultra`, `orchestrate` and `workflow` steer delegation and staged workflows) plus explicit configured modes referencing existing skills/rules. All are deterministic, expire with the turn, expose status and withhold conflicting guidance. Common-word keywords need a `/`/`#` sigil unless `bare: true`; negated or common-word hits are declined, optionally tie-broken by the classifier (ambiguous hits only, redacted, fail-open to decline); mode refs are resolved at registration. Only these explicit keywords move effort. |
| `model-routing` | Named roles (`model` + optional `provider`, `fallback: inherit\|refuse`) route delegate `model_role`, the prewalk executor and review owners to configured models — explicit pins always win, implicit lookups never refuse. `route_status`/`route_apply` expose and materialize pins; cross-provider targets are supported. Nothing picks a model from classifier output, cost or quota. |
| `papercuts` | Typed, redacted reports and native recurrence/triage. Opt-in authoritative tool-failure capture has a cross-process quota; the native binary continues owning the store. |
| `fast-compaction` | Lossless-by-deletion compaction overlay (port of tamaratran/fast-jev-compaction): registers as the ContextEngine but subclasses the built-in compressor and only intercepts `compress()`. Unpinned tool calls/results are paired, the whole conversation is fitted into a bounded state, and the classifier service answers two keep/drop heads per call in batches of 16 calls (the service's 32-head limit); stale pairs are dropped or head-truncated with a one-line note, everything kept stays verbatim, user/assistant text is never rewritten. Below `min_reduction`, a malformed answer, an unreachable endpoint, un-redactable state or an unfittable history, the call falls through to the untouched built-in summary path — the overlay never removes the fallback. Optional `proactive_prune_mode` mounts the host's mid-session seam (`turn_preflight`): `fifo` evicts oldest unpinned tool pairs with zero network cost, `classifier` runs the keep/drop pass first and degrades to fifo per-turn; the host's trigger, reclaim floor and regrowth-runway gates stay authoritative either way. `fast_compaction_status` reports attempts, prunes, per-reason fallbacks and proactive tallies. |
| `chain-compaction` | Ordered compaction cascade: runs configured stages (`prune` — reuses the fast-compaction classifier pruner; `clip` — head+tail truncation of oversized tool results; `drop_stale` — positional removal of old tool pairs) in sequence, advancing to the next whenever a stage fails, crashes or leaves the cumulative reduction below `target_reduction`, and committing verbatim output the moment the reduction is substantial enough. The built-in summary is always the final stage and cannot be removed; a whole-pipeline miss falls through to it untouched. Optional `proactive_prune_mode` (off/fifo/classifier, same contract as fast-compaction) mounts the host's mid-session eviction seam. `chain_compaction_status` reports the configured order, per-stage miss tallies and the last trace. Register exactly one context engine (host-enforced single slot): `fast-compaction` for classifier-only pruning, `chain-compaction` for the cascade — never both. |

### Throughput posture

These plugins optimize for completed work per unit of agent time. Clean paths add no prompts or ceremony; classifiers and telemetry stay off launch-critical paths; rule guidance rides successful tool results instead of forcing retries; asynchronous review releases the turn while preserving the binding; optional planning fields do not block a valid handoff; external adapters cache or reuse repeated identity/observation work. Hard stops remain only for concrete evidence, authorization, credential, or mutation-safety risks.

### Deliberate differences from OMP

These plugins intentionally narrow OMP's behavior wherever implicit magic would undermine evidence, custody or explicit choice. If you are coming from OMP, expect these differences:

| Area | OMP behavior | Here | What to expect |
|---|---|---|---|
| Prewalk handoff | The planning model can hand off implicitly (for example on the first write). | Only `prewalk_ready` switches models, and only with a native todo plan plus explicit located files, symbols/callers, constraints, chosen approach and acceptance IDs. The execution target may be an explicit `target_model`/`target_provider` or the routed `executor` role, on the same or another provider. | No implicit model switches. A plan that cannot point at real context stays on the planning model; scratch writes never count as readiness. A cross-provider target is validated at arm time and refuses to arm if unresolvable. |
| Completion | A turn completes when the model stops. | `continuation` holds coding turns in `pre_verify` until the native todo ledger is clear, verification evidence is fresh, and an independent review closed on the exact reviewed change; declared acceptance criteria must bind real native evidence. | Deliberately more demanding, slower completion: "Completion unverified" until the evidence exists. A blocked or cancelled disposition is never completion. |
| Magic keywords | Typed trigger phrases silently change behavior (modes, effort). | Restored OMP-compatible built-ins: `ultrathink` raises the current turn's reasoning effort to `ultra` through the host seam (guidance-only fallback on hosts without it), `orchestrate` and `workflow` steer delegation and staged-workflow discipline. Same deterministic rules as user modes: prose-only whole-word triggers, identifiers/quotes/code/paths never trigger, conflicts withhold both sides, `steering_status` shows what activated and what the effort request did. Built-in names are reserved; user modes configure their own triggers. | Typing `ultrathink` works out of the box and is visible, never silent: the reminder states the raise and `steering_status` records the provenance receipt. |
| Subagent effort | Native effort controls can change reasoning effort per call. | `subagent-effort` is shadow-only: verdicts and recommendations are recorded, never applied. Automatic routing stays NOT_QUALIFIED until paired evidence qualifies it. | Effort changes only through explicit user intent (the `ultrathink` keyword or the model/provider configuration) via `PluginContext.set_turn_reasoning` — never from a classifier's recommendation. |
| Model routing | OMP routes model selection by role, including metered/quota-aware routing. | `model-routing` supports role routing: configured `roles` (model + optional provider) resolve with precedence explicit > route > inherit, `fallback: inherit\|refuse` for explicit delegate `model_role` requests, and cross-provider targets validated at arm time. Metered/quota-based routing is **not ported**. | Role routing works via `model-routing`; nothing chooses a model from classifier output, cost or quota. |

## Native prerequisites

The matching host source supplies these shared contracts; there is no substitute scheduler or evidence database in this repository:

- `agent.verification_evidence`: explicit row lookup, external evidence, source/artifact metadata, terminal evidence IDs, disabled-ledger refusal and a durable edit watermark. A later check cannot revive pre-edit evidence.
- `hermes_cli.plugins` / `plugins_dispatch`: all callbacks run; block wins over approval; delivery/suppression receipts use unique directive identities. `post_tool_call_settled` fires exactly once after every `post_tool_call` dispatch, including when bounded observers are suppressed or time out, so call-observation reservations settle without being wedged forever. Modified operations are presented at approval. A selected internal `revalidate(final_args)` must return exactly `True` after consent or the operation is vetoed.
- `PluginContext.cancel_model_call(reason, *, hard=False, agent=None)`: a plugin-facing mid-stream cancellation seam. It requests interruption of the in-flight model/tool loop; the host stream monitor observes the request within its ~0.3s cadence and aborts in-flight provider streams (marks the request cancelled, closes request-local sockets, joins the streaming worker; the aborted call is not silently retried). Returns an honest receipt (`{"cancelled": bool, "reason": ...}`), never raises, and reports `cancelled: False` on surfaces with no resolvable agent (e.g. gateway). It is a cancellation request, not a sandbox: completed work is not stopped and completion is not immediate.
- `PluginContext.set_turn_reasoning(effort, reason="", agent=None)`: turn-scoped explicit reasoning control. Validates `effort` against the native vocabulary (`minimal` … `xhigh`, `max`, `ultra`, plus disable values), records the override for the current turn only (the turn finalizer clears it), and returns an honest receipt; unsupported values are refused, never coerced. For explicit user intent only (e.g. the `ultrathink` magic keyword) — never an inferred or classifier-driven switch.
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

Set `plugins.entries.prewalk.settings.target_model` to an execution model, optionally with `target_provider` when it is served by a provider other than the planner's. An explicit target beats the routed `executor` role (see *Model routing*); with neither set, prewalk stays inactive. Call `prewalk_arm`, populate native todos, then call `prewalk_ready` with:

- Located workspace-relative files and relevant symbols/callers. Files must be confined regular files; no callers requires a concrete `callers_absence_reason`.
- Constraints, non-goals, chosen approach and rejected alternatives.
- Falsification checks, replanning triggers and acceptance IDs; optional rule references.

The tool validates the brief and performs the handoff through the host model-switch seam `switch_agent_model`, falling back to native same-endpoint `switch_model` on hosts without the seam (on such hosts a cross-provider target refuses to arm). It does not claim to prove that every model-authored explanation is correct. `prewalk_status` and `prewalk_reset` expose state; `prewalk_status` also shows the target model/provider and its source (`explicit` or `route`). `replan_enabled` defaults to false; when enabled, `prewalk_replan` allows one return to the planner's original model and provider with a concrete failed assumption/check.

These tools may be deferred behind Hermes's native catalog. Discover the actual schema via `tool_search`/`tool_describe`; the bridge uses `{"calls":[{"name":"prewalk_arm","arguments":{}}]}`, not a bare `tool_name`.

## Model routing

`model-routing` resolves named roles to concrete models. Configure named roles under `plugins.entries.model-routing.settings.roles` (role names match `[a-z][a-z0-9_-]*`); each role has a required non-empty `model` and an optional `provider` validated against the configured providers without network access. An omitted provider means the calling surface's own parent provider (a parentless surface uses the configured main provider). `settings.fallback` selects what an unknown role does: `inherit` (default) or `refuse`. Unknown keys or providers fail plugin registration with the offending name:

```yaml
plugins:
  entries:
    model-routing:
      settings:
        roles:
          executor:
            model: glm-5.3-flash
            provider: openai
          reviewer:
            model: glm-5.3
        fallback: inherit
```

Resolution precedence is **explicit > route > inherit**. An explicit model — a per-call/surface model or an `auxiliary.*`/`delegation.*` pin — always wins and the role is ignored. A configured role routes to its `{model, provider}`. Anything else inherits today's behavior. With `fallback: refuse`, only an explicit delegate `model_role` request is rejected; implicit lookups (review owners, the prewalk executor) never refuse and inherit instead.

Surfaces:

- Delegate `model_role`: a per-task `model_role` beats the top-level one, and an explicit per-task model/provider pin beats both. A refused request rejects the task with the reason and spawns nothing. `/review` children take an explicit `auxiliary.review` pin first, then `model_role`/`roles.reviewer`, then inherit.
- Prewalk executor: an explicit `target_model` (with optional `target_provider`) beats `roles.executor`; with neither set, prewalk keeps today's behavior.
- Review owners: an explicit `auxiliary.<owner>` pin beats `roles.reviewer`, which beats inherit. A routed reviewer counts as routed for the existing parent-capability skip, like an explicit pin.
- `route_status` reports the configuration, effective resolutions, a bounded append-only audit of the plugin's own resolutions and the host seam. `route_apply` materializes pins — prewalk `target_model`/`target_provider`, `delegation.model`/`provider` and `auxiliary.{curator,background_review,review}` — through the native config writer with a provenance marker, never touches unrelated keys and reports drift.

Role routing needs a Hermes host with the `register_role_resolver`/`switch_agent_model` seams. Without them the plugin still loads and its tools work, `route_status` reports `host_seam: unavailable`, and nothing routes.

### Cross-provider routing

Roles and explicit targets may name a model on a different provider:

- Prewalk can hand off to another provider's model. Destination credentials and runtime are resolved exactly the way CLI/gateway startup resolves them (`resolve_runtime_provider`); at arm time a dry run validates the target, and an unresolvable provider refuses arming with the provider and reason named.
- A delegate child built on another provider gets that destination runtime's own configured trust map (`capabilities`), never the parent's — the same map gateway startup gives an agent on that provider. Direct `delegation.base_url` endpoints without a provider-configured trust map stay `{}`.
- Same-provider switches keep the agent's exact current endpoint (`api_key`/`base_url`/`api_mode`); nothing is re-resolved by provider name.

No path chooses a model from classifier output, cost or quota.

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

`keyword-steering.settings.modes` maps each mode to `refs`, optional `triggers` and `conflicts_with`. References point to your existing skills/rules rather than copied prompts. Genuine prose triggers activate only for the current user turn; code, quoted text, paths and identifiers are excluded. Contractions are prose (`don't` and `it's` no longer swallow the sentence); fenced blocks close only on their own fence. `orchestrate` and `workflow` are ordinary English words, so by default they activate only as `/workflow` or `#workflow` (`magic_keywords: {workflow: {bare: true}}` restores bare OMP matching; user modes accept `bare: false` too). The `orchestrate` routing sentence (`model_role`, `route_status`) is emitted only when the `route_status` tool is registered. A negation within three words (`don't ultrathink this`) declines the hit. With `settings.classifier.enabled`, only these ambiguous hits (common-word bare hits, negated hits) are sent, natively redacted, to the self-hosted classifier for one `steering: directive|mention|negated` label; an unavailable classifier declines the hit, and every hit's verdict and `confirmed_by` appear in `steering_status.hits`. Mode `refs` are resolved against the active skills dir and `settings.ref_roots` at registration; unresolved refs warn, are listed in `steering_status.unresolved_refs` and are annotated in the injected guidance. Conflicting modes are both withheld, the model is told to report the conflict to the user and ask which applies, a warning is logged, and `steering_status` explains requested/active/withheld state. The built-in magic keywords (`ultrathink`, `orchestrate`, `workflow`, individually toggleable via `settings.magic_keywords`) are reserved names on the same machinery: `ultrathink` explicitly raises the turn's reasoning effort to `ultra` through `PluginContext.set_turn_reasoning` (guidance-only fallback when the host lacks the seam), and the reminder plus `steering_status` report exactly what was applied. User-configured modes never change effort or permit deployment. Legacy `keywords` prompt-copy configuration is rejected rather than silently reinterpreted.

## Failure-aware recovery and risk screening

`loop-guard` records bounded exact call/result history, structured error identities and native failed checks. It detects repeated failures and short cycles without guessing that similar-looking edits are equivalent. Read-only polling is excluded. A blocker retains the failing operation: succeeding at an unrelated check does not clear it.

`loop_recovery status` shows the blocker/history/budget. A hypothesis names one exact next operation intended to distinguish explanations; it never bypasses native approval. Only delivered interventions spend the budget; suppressed/diagnostic directives do not. Exhaustion labels the final answer with the unresolved blocker. Mid-stream model cancellation is still outside this plugin's capabilities.

`auto-review-classifier` screens actual resolved targets/workspace, effects and unavailable context. Recognized credential arguments, flat assignments and nested secret-valued metadata are masked before native redaction and HTTP egress; unsupported nested or unparsed credential-bearing commands refuse screening before HTTP. This is bounded recognition, not a guarantee of detecting every arbitrary secret. Unsafe redaction or unavailable context is reported as unknown/un-screened, not safe. Approval scope includes the operation and target identity; retargeting after consent fails revalidation. This is an **advisory screen**: classifier failure remains deliberately fail-open, while deterministic host permissions and native approval remain authoritative.

`meta-classifier` keeps that screen (same redaction, snapshot, approval scope and revalidation; status `unknown` on incomplete evidence) and replaces the binary `safe/risky` head with droid's Execute risk criteria: `risk_scale: jev` sends the captured LOW/MEDIUM/HIGH definitions, brief examples and boundaries; `luna` sends the four autonomy tiers. The state is Jev-shaped (`pendingCommand` + a redacted `sessionTranscript` tail captured from `pre_llm_call`). Tiers at/above `approve_from` (default `high`) ask; `policy: deny` blocks. Before `git commit`/`git push` the Droid Shield port diffs staged (or unpushed) added lines on local backends: a line that native redaction scrubs is a finding (downgrade head, `B` at `cls_threshold` 0.3775 → **block**, else warn), a secret-named line without a finding is a risk context (risk head, `B` at 0.95 → warn). Scrubbed windows are sent, never raw values; downgrade failures block and risk failures stay silent, exactly as droid. The service exposes labels only, so droid's logprob thresholds ride as multi-label `cls_threshold`s and are uncalibrated against this model (live replay: the captured run-10 risk context droid warned on at P(B)=0.99 returned no `B` here). Local signals (`signals: true`) classify the rendered, redacted transcript with the intent/friction/expertise enum heads in one request on `session_end` and/or `turn_end` (skipped when unchanged) into `$HERMES_HOME/logs/meta-classifier-signals.jsonl`; unusable replies are recorded as errors, never as droid's plausible `Partial/OK` defaults. `decision_model` accepts a Jev evaluate request (`choice`/`noul`/`score`), redacts the state and returns labels without probabilities. `hermes meta-classifier-replay [--run] [--captures DIR]` rebuilds droid's 13 captured payloads with the plugin's schemas (`expect` only where both droid rubrics agree: `git status` → low, any push → high).

`meta-classifier` addons (`addons: [...]`, all off by default) put the same labels-only classifier on Hermes seams droid never had. Every addon writes one record (`addon`, labels or `error`) to `signals_path`; a dead classifier costs nothing on the clean path. `injection` (`transform_tool_result` on `injection_tools` and `mcp_*`) prefixes results carrying `instructions_to_agent: present` with an untrusted-data fence naming the `injection_target`. `outcome` (same hook, `terminal`/`execute_code`) labels `succeeded/failed/partial/needs_input`, replaces results with `secret_in_output: yes` by their redacted form, and feeds the turn's outcomes to `completion`. `completion` (`pre_verify`, attempt 0 only) classifies `Goal/Last action/Tool outcomes` and returns one `continue` nudge when `finished: no`, `claim_type: asserted` or a command was `failed`; a `verified` finish passes. `subagent` (`subagent_stop`) records `child_outcome/unverified_claims/scope` and logs a warning on `abandoned`/`yes`/`creep`; returns are ignored by the host, so it never rewrites a report. `request` (`pre_llm_call`) adds a one-line user-turn context only for `ambiguity: underspecified` (ask ONE question) or `destructive_request: yes` (confirm target, keep recovery path). `friction` (`post_tool_call`, `status == error` only) records droid's `frictionCategory/frictionSeverity` with a papercuts-compatible signature. `memory` (`post_llm_call`) records `durable: decision/preference/project_fact/none` candidates; it does not write memories. `test_value` (`pre_tool_call` on test-path writes/patches) records `behavioral/tautological/wiring_only`; with `test_value_gate: true` a non-behavioral label requires native approval. `complexity` (`pre_tool_call` on `delegate_task` spawns) records `trivial/moderate/deep` next to the chosen effort/role and never routes (routing stays NOT_QUALIFIED). `hermes meta-classifier-replay --cases FILE [--run]` replays human-labelled JSONL rows `{id, text, addon|schema, expect}` and prints per-head agreement, so each addon can earn its gate with evidence; the 19-case live smoke on 2026-10-18 agreed on 27/31 expectations (misses: multi-label `frictionCategory` over-fires at 0.5, `durable` preference→decision, `complexity` trivial→moderate).

`auto-answer-classifier` provides `clarification_defaults recommend`, `record` and `list`. Recommendation uses explicit bounded repository evidence and one all-choice ranking; it never treats option zero as truth. That single ranking is one `/v1/systemone` request carrying a choice question bound to the option labels and a yes/no question about whether a conventional default is sound here: the answer is accepted only at or above `min_sound_default` (default 0.5, which is the old yes/no argmax), `min_pick_confidence` (default 0.0) and a top-two `min_pick_margin` (default 0.0), so raising either of the last two turns near-ties and weak picks into abstentions. The reported probabilities, confidence and margin are recorded on the recommendation for later calibration. Supported ordinary inputs are unambiguous repo-wide `.editorconfig` settings and supported top-level TOML/JSON scalar conventions. Conditional/conflicting/unset values, ambiguous domains and secret-bearing evidence cause abstention. File hashes and ownership are checked again after ranking and before application.

A recorded convention requires an observed native human choice and a separate native **remember** consent. Only these approved scoped conventions can avoid the prompt outright. Repository recommendations stay advisory until a genuine eligible timeout; each derived answer retains provenance without overwriting human responses. Explicit skips, material choices, destructive decisions and approvals remain human-owned. `dry_run` never applies a default. Legacy batch results lacking per-row provenance remain advisory only.

## Browser backend and acceptance

Configure the native MCP server and grant its name in `plugins.entries.browser-bladebro.mcp_allowlist`; `settings.server` defaults to `bladebro`. Access is denied by default.

**Drop-in:** with `plugins.entries.browser-bladebro.allow_tool_override: true` the plugin replaces Hermes' built-in `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_scroll`, `browser_back`, `browser_press`, `browser_get_images`, `browser_vision` and `browser_console`. Names, schemas and result shapes stay the same, so prompts, skills, toolset membership and display all keep working. Bladebro runs every call. No local agent-browser/Chromium is needed. Without the grant, the built-ins stay and a warning names the key. Unloading the plugin restores them. Each task (session + `task_id`, like the built-in) owns one bladebro tab. Every call switches to that tab inside the same native run RPC as the action, so concurrent tasks sharing the backend's single current tab cannot cross. The exception is `browser_vision`: vision is not a run step, so it holds the plugin lock across switch + capture, which serializes within one process but not across processes. Refs accept the built-in `@e5`/`[5]` forms. The built-in URL secret/policy checks apply to the requested URL and to wherever the page lands. Oversized snapshots use the built-in truncate-and-store path. `browser_vision` attaches bladebro's screenshot natively or falls back to the auxiliary vision model. `browser_console(clear)` subtracts a baseline because bladebro has no buffer clear. Tabs survive turns and close at session finalize/reset; a delegated child's tabs close at `subagent_stop`. A tab bladebro lost (idle Chrome restart) is reopened by the next `browser_navigate`. `browser_cdp`, `browser_dialog` and the vault fill tools still need the built-in CDP/agent-browser stack. Leave `mcp_servers.bladebro.tools` unfiltered: `tools.include: []` leaves the server unconnected and breaks the plugin. The raw `mcp__bladebro__*` tools are only reachable through tool search anyway.

Extras: `browser_run` runs a bounded bladebro step batch on the task's current page. The batch is wrapped in an exact-URL guard bound to the URL read just before dispatch. It keeps native approval for side effects, bounded branching/iteration and no automatic replay after an uncertain outcome. `browser_scenario` and `browser_capture` provide certified acceptance. Ordinary browsing works on HTTP sites, including authenticated surfaces, but a login wall or inaccessible state does not count as success.

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

The 2026-10-10 v4 run attempted 24 arms. One timeout pair and one mixed-effort-transmission pair were excluded, leaving 10 eligible comparisons. A broken path-security oracle and incomplete schema-rollback oracle were found during review; original artifacts remain unchanged. `evaluation/reassess_paired_v4.py` replays corrected oracles against hash-verified frozen candidates, in a separate output directory, without new model calls. These results are explicitly **retrospective**, not a preregistered rerun.

The current comparative run is `paired-live-v5`: all 10 cases (the 6 frozen plus 4 additive), 20 pairs / 40 arms, `medium` versus `xhigh`, two counterbalanced repeats with shared per-pair seeds and isolated homes. It completed 18 eligible pairs; one `memo-flight` repeat timed out in both arms at the 180-second child cap and remains in correctness/latency as a censored observation, while two xhigh arms were excluded by the wire-support gate because one request omitted the effort field. Raw unfiltered outcomes were medium 19/20 correct and xhigh 20/20; eligible-pair aggregates were:

| Eligible-arm observation | medium | xhigh |
|---|---:|---:|
| Correct under corrected deterministic oracles | 17/18 | 18/18 |
| Raw unfiltered correctness | 19/20 | 20/20 |
| Cumulative native execution seconds | 353.27 | 385.99 |
| Mean native child latency, seconds | 19.63 | 21.44 |
| Native input + output tokens | 721,800 | 736,040 |
| Native API calls | 112 | 107 |
| Verification ledger rows observed | 19 | 17 |
| Verified corrective revisions | 0 | 0 |
| Native cost estimate | unknown | unknown |

The paired descriptive deltas were 838 fewer tokens and 1.82 seconds faster for `medium` on average, but the spread is large and this is not a savings claim. Two xhigh requests in separate pairs omitted the effort field and were excluded by the wire-support gate; the timeout pair remains included as censored latency with unobserved tokens/cost.

Lilith transmitted `chat_template_kwargs.reasoning_effort`, budget fields 8192/65536 and scaffold flags false/true. The client profile sets `enable_thinking: true`; an executed sandboxed render of the deployed tokenizer's hash-matched template confirms that medium and xhigh produce distinct prompt prefixes. The same render proves that budget/scaffold fields do not change this template; their intended SGLang behavior is **not qualified on the deployed vLLM fork**. Runtime reasoning-budget enforcement remains unknown. The provider also sends `auto_disable_thinking_with_tools` on tool turns, so backend thinking may flatten even when the wire effort differs. Earlier v3 missing-effort observations were an instrumentation omission, not evidence of unsupported transmission. Durable receipts are under `/home/common/projects/hermes-plugins-qualification/2026-10-10/evidence`: `paired-live-v5/`, `effort-template-support.json`, `paired-v4-closure/` and the unchanged `paired-live*` attempts. The closure receipt verifies all 24 arm provenance chains and 136 unchanged historical files. These small-sample descriptive totals establish neither general quality equivalence nor monetary savings. **Automatic routing remains NOT_QUALIFIED.**

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
- Mid-stream model cancellation is implemented (`PluginContext.cancel_model_call`); surfaces without a resolvable agent (gateway) return an honest refusal, and browser tab cleanup still cannot run after an unclean process kill.
- DSH browser/search/Docling backends are not interchangeable with OMP's native implementations.

## Attribution

Derived from dsh-plugins by carrion256 (MIT), itself porting features from [oh-my-pi](https://github.com/can1357/oh-my-pi) (MIT © Mario Zechner, Can Boeluek, Stencil Labs). Each port inherits the upstream MIT grant.
