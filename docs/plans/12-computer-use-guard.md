# 12 computer-use-guard (`tools/computer_use/` hardening)
Status: proposed · Depends on: 02 (approval policy), 11 (machine-relay, only for the `local_tool` relay path). Labels: **Observed** = read in source/docs this session; **Decoded** = from the grokbot-re reverse-engineering notes; **Inferred** = design judgement or unverified platform behaviour.

## Goal
Hermes' `computer_use` tool drives the user's desktop through a `cua-driver` subprocess. Today its guards are: hard-blocked `type` text patterns, an approval gate for destructive actions, a sticky-target mismatch refusal, and response caps. It has **no app/bundle denylist**, no per-target identity beyond the sticky app name, and no guard that holds once `--yolo` disables approval prompts or once a call leaves the Hermes process (plan 11). This plan adds a deny policy, stable target identity, bounded captures, and an audit record, so that a model-driven desktop action cannot reach a terminal, a script runner, a password manager, system settings, or the agent itself without an explicit, logged decision.

## Evidence
- **Hard blocks run before approval** — `tools/computer_use/tool.py:57-68` `_reject_unsafe(action,args)` checks `_BLOCKED_TYPE_PATTERNS` (defined `tool.py:49-51`) on `type` text and returns a JSON error *before* the approval prompt (Observed). Key canonicalisation splits on `+` and `-` so `ctrl-alt-delete` cannot bypass the combo block (`tool.py:53-55`, Observed).
- **Sticky target** — input actions go to the backend's last capture/focus target. `tool.py:70-75` `_input_target_mismatch(backend, requested_app)` compares `backend._last_app` to `requested_app` by substring; unknown target → `None` (fail open, documented at `tool.py:71-72`). The dispatcher refusal `code:"input_target_mismatch"` and its "call capture(app=…) first" text are at `tool.py:386-393` (Observed). Matching is by name only: `pid` and bundle id are not compared.
- **Approval dispatch** — `tool.py:239` `handle_computer_use`; the documented order (`tool.py:240-242`) is hard blocks → approval → backend → dispatch under the session call lock (`tool.py:247-264`, Observed). Approval scopes are the destructive action plus `bring_to_front` (`tool.py:249-253`). `_request_approval` (`tool.py:269`) delegates to `tools.approval._run_approval_gate` with scope key `cua:<action>:<mode>` (`tool.py:275-280`, Observed). Destructive marking: `_ActionSpec` (`tool.py:344`), `_input = partial(..., destructive=True)` (`tool.py:346`), `_ACTIONS` (`tool.py:348`, Observed).
- **Bypass removes prompts** — `_cua_permission_mode(session_id)` (`tool.py:93-119`) returns `unrestricted` when `--yolo`/`-z` approval bypass is active, disabling runtime approval prompts and the driver's residual ceilings (`tool.py:107-118`, Observed). Hence a guard that lives only behind approval is bypassed under `--yolo`.
- **Child env** — `permissions.py:22-32` `_child_env()` strips provider secrets via `sanitized_cua_driver_env()`, but on **import error it returns `dict(os.environ)`** (`permissions.py:31-32`, Observed). The stripping is fail-open; this plan fixes it as part of P1.
- **Element and vision caps (the host's real caps)** — `_DEFAULT_MAX_ELEMENTS = 100` (`tool.py:432`); `_MAX_ELEMENT_LABEL_CHARS = 120` (`tool.py:437`); full tree spilled via `_spill_elements_to_file` (`tool.py:627-634`); `_MAX_VISION_DIM = 1456` with `_shrink_capture_for_vision` returning a scale note (`tool.py:637-640`, Observed). Spill and capture file counts are capped at 20 (`tool.py:439`).
- **No 2500-node cap in host** — a grep for `2500` under `tools/computer_use/` returns nothing (Observed). The AX limits in the grokbot notes are reference values only (see Design §6).
- **No denylist** — `_BLOCKED_APP` finds nothing under `tools/computer_use/` (Observed). Any app can be targeted today.
- **Plugin hook order** — `pre_tool_call` fires before `registry.dispatch` (`website/docs/developer-guide/tools-runtime.md:140-143`, Observed). Its directive actions are `block` / `approve` and the `post_tool_directive` observer documents `approval_gate` (`hermes_cli/plugins.py:110-129`, Observed). Whether a hook can **rewrite args** was not confirmed (**Inferred: no**; plan assumes block/approve only).
- **Grokbot reference (Decoded)** — the official client's computer-use path uses a single `app` string plus element indices; the daemon re-checks the allow decision locally and never trusts the server's grant alone (local-exec-daemon.md §5.2, Decoded).
- **Linux accessibility** — cua-driver's Linux backend behaviour (AT-SPI availability, app identity, focus semantics) was **not verified**. Every Linux statement below is **Inferred**.

## Current state (summary)
| Guard | Where | Covers | Gap |
|---|---|---|---|
| `_reject_unsafe` type patterns | tool.py:49-68 | `type` text | no app context; any app |
| Combo canonicalisation | tool.py:53-55 | `key` combos | — |
| Destructive approval | tool.py:249-280 | destructive actions | approves *any* target; removed by `--yolo` |
| Sticky-target mismatch | tool.py:70-75, :386-393 | name-substring input | no pid/bundle identity; fail open on unknown |
| `permission_mode` | tool.py:93-119 | driver mode | not per app; yolo → `unrestricted` |
| Child env scrub | permissions.py:22-32 | provider secrets | fail-open on import error |
| Element/vision caps | tool.py:432-640 | context size | no byte cap on text payload |

## Design
**Decision 1 — guard in the host, not in cua-driver.** The deny policy lives in Hermes (`tools/computer_use/guard.py`, new), evaluated inside `handle_computer_use` after `_reject_unsafe` (`tool.py:247`) and before the approval loop (`tool.py:251`). It runs regardless of approval mode, so `--yolo` does not disable it. Reasons: (a) the cua binary is third-party, (b) approval and audit already live in Hermes. The guard is only as good as target resolution, so it **fails closed** on unresolved targets for input actions.

**Decision 2 — deny by identity, not by name.** Target identity is `target_id = pid-start-sha256(exe)[:8]` (Inferred: pid + process start time + executable hash, so a recycled pid or renamed binary yields a different id). Deny rules match `(app_name, bundle_id, exe_basename)` after normalising case and stripping `.app`/`.exe`. Name-only rules are an extra signal, never the sole allow.

**Decision 3 — deny list (default, user-extensible; all Inferred, none verified against cua-driver identities).**
- Terminals: `Terminal`, `iTerm2`, `Konsole`, `gnome-terminal`, `xterm`, `kitty`, `alacritty`, `WezTerm`, `Windows Terminal`, `cmd.exe`, `powershell.exe`, `pwsh`.
- Script runners / shells: `bash`, `zsh`, `fish`, `python`, `node`, `osascript`, `Script Editor`, `Automator`.
- Password managers: `1Password`, `Bitwarden`, `KeePassXC`, `Keychain Access`, `Seahorse`, `pass`.
- System settings: `System Settings`, `System Preferences`, `gnome-control-center`, `systemsettings5`, `ms-settings`.
- The agent itself: the Hermes process (`exe` equal to `sys.executable`) and any window whose title contains `hermes`.

**Decision 4 — per-action behaviour.**
- `capture`: allowed; for a denied app the result is `{"ok":false,"code":"app_denied","app":"…"}` and no screenshot is written to cache (`_cache_file`, `tool.py:597-607`).
- Input actions (`click`, `type`, `key`, `scroll`, `drag`, `focus_app`, `bring_to_front`): require a resolved, allowed `target_id`. Unresolved → `code:"target_unresolved"`. The existing name check (`tool.py:70-75`) is unchanged; the new check is additive.
- Denied target → `code:"app_denied"` returned **before** approval is offered. No "approve anyway" path; the user changes the deny list in config.

**Decision 5 — plugin hook vs host seam.** The guard is a host check in `handle_computer_use`, plus an optional `pre_tool_call` plugin hook that returns `{"action":"block","message":…}` for clear name matches, so the refusal is visible to other plugins. Because arg rewriting is unconfirmed (Inferred), target resolution is not done in the hook. Host seam: a `resolve_target` method on `ComputerUseBackend` (`tools/computer_use/backend.py`), signature `resolve_target(app: str | None) -> TargetInfo | None`, where `TargetInfo = (target_id, app_name, bundle_id, exe_basename, pid)`. The base implementation returns `None`, so unknown backends fail closed for input actions. Backend instance comes from `_get_backend` (`tool.py:159`). The sticky-target fields it must reconcile are `_active_pid`, `_active_window_id`, `_last_app`, `_last_target` (`cua_backend.py:234-237`, Observed), reset together at `cua_backend.py:304-305`; `resolve_target` must match on pid, not only on name.

**Decision 6 — captures.** Keep `_DEFAULT_MAX_ELEMENTS`, label clip and spill-to-file (Observed). For the **relayed** path only (plan 11 `local_tool`), add a screenshot ceiling of 1280×800 and a JPEG quality ladder 85→70→55 until ≤1 MiB, and AX text caps of 2500 nodes, depth 40, 80-char labels, 36 KB total, with overflow spilled, never silently dropped. These numbers are the grokbot reference values (Decoded); Hermes-side values are Inferred until P3 measures them.

**Decision 7 — audit.** Each guard decision (allow, deny, unresolved) appends one JSON line to `$HERMES_HOME/cache/computer_use/guard.jsonl`: `{ts, session_id, action, target_id, app_name, decision, code}`. No screenshot bytes and no typed text (only its length). Rotation reuses the `_cache_file(pattern, cap)` pattern (`tool.py:597-607`, Observed).

**Decision 8 — relay parity (plan 11).** A `local_tool` frame carries `{action, args, target_id}`. The daemon runs the same `guard.check` with its own copy of the rule set and refuses denied calls even if the server approved, per local-exec-daemon.md §5.2 (Decoded).

## Host changes
1. `tools/computer_use/guard.py` (new): `DENY_DEFAULTS`, `load_rules(config)`, `check(target, action) -> Decision`. Pure apart from the audit append.
2. `tools/computer_use/tool.py`: call `guard.check` in `handle_computer_use` between `_reject_unsafe` (`:247`) and the approval loop (`:251`).
3. `tools/computer_use/backend.py`: add `resolve_target` to the ABC, default `None`.
4. `tools/computer_use/cua_backend.py`: implement `resolve_target` from cua-driver's app listing (Inferred; check real listing output before coding).
5. `tools/computer_use/permissions.py:31-32`: change the import-error fallback from `dict(os.environ)` to an empty env plus `PATH`/`HOME`, so secrets are never inherited.
6. Plugin `plugins/computer-use-guard/` (new): `pre_tool_call` block for name matches. No arg rewriting.

## Phases
- **P0 — audit only.** Implement `guard.check` and the audit log; run log-only (never blocks). Accept: `guard.jsonl` has one decision per `computer_use` call in a test session; existing `tests/tools/test_computer_use*.py` pass unchanged.
- **P1 — enforce deny by name + fail-closed env.** Turn on `enforce: true`; fix `permissions.py:31-32`. Accept: `capture(app="Terminal")` returns `app_denied` and writes no file; `click` with unresolved target returns `target_unresolved`; with a fake approval callback, denied calls invoke it 0 times; with `--yolo` active, denial still applies; import-error env contains no `*_API_KEY` variables.
- **P2 — identity from cua-driver.** Implement `CuaDriverBackend.resolve_target`. Accept: two windows with the same title but different pids resolve to different `target_id`; a recycled pid with a different exe resolves differently.
- **P3 — captures and relay parity.** Relay screenshot ladder and AX caps; daemon-side deny for `local_tool` (plan 11 P4). Accept: screenshot ≤1 MiB at 1280×800; AX with 3000 nodes yields ≤2500 inline plus full spill; daemon refuses a denied `local_tool` the server approved.
- **P4 — Linux verification.** Verify cua-driver's Linux app identity fields, then tune defaults. Accept: a recorded fixture from a real Linux session yields the same deny decisions as macOS for the default list. Until then Linux ships with `enforce: false`.

## Tests
- `tests/tools/test_computer_use_guard.py`: unit tests of `check()` over deny forms (name, bundle, exe, case, `.app` suffix), unresolved fail-closed, audit line shape (no text, no bytes).
- Integration: real `handle_computer_use` with a fake backend implementing `resolve_target`; asserts ordering (guard before approval; approval callback count 0 on denial) and that denial holds under yolo.
- Regression: `_reject_unsafe` and `_input_target_mismatch` tests unchanged.

## Risks & open questions
- **Name spoofing**: an app can claim any title. Identity (P2) mitigates; name-only rules are bypassable. Stated limit.
- **Hook cannot rewrite args (Inferred)**: if a future hook API adds rewriting, the plugin can be simplified; the host check stays authoritative.
- **Fail-closed input** may block legitimate Linux work if AT-SPI identity is thin. Mitigation: `enforce: false` on Linux until P4.
- **Deny list is advisory against a determined model**: the model can still type into an allowed app to open a terminal. The guard limits direct targeting, not indirect routes.
- **Static deny list** is a ponytail shortcut: `# ponytail: static deny list; ceiling = unknown apps slip through; upgrade = per-platform category from OS metadata.`
- Open: should `bring_to_front` on an allowed app still prompt when a user has `always` for `cua:focus_app`? Proposed: yes; the guard adds denial and never bypasses approval.

## Non-goals
Semantic classification of screen content, OCR-based secret detection, sandboxing cua-driver itself, changing `permission_mode` semantics, Linux behaviour claims beyond the P4 fixture, any change to the approval store's grant format.
