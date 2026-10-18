# 17 mock-harness (offline scripted-model harness)
Status: proposed · Owner: — · Depends on: none (host seams: none). Consumed by every behaviour-changing plan's P-phase acceptance checks.

## Goal
One reusable, dependency-free scripted model server (`mockmodel/`) that speaks all three Hermes wire formats (OpenAI Chat Completions, OpenAI Responses, Anthropic Messages), plays plans deterministically, injects faults on demand, refuses any non-loopback egress, logs one structured line per model turn, and ships a PASS/FAIL smoke script. Plugin integration tests, the real-CLI workflow tests and the paired-experiment runner all use it instead of each re-implementing an inline HTTP handler. It proves plumbing and plugin behaviour, never model skill.

## Evidence
- **droid mock (observed, 579 lines)** `/var/lib/hermes/projects/droid/extracted/mock-factory-api.mjs`:
  - Plans embedded in a user message as `<<MOCKPLAN [json] MOCKPLAN>>`, parsed string-aware so a subagent prompt can carry its own nested plan (`:26-27`, `:67-85`).
  - Stateless step index: assistant runs after the last plan-bearing user message (`planStep`, `:87-104`). Retries, parallel sessions and subagents therefore work with no server state.
- Tool-less requests are helpers (title, summariser, classifier) and never consume a step (`chooseTurn`, `:118-129`); `MOCK_HELPER_REPLIES` maps a call type to canned text (`:355-366`).
  - A malformed plan is answered with its parse error instead of crashing (`parsePlan`, `:67-85`). `$LAST_TASK_ID` substitutes earlier tool output into later args (`:109-113`).
  - `MOCK_FAULTS=<file.json>` is re-read per request. A spec is chosen by `FAULT=<name>` in the prompt or by `match{callType,model}`, then plays `<transport>_seq[attempt]`, repeating the last step if `_repeat` (`:299-328`). HTTP step kinds: `status`, `stream_error`, `truncate_after`, `empty`, `hang` (`:306-308`); WS kinds `error|error_after|drop` (`:308`).
  - Per-turn `describe()` JSONL `{route, model, ntools, purpose, step, of, tools}` (`:292-296`).
  - Routing by path suffix so both proxy paths and BYOK base URLs work (`:10-17`, `handleLLM` `:368`).
- **droid tooling (observed)**: `tooling/e2e-env.sh:26-29` sets `HTTP(S)_PROXY` to the mock, which logs `{egress: host}` and refuses; `NO_PROXY` exempts loopback. `tooling/e2e-smoke.sh:2-4` lists the scenarios (one scripted tool loop per wire protocol, nested subagent, fault-then-resend, egress count); `:20-31` runs them and prints `PASS/FAIL`. `AGENTS.md:101-108` makes the smoke script the only regression gate. `notes/DEEP-FINDINGS.md:193` records the 0-egress result.
- **Hermes side (source)**:
  - `api_mode` is one of `chat_completions|codex_responses|anthropic_messages|bedrock_converse|codex_app_server` (`hermes_cli/runtime_provider.py:93`). A base URL path ending `/anthropic` auto-selects `anthropic_messages` (`:117-123`).
  - Responses streams are raw `responses.create(stream=True)` events assembled from `response.output_item.done`; terminal `output` may be null (`agent/codex_runtime.py:519-522`). Progress delta types are at `:533-536`.
  - Anthropic uses `messages.stream()` (`agent/chat_completion_helpers.py:3012`, `agent/anthropic_adapter.py:638-689`).
  - Helpers (title, classifiers) go through `call_llm` and are tool-less; the title call also sends a `json_schema` `response_format` (`agent/title_generator.py:264-276`).
  - `_validate_proxy_env_urls()` fail-fasts on malformed proxy env (`agent/auxiliary_client.py:2673`).
  - `grep websocket` over `agent/` finds no LLM transport (only a browser CDP URL docstring), so droid's WS faults have no Hermes counterpart.

