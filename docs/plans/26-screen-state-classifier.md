# 26 screen-state-classifier (login-or-password / payment / destructive-dialog / system-settings / normal)
Status: proposed · Owner: — · Depends on: 12 (computer-use-guard: consumes this label for its deny and approval decisions), 25 (shares untrusted-text handling of DOM/AX content), 02 (approval grants). Labels: **Observed** = read in source this session; **Decoded** = from grokbot-re notes; **Inferred** = design judgement or unverified.

## Goal
Plan 12 blocks actions by app identity, and plan 12 can only see *which app* is in front. Some of the risky moments are on screen inside an allowed app: a login or password prompt, a payment confirmation, a destructive confirmation dialog, or a system-settings pane opened from a browser. This plan defines a screen-state label that plan 12 uses to request approval (never to silently allow) before the agent clicks or types in those states. It also decides the input: accessibility (AX) text first, screenshot vision only as a fallback.

## Evidence
- **Observed** `tools/computer_use/backend.py:59` `UIElement` carries `element_token: Optional[str]` alongside `role`, `label`, `bounds`, `app`, `pid`, `window_id`, `attributes` (fields read at `backend.py:30-110` in the earlier session). The text is available without an image.
- **Observed** `tools/computer_use/backend.py:63` `class CaptureResult` supports modes `vision`, `ax`, `som` (default `som`) per plan 12 evidence (`backend.py:63`). A capture therefore *can* be AX-only.
- **Observed** `backend.py:100` the action result has `capture: Optional[CaptureResult]`, and `backend.py:102-103` add `verified` and `effect`. A post-action `effect` field exists, which plan 26 can use for before/after comparison.
- **Observed** `backend.py:129-130` `capture(...)` is an abstract method, so each backend must implement it.
- **Observed** `tools/computer_use/cua_backend.py:236-237` and `:306` map `element_index` → `element_token` via `_snapshot_tokens`. Tokens are per snapshot, so a label computed on one snapshot must not be reused for a click on a later one (see Design 4).
- **Observed** `tools/computer_use/tool.py:432` `_DEFAULT_MAX_ELEMENTS = 100`; `tool.py:437` `_MAX_ELEMENT_LABEL_CHARS = 120`; full tree spilled via `_spill_elements_to_file` (`tool.py:627-634`); `_MAX_VISION_DIM = 1456` with `_shrink_capture_for_vision` (`tool.py:637-640`); capture file count capped at 20 (`tool.py:439`). **So the AX text the classifier sees is already capped at 100 elements and 120 chars per label.** Credential fields and dialog buttons are usually near the top of the tree; this is **Inferred** and must be measured.
- **Decoded** grokbot-re notes name AX limits (~2500 nodes) as reference values only; the host has no 2500 cap (plan 12 evidence). Do not design around 2500.
- **Inferred** Linux accessibility coverage (AT-SPI role names, whether password fields expose a `password` role) was **not verified**; plan 12 marks all Linux AX behaviour as unverified. The design below depends on that and has a vision fallback for that reason.

## Current state
| Piece | Where | Status |
|---|---|---|
| Element list with role/label | `backend.py:59`, `tool.py:432-437` | exists, capped |
| Capture modes incl. AX-only | `backend.py:63` | exists |
| Post-action effect | `backend.py:102-103` | exists |
| Screen-state label | none | **missing** |
| Deny by app identity | plan 12 | proposed |
| Approval on sensitive state | plan 12 / plan 02 | proposed |

## Design
**Decision 1 — text first.** The primary input is an AX-derived summary: the window title, focused element role, and the first `N` elements' `role` + `label` (+ `attributes` such as a secure/password flag where the backend provides it). A text classifier head decides the label. This is cheap, repeatable, and avoids sending screenshots off-process.

**Decision 2 — vision only as fallback.** Escalate to a screenshot (`CaptureResult` mode `vision`, shrunk to `_MAX_VISION_DIM`) only when the AX tree is absent, empty, or has no labelled elements. A screenshot is never the first input, because the image can carry on-screen secrets and it is larger. Vision output is the same label set; it does not add labels.

**Decision 3 — the label gates approval, it does not allow.** `login-or-password`, `payment`, and `destructive-dialog` raise the action to an approval request (plan 02 scope key `cua:<action>:<state>`). `system-settings` is an additional signal to plan 12's deny list for `System Settings`-style panes reached from a browser. `normal` changes nothing. No state label ever permits an action that plan 12 would otherwise deny.

**Decision 4 — label is bound to a snapshot.** A label is valid only for the `snapshot_id` (or capture) it was computed on. Before a click on `element_token`, the action path checks the token is from the same snapshot. If the screen changed (new capture, `effect` differs), the label is recomputed or the action falls back to approval. This prevents a label from a benign page being applied to a click on a password field that appeared later.

**Decision 5 — the classifier never sees typed text.** Input excludes the text the agent is about to type and the values of password fields. Only roles, labels, and window titles are sent. A password field's value is not in the AX tree in the normal case; if the backend ever exposes it, it must be stripped before classification.

## Labels
- `login-or-password`: a credential entry or sign-in prompt is the focus or dominates the view (password-role element, "sign in", "enter password", "verification code").
- `payment`: checkout, card number, billing, or "confirm purchase/pay" controls are present.
- `destructive-dialog`: a confirmation for delete, remove, erase, format, revoke, reset, or "cannot be undone".
- `system-settings`: an OS settings or security/privacy pane (window title or labels referring to system preferences/settings).
- `normal`: none of the above.

