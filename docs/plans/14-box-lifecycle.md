# 14 box-lifecycle (host seams + `box-lifecycle` plugin)
Status: proposed · Owner: — · Depends on: host seams below; 13 (conversation plane carries box state frames)

## Goal
Give Hermes a single, explicit lifecycle model for the sandbox that runs agent work: absent, starting, running, hibernated. Operations are `describe` (non-waking), `ensure(wake)`, `snapshot`, `restore`, and `recreate(preserve_data)`. Grok Bot already separates these concerns. Hermes today has per-backend persistence (Docker bind mounts, Vercel snapshot ids) with no shared vocabulary, no consent gate on data loss, and no non-waking status probe.

The plugin exposes read-only status and shadow-mode decisions. Preservation, consent, and snapshot storage stay in host code, because they decide whether user data survives.

## Evidence
- Grok Bot run-state and lifecycle: `re/docs/coordinator-and-box.md:253` (§3.2 run-state section). Enum `SandBoxRunState` ABSENT / HIBERNATED / RUNNING / STARTING, documented in §3 (`:244-279`).
- Grok Bot `connect` wakes, `locate` returns asleep or running, `describe` is non-waking and returns running, hibernated, starting, or absent: `coordinator-and-box.md:276`.
- Grok Bot route-cache TTL of 7 days with stale-while-revalidate: `coordinator-and-box.md:279`.
- Grok Bot recreate contract `RecreateSandBox{preserve_data, force, acknowledge_terminal_upgrade_schedule}`, `ForceRecreateSandBox{}` documented as data loss: `coordinator-and-box.md:281-282`.
- Grok Bot migration phases BACKING_UP, CREATING, MOVING, CLEANING_UP, WIPING, DONE or FAILED, with a Mermaid diagram stating FAILED leaves Idle only via dismiss or ForceRecreate: `coordinator-and-box.md:284` and `:288-306`.
- Grok Bot scheduled upgrades and their states: `coordinator-and-box.md:308-326`.
- Grok Bot durable box store as content-addressed objects plus a versioned manifest with fencing (`COMMITTED`, `RETRY`, `FENCED`): `coordinator-and-box.md:328-335`.
- Grok Bot presence and park policy, with explicit idle inputs and a 10-minute away threshold: `coordinator-and-box.md:337-341`. (`[decoded]` per source doc.)
- Hermes base class for execution environments: `tools/environments/base.py:142-150` (`BaseEnvironment`).
- Hermes Docker persistence: `tools/environments/docker.py:694-712`. In persistent mode the home and workspace directories live under `get_sandbox_dir()/docker/<sanitized task_id>` and bind to `/root` and `/workspace`. Non-persistent mode uses `--tmpfs` for `/workspace` (10g), `/home`, and `/root` (1g each).
- Hermes Vercel snapshot store: `tools/environments/vercel_sandbox.py:90-108` (`_snapshot_store`, `_store_snapshot`, `_delete_snapshot` with a compare-before-delete guard). Restore on create: `:170-180`, which falls back to a fresh sandbox and deletes the stale id on failure. Snapshot before stop: `:238-250` and `:332-336` (cleanup snapshots, then stops).
- Hermes Docker egress module exists (`tools/environments/docker_egress.py`), not read this session.
- Hermes egress redaction single choke point: `agent/redact.py:807` (`redact_for_egress`).

## Current state
- Hermes has no shared lifecycle vocabulary. Persistence is per backend: Docker by bind-mount directories (`docker.py:694-712`), Vercel by a JSON snapshot map keyed by task id (`vercel_sandbox.py:90-108`).
- Vercel already implements the core pattern (snapshot, restore, fallback, compare-before-delete). Its store is Vercel-specific: the file is `vercel_sandbox_snapshots.json` under `HERMES_HOME` (`:90-91`).
- Docker persistence has no snapshot. Its data survives only because the host directory persists. Recreating the container preserves data by construction, and there is no separate "preserve" decision.
- No non-waking status probe exists for environments. Callers learn state only by trying to use the environment.
- No consent gate separates "recreate and keep data" from "recreate and discard data." Vercel's restore-failure path silently falls back to a fresh sandbox (`:177-180`), which discards state without asking.
- Grok Bot treats data loss as an explicit flag (`force`, `ForceRecreateSandBox`). Hermes has no equivalent.

