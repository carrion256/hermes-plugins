# 03 Sandbox attestation for Hermes terminal backends

Status: proposed · Owner: — · Depends on: 02

## Goal

Decide how Hermes should treat "this terminal environment is actually sandboxed" as evidence, not as a property inferred from the backend's class name. Compare three reference designs:

- **Droid whole-process sandbox with self-attestation** (B1–B10 in the droid notes).
- **Grok Build** per-command and process-wide sandbox plus an in-tree egress proxy.
- **Grok Bot 0.71.2** sandbox wire protocol, which is present but unwired.

Then map the useful parts onto Hermes terminal backends (`tools/environments/`, `tools/terminal_tool_backends.py`) and decide plugin vs. host placement.

Output of this plan: a phased implementation path. No code in this document.

## Evidence

All cites are read ranges. Droid cites are **note-sourced** (from `notes/arch/assets-and-sandbox.md`); they were not re-verified against the bundled code in this pass.

### Droid (note-sourced)

- `/var/lib/hermes/projects/droid/notes/arch/assets-and-sandbox.md:102-109` (B1): bootstrap in `apps/cli/src/cli.ts:52-72`. Sandbox is requested iff `enabled===true && mode==='whole-process'` (`wholeProcessSandboxSettings.ts:36-42`). Settings are local-only; invalid schema fails closed (`:84-91`).
- `:110-121` (B2): `ensureWholeProcessSandbox` is a supervisor/child split. The child validates first and, if sealed, installs a `node:net` loopback-only patch (`:1279-1310`) and a proxy-aware undici agent (`:1331-1344`). The supervisor builds a per-launch guard file (`:1801-1867`), canary files, and a process-wide SRT config (`:1306-1362`). A spoof guard rejects a `DROID_SANDBOXED=1` marker without launch material (`:1744-1749`).
- `:122-124` (B3): child validation fails closed with reasons. Checks include config-hash match, filesystem probes, a raw `node:net` connect that must fail within 1 s, proxy env must be loopback, Unix-socket creation blocked, and canary read/write probes. The network probe runs as a re-invoked child (exit 0 = blocked, 42 = leaked).
- `:126-127` (B4): control channel is an inherited fd. Child→supervisor kinds include `attest` (`wholeProcessControlChannel.ts:20-23`). Possession of the fd is the capability.
- `:129-135` (B5): brokers let the sandboxed child request credentials, org-settings cache writes, and user-settings writes from an unsandboxed supervisor.
- `:154`: failure reasons enumerated (unsupported platform, missing seccomp, broad unix sockets, no netns proof, spoofed marker, policy drift → "restart required").
- B6–B10 read this pass (`notes/arch/assets-and-sandbox.md:137-154`). Relevant here: B6 descendant children inherit sealed state via `prepareWholeProcessDescendantLaunch` (`wholeProcessDescendantLaunch.ts:26-81`), and `SandboxService.initializeOnce` fails closed on policy-hash mismatch (`SandboxService.ts:207-233`). B7 filesystem floors (`DroidSandboxManager.ts:231-343`). B8 per-command flow: pre-dispatch violation check (`sandboxPreCheck.ts:684`), then `wrapCommand` at `execute-cli.ts:884-897`, the closest analogue to a Hermes backend `execute()` seam. B9 env vars (`sandbox/constants.ts:7-18`). B10 failure modes (`:1753-1848`). These line anchors are note-sourced, not re-verified against bundled code (R6).

### Grok Build