## Current state
- Real-loader plugin tests: `tests/conftest.py:12-53` builds an isolated `HERMES_HOME` and replaces `socket.socket.connect` with an `AssertionError` (`:27-30`). That is an in-process, all-or-nothing egress ban with no loopback allowance, so no in-process test can reach a mock server today.
- Real-CLI tests: `tests/test_workflow_cli.py:67-155` and `tests/test_covey_cli.py:243-315` each hand-roll a `ModelBoundary(BaseHTTPRequestHandler)` (this is the "loopback model boundary" of `README.md:285`). Both are chat-completions only and keep a mutable step counter (`observed["step"]`). Review and helper traffic is recognised by substring (`"You were spawned by the /review command."`) or by `not request.get("tools")`. Tool-call args are patched from earlier tool results (the `evidence_id` splice at `:112-123`). Streaming is hand-built (`:136-150`). There are no faults, no Responses or Anthropic mode, no egress check and no per-turn log.
- Paired evaluation: `evaluation/evaluate_paired.py:424-513` takes `--base-url` and `--env-file`, hard-codes `provider: lilith, api_mode: chat_completions` (`:449-450`), and probes the live service (`:479-482`). `tests/test_evaluation_paired.py:1-8` is explicitly offline but only tests corpus shape, plan determinism and aggregator refusal; the worker/runner path is never exercised offline.
- Gap: duplicated, single-wire, stateful, fault-free boundary; no way to test retry/stream-failure/compaction/fallback behaviour of loop-guard, continuation, model-routing or papercuts; no mechanism to prove "zero egress" for the CLI subprocess.

## Design
**Location / shape.** New top-level package `mockmodel/` (stdlib only, Python ≥ the host's; no new deps), importable from `tests/` and `evaluation/` via the same `sys.path.insert` idiom `tests/test_evaluation_paired.py:20` already uses:
`mockmodel/{__init__,__main__,server,plan,helpers,faults,tripwire,describe}.py`, `mockmodel/wire/{chat,responses,messages}.py`, `mockmodel/smoke.sh`. `python -m mockmodel --port P --log run/api.jsonl` runs standalone; `mockmodel.start()` returns a handle for in-process use (daemon `ThreadingHTTPServer` on `127.0.0.1:0`, same as today).

**Routes** (path-suffix match, like droid): `POST …/chat/completions`, `POST …/responses`, `POST …/messages`, `GET …/models` (list with `context_length`, matching the existing handler `test_workflow_cli.py:71-72`), plus control endpoints `POST /__mock/plans`, `POST /__mock/faults`, `POST /__mock/reset`, `GET /__mock/turns`. Base-URL recipes (verified against `runtime_provider.py`): chat `…/v1` with `api_mode: chat_completions`; Responses `…/v1` with `api_mode: codex_responses`; Anthropic `http://127.0.0.1:P/anthropic` (auto-detected, `:117-123`) with explicit `api_mode: anthropic_messages`. P1 must prove which `providers:` config shape keeps `codex_responses` for a loopback host (see Risks).

**Wire formats emitted.** Streaming and non-streaming for each:
- Chat: `chat.completion.chunk` deltas (`role`, `content`, `tool_calls[{index,id,function{name,arguments}}]`, optional `reasoning_content`), a final chunk with `finish_reason` and `usage`, then `data: [DONE]` (superset of what `test_workflow_cli.py:136-150` emits). Multiple tool calls per step are emitted as indexed calls.
- Responses: `response.created`, `response.output_item.added`, `response.output_text.delta`, `response.function_call_arguments.delta|done`, `response.output_item.done` (the event Hermes assembles from, `codex_runtime.py:519-522`), `response.completed` with `usage`. Hermes' terminal handlers are `response.completed|incomplete|failed` (`codex_runtime.py:724-726`); progress deltas are the set at `:533-536`. A `terminal_output: null|[]|absent` knob covers the null-output contract at `:519-522` (the SDK's `responses.stream(...)` crashes on a null `response.completed.response.output`, so Hermes consumes raw `responses.create(stream=True)`). `response.failed` frames drive the fault catalogue.
- Messages: `message_start`, `content_block_start|delta|stop` (`text_delta`, `input_json_delta`, `thinking_delta`), `message_delta{stop_reason,usage}`, `message_stop`; `event: error` for faults. Hermes streams through `messages.stream()` (`chat_completion_helpers.py:3012`; `_stream_final_message` `anthropic_adapter.py:638-660`) and falls back to `create()` when the stream is unavailable (`_is_stream_unavailable_error` `:627`, fallback `:678-689`), so the non-streamed JSON shape must also be served.
Each emitter is validated in the self-test by feeding it to the real `openai` / `anthropic` SDK client, so drift is caught against the SDK rather than against our own reading of it.