## Design

### State model
| Hermes meaning | Grok Bot analogue | Docker | Vercel |
|---|---|---|---|
| absent | ABSENT | no container, no sandbox dir | no sandbox, no snapshot |
| starting | STARTING | container created, not ready | sandbox creating or restoring |
| running | RUNNING | container up | sandbox up |
| hibernated | HIBERNATED | container stopped, dirs kept | no sandbox, snapshot present |

Docker has no native hibernate. Stopping the container while keeping its bind-mount directories is the closest equivalent [INFERENCE: needs a probe before claiming parity].

### Host seam H1: `EnvironmentLifecycle` on `BaseEnvironment`
Add four methods to `BaseEnvironment` (`base.py:142-150`):
- `describe() -> LifecycleState`: non-waking. It must not create, start, or restore anything.
- `ensure(wake: bool) -> LifecycleState`: creates or restores when `wake` is true. With `wake` false it returns the state and never mutates.
- `snapshot() -> str | None`: returns a backend snapshot id or `None` when the backend cannot snapshot. Docker returns `None` because its directories already persist.
- `restore(snapshot_id)`: used only by `recreate`.

`LifecycleState` is a small enum with the four values above. It goes in the host, not in a plugin.

### Host seam H2: shared snapshot store
Generalize the Vercel store pattern (`vercel_sandbox.py:90-108`) into one host-owned store:
- File: `HERMES_HOME/environment_snapshots.json`, keyed by `{backend, task_id}`.
- Operations mirror Vercel: store, load, delete-if-equal.
- Vercel migrates to it. The old `vercel_sandbox_snapshots.json` is read once and then removed. No dual-read after that, per the clean-cutover rule.

### Host seam H3: recreate with explicit consent
`recreate(task_id, preserve_data: bool, force: bool)`:
1. If `preserve_data` is true and `snapshot()` returns `None` while the backend is not a directory-persistent one, refuse. Do not silently drop data.
2. Snapshot, stop, create, restore.
3. On restore failure, do not fall back to a fresh sandbox unless `force` is true. Without `force`, return `rejected{reason=restore-failed}` and leave the old snapshot in place.
4. `force=true` is the only path that discards data, matching Grok Bot's `ForceRecreateSandBox`.

This replaces the silent fallback at `vercel_sandbox.py:177-180`. Behavior changes for Vercel: a restore failure becomes an error the caller sees, not a quiet data loss. That is the intended posture change.

### Host seam H4: idle policy (default off)
An idle hibernate policy with inputs modeled on Grok Bot's park policy (`coordinator-and-box.md:337-341`): idle duration and task activity. Default off. In shadow mode it logs would-hibernate decisions and takes no action. Presence-based inputs (focus, suspend, lock) do not apply to Hermes. Hermes is not a desktop app. Only task-activity inputs are in scope.

### Plugin: `box-lifecycle`
- Read-only tool `box_state`: calls `describe()` for the current task environment and returns `{state, backend, snapshot_present}`. It never calls `ensure`, `snapshot`, or `restore`.
- Shadow-mode event: when H4 is enabled in shadow mode, the plugin observes would-hibernate decisions from the host and reports them through its tool. It does not change host state.
- No plugin owns storage, timers, or consent. Those are host responsibilities.
- Hook choice: none needed beyond `register_tool`. The plugin does not subscribe to `post_tool_call_settled` for lifecycle, because lifecycle changes are host-driven, not tool-driven.

### Egress and secrets
Snapshots of a sandbox can contain credentials written into the home directory. Rules:
- Snapshot contents are never sent to a model or a renderer. `box_state` returns metadata only.
- Anything that leaves the process for egress goes through `redact_for_egress` (`agent/redact.py:807`).
- Secrets are copied into a sandbox only on explicit egress opt-in, which is a host config flag, not a plugin decision. Grok Bot keeps the egress tunnel as a separate host-owned service (`coordinator-and-box.md:244-279`, ports 8790 and 8791). Hermes equivalent: `docker_egress.py` [INFERENCE: not read this session].