- `re/docs/grok-build-platform.md:462-464` (§2.1): two unrelated confinement mechanisms: process-wide sandbox and per-command sandbox.
- `:483` (§2.2): Linux process-wide startup via `apply_sandbox` (`xai-grok-shell/src/config/mod.rs:1356-1511`).
- `:592-595` (§2.3): macOS nono Seatbelt; `deny` entries get explicit sub-action rules because last-match wins.
- `:623-626` (§2.4): Windows has no sandbox ("[unknown: cite unresolved]" for per-command backend detection).
- `:725-728` (§2.6.1): per-command launch seam, policy-blind tools crate.
- **Grok Build egress proxy** (`xai-grok-egress-proxy`, §2.7.1, `:788-811`): HTTP/1.1 forward proxy and `CONNECT` tunnel bound to `127.0.0.1`, one per served folder. Non-loopback peers are dropped at accept (`server.rs:33-35`). Its library doc says it "does not confine child networking" (`lib.rs:1-4`); the real boundary is the Seatbelt `network-outbound localhost:<port>` rule. It is a decider, not a confinement boundary.
- `re/docs/grok-build-platform.md:754-784` (§2.6.3): approval and replay. Per-command sandbox denials go to a user card, and a grant buys exactly one re-dispatch (`result_path.rs:198-229`). Fail closed: no transport, cancelled card, or unrecordable grant leaves the denial standing (`sandbox_gate.rs:241-252,279-286`). No unsandboxed retry path exists (`unsandboxed` grep: 0 hits in `xai-grok-tools`).
- `re/docs/grok-build-platform.md:790-797` (§2.7.1): proxy protocols are HTTP/1.1 forward and `CONNECT` only; no TLS interception. The egress-proxy cites were re-read against source on 2026-10-18 (`crates/codegen/xai-grok-egress-proxy/src/lib.rs:1-4`, `server.rs:33-35`), as was `crates/codegen/xai-grok-workspace/src/sandbox/network.rs:216-250` (`sync_network`: the proxy runs only in enforce mode and restarts if it dies).
- `:822-843` (§2.9): Grok Bot comparison. Grok Bot's helper binary is absent from 0.71.2; its policy protobuf and helper invocation are decoded but the network proxy and devices rows are **[unknown]**.

### Grok Bot (0.71.2)

- The sandbox helper is not wired in 0.71.2. `local-exec-daemon.md` item 4 (items listed at `:14-17`; `Mae()` ~1182673) returns `Vkt`, a variable declared in `local-exec-daemon/main.cjs` and never assigned there. Item 5 records `ls` failing with "Ripgrep path not configured" (`Tc()` ~1154616). `grok-build-platform.md:838` states the network policy is "unreachable in 0.71.2".
- Consequence: Grok Bot offers a protocol shape (`agent.v1.SandboxPolicy`, `grok-build-platform.md:825`) but no working reference implementation. Do not port it.

### Hermes (host, verified this pass)

- `agent/terminal_env_provider.py:1-41`: `TerminalEnvironmentProvider` ABC. Declarative classification attributes `is_remote`, `is_container`, `skip_container_guards` (defaults to `is_container`), `cache_path_base`, `strip_env_keys`, `session_isolated_when_nonpersistent`. Providers register via `PluginContext.register_terminal_environment_provider`.
- `hermes_cli/plugins.py:1071-1078`: third-party providers register under `terminal.backend`. Built-in names (`local, docker, singularity, modal, daytona, vercel_sandbox, ssh`) are **rejected**: "plugins never shadow in-tree backends." A plugin therefore cannot wrap or replace the `docker` backend; it can only add a new named backend.
- `tools/terminal_tool_backends.py:204-207`: `_ENV_BUILDERS` table for the seven built-ins.
- `tools/terminal_tool_backends.py:217-220`: `_create_environment` dispatches `_ENV_BUILDERS.get(env_type, _build_plugin_env)`. Unknown names fall through to plugin backends.
- `tools/environments/docker_egress.py:1-34`: iron-proxy egress plumbing for docker. `_egress_proxy_args_for_docker` returns volume/env/host args; under `proxy.enforce_on_docker` (default) any half-configured state raises so the sandbox refuses to start unprotected. `_PROXY_CONTROL_ENV` (`:22-26`) blocks overrides that would weaken egress. Only `ImportError` is swallowed.
- `tools/approval.py:939-957` (earlier read): `_should_skip_container_guards` skips dangerous-command prompts for isolated backends, except docker with host bind mounts. The skip decision trusts the backend class; it does **not** consult any runtime evidence that the sandbox is intact.

## Current state

