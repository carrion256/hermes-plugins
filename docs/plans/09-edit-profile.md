# 09 — Model-specific edit profile
Status: proposed · Owner: — · Depends on: none (host refactor in `tools/file_tools.py` + `agent/coding_context.py`)

## Goal
Pick one edit format per model family in one place, and make the tool schema and the prompt guidance agree. Today Hermes has two independent decisions about the V4A (OpenAI `apply_patch` dialect) patch mode: whether the schema exposes it (`tools/file_tools.py`) and whether the system prompt tells the model to use it (`agent/coding_context.py`). They use different matching rules, so they can disagree. This plan unifies them behind one resolver, adds an explicit per-config override, and states what would be needed before a true freeform (`custom`) tool is worth building. It does not add a freeform transport now.

## Evidence
- **Hermes schema gate, read.** Base `PATCH_SCHEMA` is replace-only; the comment says V4A layers on "for OpenAI-family mains only" to avoid a ~148 tok/call tax on every other session (`tools/file_tools.py:1056-1065`). The layer is applied by `_patch_schema_overrides` (`file_tools.py:1252`) and wired via `dynamic_schema_overrides` at `file_tools.py:1274`.
- **Hermes family test, read.** `_is_openai_family_main()` (`file_tools.py:1128-1150`) returns true for providers `{openai, openai-chat, openai-codex, azure-openai, codex}`, or when the model slug (after the last `/`) starts with `gpt-`, `gpt.`, `chatgpt`, `codex`, `o1`, `o3`, `o4`, `o5`, or the model contains `openai/`.
- **Hermes prompt guidance, read.** `_EDIT_FORMAT_GUIDANCE` (`agent/coding_context.py:61-81`) selects `patch` mode when the model id contains any of `gpt`, `codex`; it selects `replace` for a list including `claude`, `gemini`, `deepseek`, `qwen`, `kimi`, `glm`, `grok`, `hermes`, `llama`, `mistral`, and others. Matching is substring on the model id. The comment says it is "aligned with TOOL_USE_ENFORCEMENT_MODELS", which is a second, unshared list.
- **Existing tests, read.** `tests/tools/test_patch_v4a_gate.py:1-31` exercises `_is_openai_family_main` and `_patch_schema_overrides` by patching `agent.auxiliary_client._read_main_provider` and `_read_main_model`. `tests/tools/test_file_tools.py:515-543` asserts that the V4A layer documents both modes' required params. The handler accepts both shapes from any model, with `mode` defaulting to `replace` (`file_tools.py:1062-1065`).
- **Freeform on the receive side only (grep).** `agent/codex_responses_adapter.py:891-965` parses `custom_tool_call` items and reads the `input` field. A grep for a request-side freeform tool definition (`"type": "custom"`) in `agent/`, `tools/`, and `hermes_cli/` found none; the only hit is an ACP client config (`website/docs/user-guide/features/acp.md:208`). Hermes therefore parses freeform output but never sends a freeform tool.
- **Droid reference, read.** `resolveFileEditToolProfile({modelId, customModel})` (`extracted/bunfs/sources/packages/utils/src/llm/model-matching.ts:216-246`) returns, in order: `customModel.fileEditToolProfile` if set; the registry entry's profile; `ApplyPatch` when the registry provider is `ModelProvider.OPENAI` or, with no registry entry, when `isOpenAIModelString` matches; otherwise `CreateEdit`. One resolver, one explicit override field.
- **Droid permissions, notes.** `notes/arch/tools.md:7-10`: the permission class is derived from `uiGroupId` (`EditFile|CreateFile` → edit). ApplyPatch prompts unless auto-approve-file-edits is set (`shouldConfirmExecution.ts:630, 657, 945`, per notes). Relevant only because a second edit tool would need its own permission mapping.
- **Plugin seam, not read.** `tools/registry.py` exists (path confirmed), but its `register` signature and the handling of `dynamic_schema_overrides` for plugin-registered tools were not read. Treated as an open question (see Risks).