## Host changes
- `tools/environments/base.py`: add `LifecycleState` and the four lifecycle methods with default "not supported" behavior. Only backends that implement them report capability.
- `tools/environments/docker.py`: implement `describe` from container and directory presence. `snapshot` returns `None`. `recreate` preserves the bind-mount directories by construction.
- `tools/environments/vercel_sandbox.py`: move `_snapshot_store`, `_store_snapshot`, and `_delete_snapshot` to the shared store (H2). Replace the silent fallback at `:177-180` with H3.
- New host module for the shared snapshot store and recreate orchestration. Location: `hermes_cli/` or `tools/environments/` [INFERENCE: pick the location that matches the existing `remote_common.py` pattern once read].
- `plugins/box-lifecycle/`: `box_state` tool only.
- Config: `environments.idle_hibernate` with values `off` (default) or `shadow`. No `on` in this plan.

## Phases
1. **P0 read-only**: `LifecycleState` and `describe()` for Docker and Vercel. `box_state` tool. No mutation anywhere. Verification: `describe` leaves the store file and the container list unchanged.
2. **P1 shared store**: H2. Migrate Vercel to the shared file. Remove the old file after one read. Verification: an existing `vercel_sandbox_snapshots.json` is imported once and deleted.
3. **P2 recreate with consent**: H3. Remove the silent fallback. Verification: a forced restore failure returns `rejected` and the snapshot survives.
4. **P3 idle shadow**: H4 in `shadow` mode only. Log would-hibernate decisions for two weeks [INFERENCE: window length is a guess]. No `on` mode in this plan; a separate plan must justify turning it on.

## Tests
- `describe()` is non-mutating: call it on Docker and Vercel fixtures, assert no container creation, no snapshot write, no file change.
- Snapshot store round-trip: store, load, delete-if-equal refuses to delete a newer id.
- Restore failure without `force` returns `rejected` and keeps the snapshot. With `force`, it creates a fresh sandbox and deletes the old snapshot id.
- Docker persistent and non-persistent mount sets match `docker.py:694-712`.
- Migration: the old Vercel file is imported once and removed.
- `box_state` output contains no snapshot contents and no environment variables.
- Run the full test module for the environments package, not only the new tests.

## Risks & open questions
- **Docker hibernate parity** is `[INFERENCE]`. Stopping a container may not match Grok Bot's hibernate semantics for in-flight processes. Probe before claiming parity.
- **Behavior change on Vercel restore failure**: from silent fresh sandbox to a visible `rejected`. Some callers may depend on the fallback. Grep all call sites of the Vercel create path before P2 lands.
- **Snapshot size and retention**: Grok Bot uses a versioned manifest with fencing (`coordinator-and-box.md:331`). Hermes has no fencing; two processes writing the shared store could race. Use compare-before-delete (already in Vercel) for now. Fencing is a separate decision.
- **Credentials inside snapshots**: a home directory snapshot can contain tokens. Retention and encryption at rest are unresolved. Egress redaction covers outbound text only.
- **Grok Bot upgrade schedules** (`coordinator-and-box.md:308-326`) have no Hermes analogue. Image upgrades for Hermes environments are out of scope here.
- **Grok Bot park policy inputs** (presence, focus, lock) do not map to a headless agent. Only idle time is in scope.
- **Docker egress module** (`docker_egress.py`) was not read. Egress claims above are provisional.
- **Naming**: `LifecycleState` and `box_state` are proposed names. Neither exists in the codebase.

## Non-goals
- Replacing the Vercel, Docker, Modal, or SSH backends. This plan adds a lifecycle surface to them.
- Grok Bot image upgrades, host upgrades, or team-admin sandbox operations.
- Presence-based parking or desktop focus detection.
- A plugin-owned timer, scheduler, or snapshot store.
- Changing the in-box protocol or the Grok Bot wire format.
- Turning idle hibernate on by default.