- Hermes has a network-egress layer for docker only (iron-proxy). It has no runtime attestation: nothing verifies at session start that the backend is sandboxed the way its class claims.
- `skip_container_guards` is a static class attribute. A misconfigured docker with host bind mounts is already excluded by `_should_skip_container_guards`; any other drift is invisible.
- There is no per-session attestation record. `ensure_task_env` creates sandboxes lazily for non-terminal callers (issue #62825), so the first command may run before any check.
- No `VALID_HOOKS` entry fires after environment creation and before the first command. `hermes_cli/plugins.py:110-134` lists `pre_tool_call`, `post_tool_call`, `pre_llm_call`, and related hooks, and `:210-211` says there is no inert hook surface ahead of fire-sites. The list at `:135-222` was not fully read, so P0 confirms this before relying on it.

## Design

### D1. Attestation is evidence, recorded per environment

Each created environment gets an attestation record: `{backend, task_id, probes: [{name, passed, detail}], config_hash, attested_at, verdict: pass|fail|skipped}`. The record is stored alongside the environment's task state. Nothing trusts the class name alone.

### D2. Probes are the Droid B3 set, adapted to Hermes backends

Run inside the backend via its `execute()` duck type, so the same probes work for docker, ssh, singularity, and plugin backends. Droid's probes are network and filesystem only, so these are the minimum viable set:

| Probe | Passes when | Droid analogue |
|---|---|---|
| `canary_write_outside_workspace` | write to a canary path outside the workspace fails | B3 canary write probe |
| `canary_read_denied` | read of a deny-listed canary fails | B3 canary read probe |
| `raw_connect_blocked` | direct TCP connect to a public IP fails within 1 s | B3 raw `node:net` probe |
| `proxy_env_loopback` | `HTTP(S)_PROXY`/`ALL_PROXY`, when set, point to loopback | B3 proxy env check |
| `config_hash_stable` | policy hash recorded at create equals hash at probe time | B3 `config_hash_mismatch` |

Probes that cannot run (missing tool in the image, backend unreachable) yield `skipped`, never `pass`. A run with `skipped` on any probe the policy requires is treated as `fail` in enforce mode.

Droid's netns proof and seccomp checks are Linux-only host properties; they do not translate to arbitrary backends and are out of scope.

### D3. Attestation gates approval skipping, not execution

The verdict feeds one decision: `_should_skip_container_guards`. Under `sandbox.attest: enforce`, a backend whose verdict is not `pass` loses the container-guard skip and falls back to normal dangerous-command approval. Execution itself is not blocked by attestation; approvals are. This keeps the change narrow and reversible.

Rationale: this reuses the existing approval path (see plan 02) instead of adding a second blocker. It also means an unattested environment is no less safe than a non-container backend today.

### D4. Placement: plugin for policy and probes, one host seam for timing

- **Plugin** (`plugins/sandbox-attest/`): probe definitions, `sandbox.attest` policy (`off|shadow|enforce`), attestation record storage, CLI `sandbox-attest status|run`, and the `TerminalEnvironmentProvider`-independent probe runner.
- **Host seam** (H1): a call from `_create_environment` (`tools/terminal_tool_backends.py:217-220`) to run probes after the environment is constructed and before `ensure_task_env` returns. Without it the plugin cannot see built-in backend creation, because built-in names cannot be shadowed (`plugins.py:1074-1078`).
- **Host change** (H2): `_should_skip_container_guards` (`tools/approval.py:939-957`) reads the verdict. This is a one-line condition, guarded by the policy value.

Rejected: a plugin that wraps the docker backend. The registry forbids it.
Rejected: a new built-in backend named `attested-docker`. That duplicates `docker` and splits configuration.

### D5. Egress: keep iron-proxy; do not port Grok Build's proxy

Hermes already has a docker egress proxy with fail-closed semantics (`docker_egress.py:29-34`). Grok Build's in-tree proxy (§2.7) solves the same problem for a different architecture (per-command, per-folder). Porting it adds a proxy the Hermes process must run and a second policy source. Non-goal.

Attestation D2 checks that egress is in effect (`raw_connect_blocked`, `proxy_env_loopback`). It does not replace iron-proxy.

### D6. Whole-process sandboxing is not a Hermes concept

Droid's whole-process model (supervisor + sealed child + control channel + brokers) assumes Hermes can re-exec its own agent under a sandbox. Hermes runs the agent in-process and sandboxes commands, not itself. Adopting the whole-process model would be a rewrite of the agent entrypoint. Out of scope; record as a rejected option.

Droid's control channel and broker pattern (B4–B5) also does not apply. Hermes resolves secrets host-side at startup through registered secret sources (`plugins.py:1079-1085`: `load_hermes_dotenv()` runs them after `~/.hermes/.env`, before credentials are read, gated by `secrets.<name>`). The sandboxed child never receives a broker fd, so there is no runtime request channel to attest. `[INFERENCE]` that no runtime credential broker exists in Hermes; not verified beyond that range.

### D7. Fail-closed where the sandbox is the boundary

`docker_egress.py:29-34` already refuses to start an unprotected docker sandbox when `proxy.enforce_on_docker` is set and proxy config is half-applied. Attestation follows the same rule: in `enforce` mode, an environment whose probes cannot run is treated as unattested. It is not silently passed.

## Host changes

- **H1** (required for D4): one call in `_create_environment` to run attestation after construction. Exact placement depends on whether `ensure_task_env` (`tools/terminal_tool_lifecycle.py:184-262`) is the right caller for lazily created environments; P0 decides.
- **H2** (required for D3): `_should_skip_container_guards` (`tools/approval.py:939-957`) consults the attestation verdict when `sandbox.attest` is `enforce`.
- No change to `TerminalEnvironmentProvider` (`agent/terminal_env_provider.py`). Third-party backends get probes for free via the seam.
- No change to `plugins.py`. Built-in-name rejection stays.

## Phases

**P0 — Verify seams (no code).**
1. Read `VALID_HOOKS` in `hermes_cli/plugins.py` to confirm whether an existing hook fires after environment creation. If one does, H1 becomes a plugin hook and the host change shrinks to H2 only.
2. Read `ensure_task_env` (`tools/terminal_tool_lifecycle.py:184-262`) and `_teardown_env` (`:103`) to confirm the creation and teardown points for lazily created environments.
3. Read `tools/environments/base.py:489-520` to confirm `execute()` return shape for probe use.
4. Read `tools/terminal_tool_backends.py:223-233` for requirement checkers, to see how a probe can report "backend unavailable" as `skipped`.
5. Record findings in this plan's Risks section before P1.

**P1 — Probe runner in the plugin.** Plugin `sandbox-attest` with the five D2 probes, run through `execute()`, producing the record from D1. No host change. Tests run the probes against the local backend (which should fail `raw_connect_blocked` by design, so verdict `fail` is the expected outcome there).

**P2 — Shadow mode with the host seam (H1).** Verdicts are logged but not enforced. Shadow logs are compared against the existing approval decisions for one week of normal use before enforcement.

**P3 — Enforcement (H2).** `sandbox.attest: enforce` makes `_should_skip_container_guards` consult the verdict. Default remains `off` until P2 data is reviewed.

**P4 — CLI and docs.** `sandbox-attest status` lists per-task verdicts. Docs state that attestation gates approval skipping only.

## Tests

- Probe unit tests with a fake `execute()` returning canned exit codes: a pass case where every probe fails as expected, and a fail case where `raw_connect_blocked` succeeds. Assert the record verdict.
- `skipped` handling: an unavailable tool yields `skipped`; under `enforce` with a required probe, the verdict is `fail`.
- H2 test: with a fake `fail` verdict and `enforce`, `_should_skip_container_guards` returns False for `docker` with no bind mounts. With `off`, existing behaviour is unchanged (regression lock).
- Build the test on the existing `tests/conftest.py` no-network guard (`tests/conftest.py:27-30`) so probes cannot accidentally make real connections.
- Negative control: remove the probe (or force it to pass) and assert the verdict changes. This is the "prove the guard discriminates" check.

Run the full test module once after P3, not only the new test.

## Risks

- **R1. P0 may invalidate H1.** If an existing hook already fires after creation, the host change shrinks. Left open until P0.
- **R2. Probe false positives.** `raw_connect_blocked` depends on network topology. A backend on a network with an allowed public egress path fails attestation legitimately. Shadow mode (P2) exists to find these before enforcement.
- **R3. Probe cost.** Each probe is a command execution inside the sandbox. Five probes per environment creation adds latency; run once per environment, not per command.
- **R4. Side effects of canary writes.** Canary write probes must use a path the backend can create and remove, and must clean up on failure. P1 must test cleanup.
- **R5. Grok Bot is not a reference.** Its helper is absent in 0.71.2 (`grok-build-platform.md:838`). Any claim derived from its protocol fields is design input, not verified behaviour.
- **R6. Droid evidence is note-sourced.** B1–B5 line numbers come from notes, not from a code re-read in this pass. Re-verify against the bundled source before citing externally.
- **R7. Approval coupling.** D3 ties attestation to plan 02's approval path. If plan 02 changes the approval gate order, D3 must be re-checked.

## Non-goals

- Porting Grok Build's in-tree egress proxy (D5).
- Porting Droid's whole-process sandbox, supervisor, control channel, or brokers (D6).
- Wrapping or replacing built-in backends through plugins (forbidden by `plugins.py:1074-1078`).
- Blocking execution on attestation failure. Attestation gates approval skipping only.
- Netns, seccomp, and bwrap checks, which are Linux-host properties of Droid's supervisor.
- Changing iron-proxy behaviour or `docker_egress.py` policy.
