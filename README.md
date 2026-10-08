# hermes-plugins

Hermes Agent plugins ported from the DSH plugin workspace (`carrion256/dsh-plugins`).
Each port maps a DSH concept onto an existing Hermes plugin seam — no core forks.

## Layout

```
plugins/<name>/
├── plugin.yaml      # manifest v2 (config_schema, capabilities, provides_*)
├── __init__.py      # exposes register(ctx)
└── ...
```

Install for local development (symlink into the user plugin dir):

```sh
ln -s /home/common/projects/hermes-plugins/plugins/<name> ~/.hermes/plugins/<name>
```

Hermes discovers any directory under `$HERMES_HOME/plugins/` with an `__init__.py`
exposing `register(ctx)` (plus `plugin.yaml`). Validate and inspect with:

```sh
hermes plugins validate plugins/<name>
hermes plugins list
hermes plugins doctor
```

## Gap matrix (Oct 2026 audit)

| DSH plugin | Hermes seam | Status | Notes |
|---|---|---|---|
| dsh-browser-bladebro | `register_tool` (new `bladebro` toolset over the bladebro CLI `--json`) | ✅ done | tools stay registered, `check_fn` gates dispatch; bladebro binary absent on host so tool is dormant |
| dsh-keyword-steering | `pre_llm_call` → `{"context": ...}` injection | ✅ done | config-driven whole-word table, hot path regex-only |
| dsh-papercuts | `register_tool` + `register_cli_command` | ✅ done | wraps native binary, all 5 modes |
| dsh-tool-docling | `register_tool` wrapping docling CLI | ✅ done | `docling_convert`, returns text + durable file path |
| dsh-tool-fff | `register_tool(..., override=True)` | ✅ done | registers `fff_search`; `search_files` override is opt-in (`override_search_files: true` + consent gate) — validator refuses undeclared built-in collisions |
| dsh-auto-review-classifier | `tool_execution` middleware (short-circuit = deny) | ✅ done | read-only tools skip screening; classifier errors fail open |
| dsh-continuation | `post_api_request` capture + `on_session_end` decide + `inject_message` | ✅ done | no GoalProvider registry in 0.21.2 — landed on hooks instead; capped steers, /stop respected |
| dsh-prewalk | `prewalk_arm` tool + `llm_request` model rewrite + `pre_llm_call` nudge | ✅ done | model swap honors mid-session (payload applied per API call); target model must share endpoint/provider |
| dsh-loop-guard | `post_tool_call` + `post_api_request`, steer via `inject_message` | ✅ done | turn-level detectors only — streaming Z-algorithm detectors skipped (no inject channel into an in-flight stream) |
| dsh-rules | `pre_llm_call` (always-apply) + `tool_request` middleware (tool-matched) | ✅ done | rule files = `*.md` with `applies:`/`tools:` front matter |
| dsh-subagent-effort | `subagent_start` capture + `llm_request` effort rewrite | ✅ done | identity VERIFIED: middleware gets `session_id`; `subagent_start` carries `child_session_id`/`child_goal` (delegate_tool.py:287) |
| dsh-auto-answer-classifier | `post_tool_call` on `clarify` timeout + `inject_message` | ✅ done | two classifier passes must agree on the recommended option; disagreement leaves the question open |
| dsh-jump | (web-GUI overlay per element) | ❌ skip | Desktop Plugin SDK too coarse |
| dsh-settings-panels | `config_schema` in plugin.yaml | ❌ skip | generic validated form exists natively; custom panels don't |
| dsh-task-prewalk | — | N/A | empty dir; plan doc for dsh-prewalk |

## Seams available (Hermes 0.21.2 / manifest v2)

- Hooks: `pre_tool_call`, `post_tool_call`, `pre_llm_call` (context injection),
  `transform_tool_result`, `on_stream_delta`, `on_session_start/end`, `subagent_start/stop`,
  `pre_gateway_dispatch`, `pre_verify`, and more (`hermes_cli/plugins.py:VALID_HOOKS`).
- Middleware: `tool_request`, `tool_execution`, `llm_request`, `llm_execution`
  (`hermes_cli/middleware.py`). Execution kinds can short-circuit via `next_call`.
- Tools: `ctx.register_tool(..., override=True)` — built-in override gated by
  `plugins.entries.<id>.allow_tool_override`.
- GoalProvider: `register_goal_provider(name, provider)` selected via
  `auxiliary.goal.provider` (judge + continuation prompt).

## Conventions

- Fail-open for observers; fail-loud for schema validation.
- No secrets in plugin config — env-var **names** and file paths only.
- `hermes plugins validate --ci <dir>` must pass before shipping.

## Deployment note (NixOS-managed installs)

`hermes plugins enable <name>` prints ✓ but cannot save on managed installs
("Cannot save configuration: this Hermes installation is managed by nixos") —
the plugin stays "not enabled". Add the enablement (and any
`plugins.entries.<id>.allow_tool_override` / `allow_gateway_injection` grants)
to the NixOS-managed config instead. Verified 2026-10-08.