## Current state
- Two gates, two matching rules, no shared resolver. Disagreement is possible. Example to verify in P1: a model id `llama-gpt-tuned` on `openrouter` contains the substring `gpt`, so `coding_context` prints the V4A guidance, while `_is_openai_family_main` checks the slug prefix, so V4A is not in the schema. The prompt then tells the model to use `patch mode='patch'` on a schema that does not offer it. [INFERENCE from reading both rules; not yet reproduced.]
- No config override exists. Users cannot force a profile per model, unlike droid's `customModel.fileEditToolProfile`.
- The handler already tolerates both shapes, so schema exposure is a prompt/size concern, not a correctness concern for replay.
- No freeform request-side path exists in Hermes.

## Design
One resolver, one override, one prompt line.

**Resolver.** `resolve_edit_profile(provider, model, config) -> "v4a" | "replace"` in a new small module `agent/edit_profile.py` (new file justified because two consumers, `tools/file_tools.py` and `agent/coding_context.py`, must not import each other). Order, mirroring droid:
1. `config.agent.edit_profile` if set to `v4a` or `replace` (explicit override).
2. Provider in the OpenAI-family set, or slug-family match, using the family rule currently in `_is_openai_family_main` (moved, not re-written).
3. Otherwise `replace`.

Unknown families get `replace`, the same as today.

**Consumers.**
- `file_tools._patch_schema_overrides` calls the resolver; V4A layer is added only when it returns `v4a`.
- `coding_context._EDIT_FORMAT_GUIDANCE` is keyed by the resolver result, not by substring. The substring lists are deleted. The guidance text stays as-is.

**Freeform (explicitly deferred).** A true `custom` tool (grammar `*** Begin Patch` as raw text rather than a JSON string) would need: provider support confirmed per transport, a request-side emitter in each transport (not just the Codex parser), and a parser on each return path. None exists. The V4A-in-JSON-string form already works on every transport that supports function calls, and the handler already accepts it. Plan 09 keeps V4A as a `mode` of the `patch` function tool. Freeform is revisited only if P3's probe shows a measurable edit-success gain.

**Plugin seam.** No new plugin. The edit-tool choice is core to the `patch` tool's schema, so it stays a host concern. A plugin can influence the choice only through config (`agent.edit_profile`). If a third-party plugin needs its own edit tool, it registers a separate tool name; the resolver does not change.

## Host changes
- **H1 — resolver module.** Add `agent/edit_profile.py` with `resolve_edit_profile`. Move the family predicate out of `file_tools._is_openai_family_main` and have that function delegate to the resolver (keep the name, since tests patch it indirectly through `_read_main_*`). No behaviour change in P0.
- **H2 — config key.** Read `agent.edit_profile` (default unset/`auto`) in the resolver. Add to the config schema and docs wherever `agent.verify_guidance` / `agent.max_verify_nudges` are documented (`verify_hooks.py` already reads `agent.*` keys this way).
- **H3 — guidance wiring.** `coding_context.py` imports the resolver and selects guidance from its result. Delete `_EDIT_FORMAT_GUIDANCE` substring tuples.
- **H4 (only if P3 proceeds).** Request-side freeform emitter per transport. Not in scope for P0–P2.

No change to `VALID_HOOKS`, no new hook, no change to `_patch_schema_overrides`' signature (it is registered with `dynamic_schema_overrides` and the registry calls it with no args).

## Phases
- **P0 — resolver, no behaviour change.** Add the module; route `_is_openai_family_main` through it. Acceptance: `tests/tools/test_patch_v4a_gate.py` and `tests/tools/test_file_tools.py:515-543` pass unmodified; the schema bytes for a GPT main and a Claude main are identical to `main` (compare `json.dumps(PATCH_SCHEMA)` plus overrides).
- **P1 — prompt agrees with schema.** `coding_context` uses the resolver. Acceptance: for a fixed table of ≥20 (provider, model) pairs, the guidance mode equals whether the schema carries the V4A layer. The `llama-gpt-tuned`/`openrouter` case flips from disagreement to agreement (expected: `replace` guidance, since slug is not GPT-family; verify and record the actual outcome).
- **P2 — config override.** `agent.edit_profile: v4a` on a Claude main exposes V4A in the schema and guidance; `replace` on a GPT main removes it; `auto`/unset matches P1. Acceptance: three unit cases plus a round-trip through the `patch` handler with `mode='patch'` on a Claude main when forced.
- **P3 — freeform probe (gated, optional).** Measure V4A edit success (applied-without-retry rate) on the existing eval harness for GPT mains. Build H4 only if the measured gain over the JSON-string V4A form exceeds the measurement noise on at least two model families. Acceptance: a written result either way; no code unless the gate passes.