**Plans.** Same grammar as droid, extended. A step is `{"text":…}` | `{"tool":name,"args":{…}}` | `{"tools":[{name,args}…],"text"?,"reasoning"?}`; any step may add `usage` (to trip compaction thresholds) and `delay_ms`.
- *In-prompt*: `<<MOCKPLAN [..] MOCKPLAN>>` in a user message (nested plans in delegate goals work as in droid).
- *Registered* (`MOCK_PLANS=<file.json>` or `POST /__mock/plans`): `[{"match":<regex over first user/system text>,"model"?:substr,"steps":[…],"repeat"?:bool}]`. This is the default for plugin tests because putting JSON in `--query` perturbs `keyword-steering`, `auto-answer-classifier` and `prewalk`, which read the query.
- *Step index (stateless)*: number of maximal assistant runs after the plan-bearing user message (in-prompt) or after the conversation start (registered). Chat counts `assistant` messages; Responses counts `function_call|reasoning|message(assistant)` items; Anthropic counts `assistant` messages. Tool/`function_call_output` messages and Hermes-injected user nudges (verify-on-stop, rule guidance) do not advance it. Retries therefore replay the same step.
- *Helpers never consume steps*: a request is a helper when it has no `tools`, or when it matches a `helpers` entry (`{"match":regex,"reply":text|json,"tools_ok":true}`, replaces both `MOCK_HELPER_REPLIES` and the `/review` substring hack). Defaults: tool-less + `json_schema` named `session_title` → `{"title":"Mock session"}`; other tool-less → `"mock helper"`.
- *Templating* generalises `$LAST_TASK_ID`: `{{last_tool.json.<dotted.path>}}` resolves against the latest tool-result JSON, covering the `evidence_id` splice currently coded by hand. An unresolvable reference fails the turn visibly (answers `mock: unresolved …` and logs `planError`) rather than sending a wrong argument.
- A tool named in a step but not advertised in `request.tools` is rewritten to the `tool_call` wrapper only when a `wrap` rule is configured (today's behaviour at `test_workflow_cli.py:130-132`); otherwise it is emitted as-is so a misconfigured plugin shows up as a Hermes "unknown tool" result, not a hidden pass.
- After the last step: `"MOCKPLAN complete"`; no plan: fixed text. A malformed plan answers its parse error as assistant text.

**Fault catalogue** (`MOCK_FAULTS=<file.json>`, re-read per request; selection by `FAULT=<name>` in a tool-bearing request body or by `match{callType,model}` (substring) so tool-less helpers can be targeted; `<transport>_seq[attempt]` per name, `<transport>_repeat`; attempt counters reset by `/__mock/reset`; mirrors droid `pickFault` `:310-328`):
| kind | effect | exercises |
|---|---|---|
| `status` `{status,headers?,body?}` | 429 (+`retry-after`), 500, 502/503, 529 (Anthropic overloaded), 401, 400 context-length body | Hermes retry/backoff/fallback, compaction trigger, credential error path |
| `stream_error` `{after,event}` | well-formed error event after N events | mid-stream failure, partial-turn discard |
| `truncate_after` N | stream ends without terminal event/`[DONE]` | incomplete-stream handling |
| `no_done` | chat stream ends with a clean `finish_reason` + usage frame but no `data: [DONE]` | must complete, not be treated as a drop (Nous-style terminal, `chat_completion_helpers.py:2702-2705`) |
| `empty` | valid stream, no content | empty-response retry |
| `hang` | accept, never answer | request timeout, `cancel_model_call` |
| `stall` `{after,delay_ms}` | pause mid-stream | liveness watchdog (`anthropic_adapter.py:672`) |
| `garbage` `{body}` | 200 with non-JSON / malformed SSE `data:` | parser robustness |
| `bad_tool_args` | tool call whose `arguments` is invalid JSON | argument-repair / loop-guard paths |
| `terminal_output_null` | Responses `completed` with `output:null` | `codex_runtime.py:519` contract |
WS faults (`error|error_after|drop`) are not ported: no WS transport in Hermes' text path. Every fault applied is logged `{fault:{name,idx,kind}}`.

**Egress tripwire.** The same server also answers proxy-style requests (`CONNECT host:port` and absolute-URI `GET http://…`) on its port: logs `{egress:"host"}` and refuses (403 / close). `mockmodel.env(port)` returns `HTTP(S)_PROXY`/`http(s)_proxy` = the mock and `NO_PROXY=127.0.0.1,localhost`, the droid recipe (`e2e-env.sh:25-29`); all CLI-subprocess runs inherit it. Hermes validates these vars as URLs (`auxiliary_client.py:2673`), so they must be well-formed. Second layer for in-process tests: replace the `conftest.py:27-30` blanket ban by an allow-list guard permitting only `127.0.0.1:<mock port>` and still raising for everything else. Assertion helper: `handle.egress() == []`, enforced in the fixture finaliser so no test can forget it.

**Per-turn log (`describe`).** One JSONL line per model request: `{t, route, api_mode, model, stream, nmessages, ntools, tools:[advertised names], purpose:(first 70 chars of system), step, of, helper, helper_name, plan, called:[tool names emitted], reasoning_effort:(extracted from `reasoning`/`reasoning_effort`/`thinking`), fault}`. `reasoning_effort` and `model` are what make the subagent-effort and model-routing plugins observable offline. `MOCK_LOG_BODIES=1` adds the redacted request body (keys masked) for transcripts; off in CI.

**In-process API.** `handle.turns(model=, helper=)`, `handle.requests()` (raw bodies, for asserting what `llm_request` middleware did to the prompt), `handle.egress()`, `handle.set_plan(...)`, `handle.set_faults(...)`.

**Posture.** Harness answers are labelled synthetic: every response id is `mock_…`, usage numbers are fixed (`100/30` as today) unless a step overrides them, and the evaluation runner stamps `services.model = "mockmodel"` (below). Nothing here can produce a verdict about a real model.

## Host changes
None. All hooks used are provider-level (`base_url`, `api_mode`) and `HTTP(S)_PROXY` env, which Hermes already honours.

## Phases
- **P0 – core + chat wire + self-test.** `mockmodel/` server, plan grammar, stateless step index, helper bypass, chat emitter, `describe` log, `python -m mockmodel`. *Accept*: `tests/test_mockmodel_selftest.py` drives the real `openai` client (stream and non-stream) through a 3-step plan, a retry that replays the same step, a helper that consumes no step, a malformed plan, and `{{last_tool…}}` templating.
- **P1 – Responses and Anthropic wires.** Emitters plus `terminal_output` knob. *Accept*: self-test passes with the real `openai` Responses client and `anthropic` `messages.stream()`; a one-shot `hermes -z "<prompt>"` (`_parser.py:113`) completes a write-file tool loop on each of `chat_completions`, `codex_responses`, `anthropic_messages`; the codex_responses config shape that survives the drop at `runtime_provider.py:152-154` for a loopback host is recorded in the plan's Risks section.
- **P2 – faults + tripwire.** Fault catalogue, `reset`, proxy listener, `env()`, allow-list socket guard. *Accept*: smoke shows a 429-then-pass run finishing with the correct result and `fault` log `status,pass`; `hang` ends in a Hermes timeout error rather than a hang past the test timeout; an injected `curl https://example.test` from a plan'd terminal call produces exactly one `egress` record and a FAIL from the fixture.
- **P3 – migrate existing boundaries.** Re-express `test_workflow_cli.py` and `test_covey_cli.py` as registered plans + helpers, delete both inline `ModelBoundary` classes, add the `mock_model` fixture and the `hermes_cli_run(plugins, settings)` helper to `tests/conftest.py`. *Accept*: both tests pass with their assertions unchanged (no assertion is weakened to fit; if a plan cannot reproduce an assertion the harness is extended, not the test), and `grep -rn ModelBoundary tests` is empty.
- **P4 – smoke script + plugin scenarios.** `mockmodel/smoke.sh` (below) and one new fault scenario per behaviour plugin as each plan lands (loop-guard on `bad_tool_args`, continuation on `empty`/`truncate_after`, model-routing/subagent-effort on logged `model`/`reasoning_effort`). *Accept*: smoke exits 0 with all `PASS`.
- **P5 – evaluation adoption.** `evaluate_paired.py --mock` starts `mockmodel` on a free port and points `--base-url` at it with `evaluation/mock_plans/<case_id>.json`; each pair's two arms receive byte-identical plans, so any per-arm difference in the log is attributable to the plugin (effort, model) rather than to sampling. *Accept*: a full dry run over the corpus completes offline; `manifest.json` records `services.model.identity = "mockmodel"` and `qualification` stays `NOT_QUALIFIED`; a new `tests/test_evaluation_paired.py` case proves `aggregate_pairs` refuses mock evidence for any qualification claim and that `--mock` and `--base-url <live>` are mutually exclusive.

**Smoke script (`mockmodel/smoke.sh`).** Mirrors `e2e-smoke.sh`: temp lane dir, isolated `HERMES_HOME`, `. env` for proxy vars, background mock, wait for port, a `run name wire expected plan [FAULT]` function printing `PASS name` / `FAIL name (got: …; see lane/name.err)`. Scenarios: one tool loop per wire (chat/responses/messages), nested delegate with its own plan, helper-doesn't-consume-step, 429 then pass, `hang` (bounded by `timeout`), egress count `== 0`, then a `turn route model step=… tools=…` summary and `exit $fail`. Needs no network and no real keys (`synthetic-test-key`, as the existing tests use).

## Tests
- `tests/test_mockmodel_selftest.py` (new): step index (retry/parallel/nested), helper bypass, templating, each wire parsed by the real SDK client, every fault kind observed from a real client, tripwire refuses `CONNECT`.
- `tests/test_workflow_cli.py`, `tests/test_covey_cli.py`: migrated (P3); they remain the real-CLI regression for planning → verify → review.
- `tests/test_evaluation_paired.py`: mock-evidence refusal and `--mock` exclusivity (P5).
- Each new plugin plan adds its fault scenarios here, using real loaders from `conftest.load_plugin` and the `mock_model` fixture, never a Hermes double.
- **CI command** (extends `README.md:276-283`):
  ```sh
  export HERMES_SOURCE=/home/common/projects/hermes-agent
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERMES_SOURCE${PYTHONPATH:+:$PYTHONPATH}" python -m pytest -p no:cacheprovider tests -q \
    && bash mockmodel/smoke.sh
  ```
  No CI configuration exists in the repo (`.github/.forgejo/.gitea` absent), so this is the documented gate until one is added.

## Risks & open questions
- **Step-index fragility under Hermes history rewriting.** Context compaction replaces history; the plan-bearing message may vanish, and plugins that drop or rewrite assistant turns (loop-guard redirects, steering) shift the count. Mitigations: registered plans count from conversation start of the *matched* conversation; a step may carry `"anchor":"<substring>"` to pin itself to a tool-result marker; the log records `step/of` so a drift is visible. Compaction-sensitive scenarios use a `usage` override and assert on the log, not on silent success.
- **Provider routing for loopback.** `runtime_provider.py:152-154` ignores a persisted `codex_responses` for non-OpenAI endpoints in the plain-custom path; the named `providers:` shape used by the existing tests may behave differently. Verify empirically in P1 and document the one working config; if none exists, the Responses wire is only reachable through the SDK-level self-test and an upstreamable host seam would have to be proposed (not assumed here).
- **Proxy honouring.** The tripwire only catches clients that respect `HTTP(S)_PROXY` (httpx does). Raw-socket code (e.g. `urllib` with proxies disabled, subprocess tools that ignore env) is only covered by the in-process socket guard; the CLI subprocess has no second layer. State this limit in the fixture docstring.
- **Helper detection by regex is brittle** against upstream prompt wording changes (e.g. the title prompt in `title_generator.py`); the tool-less default plus `json_schema` name is the stable signal, regex matches are per-test opt-in.
- Anthropic SDK path assumptions (`/v1/messages` appended to base URL) are inferred from SDK behaviour, not read in the SDK; the P1 self-test settles it.
- Whether the meta-classifier GLiNER service (`CLASSIFIER_URL`) deserves its own route set is open; the `helpers` table covers canned replies, not the service protocol.

## Non-goals
- Not a model-quality or paired-effort *result*; mock runs can never qualify routing or effort.
- Not a classifier-service emulator (`evaluate_shadow.py` and `meta-classifier` use their own boundary).
- No WebSocket transport, no Gemini route, no Bedrock/`codex_app_server`.
- No record/replay of real provider traffic and no fuzzing; plans are hand-authored and deterministic.
- No change to Hermes or to plugin runtime code; test-time only.