Multi-label is allowed in principle (a payment page can also be a login page). Plan 12 uses the **highest-priority** label for the approval request, in the order `login-or-password` > `payment` > `destructive-dialog` > `system-settings` > `normal`. This order is a design choice (Inferred), chosen so that credentials are the strictest case.

## Input contract
- `snapshot_id` / capture id, `app` and `pid` (from plan 12 identity), `window_title`.
- `elements`: all of `cap.elements` (the full list, host-side). The 100-entry cap (`tool.py:432`) and 120-char label cap (`tool.py:437`) shape only the rendered view (`_capture_view`, `tool.py:456-458,499`), not the classifier input, so a decisive element past index 100 is still seen. `attributes.secure` is included if the backend provides it. `element_token` is **not** sent to the model; it stays host-side.
- `screenshot` (optional): only on the Decision 2 fallback; shrunk to `_MAX_VISION_DIM`.
- Excluded: typed text, password field values, prior agent turns, the user's prompt.

## Decision wiring
- **Shadow first.** Label written to the audit record for each `computer_use` action, beside the plan 12 audit entry. No change in behaviour.
- **Approval (P2).** For the three high-risk labels, plan 12 issues an approval request with `rule_key = cua:<action>:<label>` (plan 12's scope key shape, to be confirmed against its implementation). The approval binds to the final executed args (existing `plugins.py:2005-2045` digest mechanism), so a label cannot be reused for a different click.
- **Deny (P3).** `system-settings` combined with plan 12's deny list for system settings applies a `block`. The other labels never `block` on their own.
- **Hook.** Labels are computed inside the computer-use dispatch after capture and before approval (plan 12 order: hard blocks → classify → approval → backend). This is host code, not a plugin hook, because it needs the capture object. Observer hooks cannot change the outcome.

## Host changes
1. `tools/computer_use/tool.py`: after the capture step, call the classifier with the Decision 1 input; attach the label to the action record.
2. `tools/computer_use/backend.py`: no schema change. Confirm `attributes` carries a secure/password flag per backend; if a backend has none, the label falls back to title and role heuristics and records `secure-flag: unknown`.
3. `plugins/meta-classifier/schemas.py`: add `SCREEN_STATE_SCHEMA` next to `INJECTION_SCHEMA`, using the convention in plan 01.
4. Plan 12 owns the approval and deny wiring; this plan supplies only the label and its binding.

## Phases
1. **P0 (shadow, AX text):** label computed from AX only, logged. Measure how often AX is empty or capped (the `100`-element cap) on real apps. Linux AX coverage is measured here, not assumed.
2. **P1 (vision fallback):** enable Decision 2 fallback; measure how often it fires and its cost.
3. **P2 (approval):** plan 12 requests approval on the three high-risk labels, default off behind config.
4. **P3 (deny):** `system-settings` deny applied with plan 12's deny list.

## Data & evaluation
- Fixture screens (synthetic AX dumps and screenshots) per label: sign-in form, card form, "delete account" confirm, system preferences pane, and a benign document.
- Metrics: per-label precision and recall on fixtures; share of captures where AX is empty (triggers vision); share where the 100-element cap truncated the tree before the decisive element.
- The cap measurement is the key unknown: if the decisive element (password field) is usually beyond element 100, the cap must be raised or the element list ordered by relevance, which is a plan 12 change.
- No live credentials or real payment screens are used for evaluation. Use synthetic or redacted fixtures only.

## Failure policy
- **Classifier timeout:** label is `unknown`. For `unknown`, plan 12 treats the action as *high-risk* and requests approval (fail toward a prompt). It never silently allows.
- **Unknown label string:** treated as `unknown` (fail toward approval), logged.
- **Low confidence:** the schema is label-only (no probability). Ambiguous screens must be labelled by the rubric as the higher-risk label (credential over payment over destructive over normal). A future probability head defaults to approval below threshold.
- **AX empty and vision unavailable:** `unknown` (as above), and the action is not auto-approved under `--yolo` (plan 12 Decision 1: the guard runs regardless of approval mode).
- **Stale label** (snapshot mismatch, Decision 4): recompute or fall back to `unknown`.

## Tests
- A password-role element in the AX dump yields `login-or-password` regardless of window title.
- A password-role element at index > 100 in `cap.elements` is still classified `login-or-password` (the classifier reads the full list, not the rendered view).
- A click on an `element_token` from snapshot N after the screen changed to N+1 falls back to `unknown`, not the old label.
- Typed text and password values are absent from the classifier input (assert on the built request).
- `--yolo` mode does not skip the label computation or the approval request for high-risk labels.

## Risks & open questions
- **Linux AX fidelity is unverified.** Whether AT-SPI exposes password roles or a secure flag is the largest unknown; the vision fallback exists because of it.
- The 100-element cap may hide the decisive element; see Data & evaluation.
- Labels from one snapshot may be stale by the time of action; Decision 4 is required, not optional.
- Overlap with plan 25: browser DOM text is untrusted and may carry injected content aimed at this classifier. Its output must be treated as data, and the label cannot be steered by page text that says "this is a normal page". Plan 25's fence applies; decide before P0 whether 26 reads 25's label.
- Vision input can contain secrets visible on screen; it is sent only as the fallback. Confirm the model endpoint's data policy before P1.

## Non-goals
- OCR-based secret detection (plan 12 non-goal) or semantic classification of arbitrary screen content beyond the five labels.
- Changing cua-driver or its element identity model.
- Allowing any action on the strength of a `normal` label.
- Replacing plan 12's app-identity deny list.