## Tests
`tests/agent/test_edit_profile.py` (no model calls; real config loader with a temp config):
- table test: (provider, model, override) → expected profile, covering each provider in the family set, slug-prefix cases, `openrouter` aggregators with `openai/` prefix, `llama-gpt-*` (the disagreement case), and unknown models → `replace`;
- schema agreement: for each table row, `'mode' in _patch_schema_overrides()` description ⇔ resolver returns `v4a`;
- handler round-trip: with the profile forced either way, `patch` accepts the replace shape and the V4A shape from any model; `mode` omitted defaults to `replace` (`tools/file_tools.py:1062-1065`). A V4A call on a profile that does not expose V4A still succeeds, because the handler does not gate on the schema.
- guidance agreement: `coding_context` guidance line contains `mode='patch'` ⇔ resolver returns `v4a`;
- override precedence: explicit config beats family rule in both directions.

Keep the table in one place; the test is the only consumer of the full table.

## Risks & open questions
- **Two lists may be intentional.** The comment says `_EDIT_FORMAT_GUIDANCE` is "aligned with TOOL_USE_ENFORCEMENT_MODELS". If that list has families the schema rule does not, P1 must choose one truth; the plan currently assumes the schema rule (it governs what the model can call). Verify before P1.
- **Plugin dynamic overrides.** Unverified whether a plugin-registered tool can pass `dynamic_schema_overrides` to the registry. If not, plugins cannot add their own edit-mode layer; the plan does not require that.
- **Schema byte cost.** P0's byte-identity check assumes `_patch_schema_overrides` is deterministic per resolver result. Config changes mid-session (compaction refresh, `file_tools.py:1255` comment) must not change the resolver output mid-turn; confirm the config read is cached per session.
- **Freeform may be worth more than measured here.** P3 may show a gain only on the Responses path; then the work belongs to the Codex transport, not this plan.
- **Droid's ApplyPatch approval prompt** (`shouldConfirmExecution.ts:630, 657, 945`) has no Hermes analogue; Hermes approval is keyed by tool name, so a V4A call inside `patch` is approved as a `patch` call. Confirm that this matches the desired policy before P2.
- **Agreement test anchors on text.** The schema check reads the `patch` description to detect the V4A layer. Wording changes would break the agreement test without a real disagreement; keep the test on the resolver result and read the description only for the layer marker.
- **Mid-session config.** If `agent.edit_profile` changes during a session, the schema must not flip mid-turn. `file_tools.py:1254-1255` says the layer is a config/context probe with no I/O at schema-build time, and that compaction's tool refresh re-runs it. So a config change can take effect at the next compaction refresh; P0 must decide whether to accept that or pin the profile at session start.
- **Aggregator slugs.** Aggregator ids (`openrouter/...`, vendor-prefixed slugs) may route to a different family than their name. The resolver follows the slug, so a mis-named aggregator gets the slug's profile. Accepted; overridable through H2.
- **Per-model map not in scope.** Droid's `customModel.fileEditToolProfile` is per custom model; Hermes config is per agent. A per-model map needs a new config shape and is not part of this plan.

## Non-goals
- No freeform (`type: custom`) tool in this plan.
- No new edit tool name; `patch` stays the only edit tool with `write_file` for new files.
- No change to `tools/approval` decisions or to the permission classes of `patch` / `write_file`.
- No retuning of model family lists beyond what P1 forces; no new providers.
- No port of droid's `CreateEdit` tool; its behaviour is covered by `replace` mode.
