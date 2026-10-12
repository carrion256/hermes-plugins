"""Steering modes — explicit, turn-local modes referencing skills/rules.

Clean cutover from the removed ``keywords`` prompt-copy table: each configured
mode is activated when one of its triggers appears in genuine prose (fenced
code, inline code, quoted text, and identifier/path contexts are excluded),
and injects a deterministic consult pointer naming the mode's skill/rule
references — never a copy of their text. Modes declared mutually conflicting
surface the conflict and BOTH sides' guidance is withheld (symmetric, order-
independent). Everything is deterministic and inspectable via the
``steering_status`` tool. Lifetime is per user turn (host pre_llm_call
context is turn-ephemeral).

OMP-compatible magic keywords — ``ultrathink``, ``orchestrate``,
``workflow`` — ship as built-in modes, enabled by default and configurable
via the ``magic_keywords`` mapping (name -> bool, plus optional top-level
``enabled``). They ride the same deterministic machinery (prose-only,
whole-word, path-safe triggers; conflict rules; per-turn lifetime;
``steering_status`` visibility) and their names are reserved: a user mode
shadowing one fails loudly at registration. ``ultrathink`` additionally
requests the host-sanctioned effort raise through
``PluginContext.set_turn_reasoning("ultra", ...)`` when the host provides
it, degrading to guidance-only (surfaced in the reminder and
``steering_status``) when it does not. Apart from that one seam the plugin
stays guidance-only: no messages or tool args are ever rewritten, and no
semantic classifier or model/effort mutation is involved.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

_FENCE_RX = re.compile(r"(?s)^(`{3,}|~{3,})")
_INLINE_RX = re.compile(r"(?s)`[^`]*`")
_QUOTE_RX = re.compile(r"(?:\"[^\"]*\"|'[^']*')")

_LIFETIME = "turn"
_MODE_FIELDS = {"refs", "triggers", "conflicts_with"}

# Built-in OMP-compatible magic keywords: enabled by default, configurable
# via the 'magic_keywords' mapping, and a reserved namespace — user modes
# may not shadow them (registration fails loudly) but may name them in
# conflicts_with.
_MAGIC_KEYWORDS = ("ultrathink", "orchestrate", "workflow")
_MAGIC_REF = "built-in magic keyword (OMP-compatible)"
_MAGIC_GUIDANCE = {
    "ultrathink": (
        "Magic keyword 'ultrathink' is active this turn (built-in, "
        "OMP-compatible; whole-word match in prose): maximum reasoning effort "
        "requested — think as deeply and thoroughly as possible before answering."
    ),
    "orchestrate": (
        "Magic keyword 'orchestrate' is active this turn (built-in, "
        "OMP-compatible; whole-word match in prose): fan out independent work "
        "through delegation and parallel subagents where possible — the host "
        "has a delegate tool — and batch independent requests. When model "
        "routing is configured, delegate tasks may pass `model_role` to "
        "request a configured role, and the `route_status` tool lists the "
        "configured roles."
    ),
    "workflow": (
        "Magic keyword 'workflow' is active this turn (built-in, "
        "OMP-compatible; whole-word match in prose): follow a structured, "
        "staged workflow for this turn — explicit plan first, then "
        "verification, then a closing review."
    ),
}
_EFFORT_REASON = "magic keyword: ultrathink"
_EFFORT_APPLIED_LINE = (
    "Reasoning effort raised to 'ultra' for this turn: the host accepted the "
    "set_turn_reasoning('ultra') request (receipt applied=true)."
)
_EFFORT_DECLINED_LINE = (
    "Reasoning-effort request degraded to guidance-only: the host did not "
    "apply set_turn_reasoning('ultra') (receipt applied=false); recorded in "
    "steering_status."
)
_EFFORT_NO_API_LINE = (
    "Reasoning-effort request degraded to guidance-only: this host does not "
    "provide PluginContext.set_turn_reasoning; recorded in steering_status."
)


def _trigger_pattern(trigger: str) -> re.Pattern:
    # Whole-word AND path-safe: no word char, and no path separator or
    # extension dot touching the trigger ("src/deploy/hooks.py" never hits).
    return re.compile(
        rf"(?<![\w./\\-]){re.escape(trigger)}(?!\w)(?![./\\][A-Za-z0-9])",
        re.IGNORECASE,
    )


_MAGIC_PATTERNS = {name: _trigger_pattern(name) for name in _MAGIC_KEYWORDS}


def _read_modes(ctx) -> dict:
    """Parse config into validated mode specs; fail loudly and early.

    Invalid or legacy configuration raises at registration time — the plugin
    refuses to load rather than steer on a half-valid table.
    """
    if ctx.get_config("keywords") is not None:
        raise ValueError(
            "keyword-steering: the 'keywords' config was removed; migrate each "
            "entry to an explicit mode under 'modes' — "
            "modes: {<name>: {refs: [skill/rule path or id], triggers: [...], "
            "conflicts_with: [...]}}"
        )
    raw = ctx.get_config("modes") or {}
    if not isinstance(raw, dict):
        raise ValueError("keyword-steering: 'modes' must be a mapping of mode name -> spec")
    for name in raw:
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"keyword-steering: mode name {name!r} must be a non-empty string")
        if name.strip() in _MAGIC_KEYWORDS:
            raise ValueError(
                f"keyword-steering: mode name {name.strip()!r} is reserved by a "
                "built-in magic keyword (enabled/disabled via 'magic_keywords'); "
                "user modes must not shadow it"
            )
    modes = {}
    for name, body in raw.items():
        name = name.strip()
        if not isinstance(body, dict):
            raise ValueError(f"keyword-steering: mode {name!r} must be a mapping")
        unknown = set(body) - _MODE_FIELDS
        if unknown:
            raise ValueError(
                f"keyword-steering: mode {name!r} has unknown field(s) {sorted(unknown)}; "
                f"allowed: {sorted(_MODE_FIELDS)}"
            )
        refs = body.get("refs")
        if (not isinstance(refs, list) or not refs
                or any(not isinstance(r, str) or not r.strip() for r in refs)):
            raise ValueError(
                f"keyword-steering: mode {name!r} needs a non-empty 'refs' list of "
                "skill/rule paths or identifiers"
            )
        triggers = body.get("triggers", [name])
        if (not isinstance(triggers, list) or not triggers
                or any(not isinstance(t, str) or not t.strip() for t in triggers)):
            raise ValueError(
                f"keyword-steering: mode {name!r} 'triggers' must be a non-empty list of strings"
            )
        conflicts = body.get("conflicts_with", [])
        if (not isinstance(conflicts, list)
                or any(not isinstance(c, str) for c in conflicts)):
            raise ValueError(f"keyword-steering: mode {name!r} 'conflicts_with' must be a list of mode names")
        if any(str(c).strip() == name for c in conflicts):
            raise ValueError(f"keyword-steering: mode {name!r} cannot conflict with itself")
        modes[name] = {
            "refs": tuple(r.strip() for r in refs),
            "patterns": tuple(_trigger_pattern(t.strip()) for t in triggers),
            "conflicts_with": frozenset(str(c).strip() for c in conflicts),
        }
    # Second pass: dangling conflict targets (targets defined after this
    # mode; built-in magic keywords are always valid targets, enabled or not).
    for name, spec in modes.items():
        dangling = [c for c in spec["conflicts_with"]
                    if c not in modes and c not in _MAGIC_KEYWORDS]
        if dangling:
            raise ValueError(
                f"keyword-steering: mode {name!r} conflicts_with unknown mode(s) {sorted(dangling)}"
            )
    return modes


def _read_magic(ctx) -> dict:
    """Parse the 'magic_keywords' config; every built-in defaults to enabled."""
    raw = ctx.get_config("magic_keywords")
    if raw is None:
        return {name: True for name in _MAGIC_KEYWORDS}
    if not isinstance(raw, dict):
        raise ValueError(
            "keyword-steering: 'magic_keywords' must be a mapping of keyword -> "
            f"boolean (built-ins: {list(_MAGIC_KEYWORDS)}), plus optional "
            "top-level 'enabled'"
        )
    unknown = sorted(str(k) for k in raw if k != "enabled" and k not in _MAGIC_KEYWORDS)
    if unknown:
        raise ValueError(
            f"keyword-steering: unknown magic keyword(s) {unknown}; "
            f"built-ins: {list(_MAGIC_KEYWORDS)}"
        )
    default = raw.get("enabled", True)
    if not isinstance(default, bool):
        raise ValueError("keyword-steering: magic_keywords 'enabled' must be a boolean")
    enabled = {}
    for name in _MAGIC_KEYWORDS:
        value = raw.get(name, default)
        if not isinstance(value, bool):
            raise ValueError(
                f"keyword-steering: magic_keywords[{name!r}] must be a boolean"
            )
        enabled[name] = value
    return enabled


def _prose_only(message: str) -> str:
    """Strip fenced blocks, inline code, and quoted spans; prose stays."""
    kept = []
    in_fence = False
    for line in message.splitlines():
        m = _FENCE_RX.match(line.strip())
        if m:
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = _INLINE_RX.sub(" ", line)
        line = _QUOTE_RX.sub(" ", line)
        kept.append(line)
    return "\n".join(kept)


def _evaluate(modes: dict, message: str, session_id: str, turn_id: str) -> dict:
    """Deterministically resolve this turn's active/withheld modes."""
    prose = _prose_only(message)
    requested = [name for name, spec in modes.items()
                 if any(rx.search(prose) for rx in spec["patterns"])]
    req = set(requested)
    conflicts = sorted({(min(a, b), max(a, b))
                        for a in requested
                        for b in modes[a]["conflicts_with"] if b in req})
    withheld = sorted({m for pair in conflicts for m in pair})
    active = [n for n in requested if n not in withheld]
    guidance = []
    for n in active:
        if modes[n].get("magic"):
            guidance.append(_MAGIC_GUIDANCE[n])
        else:
            guidance.append(
                f"Steering mode `{n}` active this turn (turn-local). Consult: "
                f"{', '.join(modes[n]['refs'])}."
            )
    if conflicts:
        guidance.append(
            "Steering conflict: "
            + "; ".join(f"`{a}` vs `{b}` declare mutual exclusion" for a, b in conflicts)
            + ". Guidance for the conflicting modes is withheld — disambiguate first."
        )
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "lifetime": _LIFETIME,
        "requested": requested,
        "active": active,
        "withheld": withheld,
        "conflicts": [list(p) for p in conflicts],
        "guidance": "\n\n".join(guidance),
        "magic_effects": {},
        "injected": False,
    }


def _raise_ultrathink_effort(ctx) -> dict:
    """Ask the host to raise this turn's reasoning effort; honest receipt.

    The seam is probed with getattr so the plugin stays independently
    installable: an absent (or raising, or declining) host API degrades to
    guidance-only, and that outcome is recorded for ``steering_status``.
    """
    fn = getattr(ctx, "set_turn_reasoning", None)
    if not callable(fn):
        return {"applied": False, "degraded": True, "reason": _EFFORT_REASON,
                "detail": "host PluginContext.set_turn_reasoning unavailable",
                "degraded_line": _EFFORT_NO_API_LINE}
    try:
        receipt = fn("ultra", reason=_EFFORT_REASON)
    except Exception as exc:  # Host contract: never raises; guarded anyway.
        return {"applied": False, "degraded": True, "reason": _EFFORT_REASON,
                "detail": f"set_turn_reasoning raised {exc!r}",
                "degraded_line": _EFFORT_NO_API_LINE}
    applied = isinstance(receipt, dict) and bool(receipt.get("applied"))
    return {
        "applied": applied,
        "degraded": not applied,
        "reason": _EFFORT_REASON,
        "receipt": (receipt if isinstance(receipt, dict)
                    else {"applied": False, "raw": receipt}),
        "degraded_line": _EFFORT_DECLINED_LINE,
    }


def register(ctx) -> None:
    modes = _read_modes(ctx)
    magic_enabled = _read_magic(ctx)
    all_modes = dict(modes)
    for name in _MAGIC_KEYWORDS:
        if magic_enabled[name]:
            all_modes[name] = {
                "refs": (_MAGIC_REF,),
                "patterns": (_MAGIC_PATTERNS[name],),
                "conflicts_with": frozenset(),
                "magic": True,
            }
    if not all_modes:
        logger.info("keyword-steering: no modes configured and no magic keywords enabled; hook registered but idle")

    # ponytail: per-process in-memory per-session turn record; if steering
    # ever must survive restarts, persist per-session rows in the session DB.
    records: dict = {}  # session_id -> current-turn record

    def pre_llm_call(user_message: str = "", session_id: str = "",
                     turn_id: str = "", **_kw):
        sess, turn = str(session_id or ""), str(turn_id or "")
        record = records.get(sess)
        if record is None or not turn or record["turn_id"] != turn:
            record = _evaluate(all_modes, str(user_message or ""), sess, turn)
            records[sess] = record
        if record["injected"] or not record["guidance"]:
            return None
        if "ultrathink" in record["active"]:
            effect = _raise_ultrathink_effort(ctx)
            record["magic_effects"]["ultrathink"] = effect
            record["guidance"] += "\n\n" + (
                _EFFORT_APPLIED_LINE if effect["applied"] else effect["degraded_line"]
            )
        record["injected"] = True
        return {"context": record["guidance"]}

    def steering_status(session_id: str = "", **_kw):
        magic_summary = {name: {"enabled": magic_enabled[name],
                                "source": "built-in (OMP-compatible)"}
                         for name in _MAGIC_KEYWORDS}
        record = records.get(str(session_id or ""))
        if record is None:
            return json.dumps({
                "success": True, "session_id": str(session_id or ""),
                "scope": "session", "lifetime": _LIFETIME, "state": "inactive",
                "requested": [], "active": [], "withheld": [], "conflicts": [],
                "references": {}, "magic_keywords": magic_summary,
                "magic_effects": {},
            })
        return json.dumps({
            "success": True, "session_id": record["session_id"],
            "turn_id": record["turn_id"],
            "scope": "session", "lifetime": _LIFETIME,
            "state": "conflict" if record["conflicts"] else "active" if record["active"] else "inactive",
            "requested": record["requested"], "active": record["active"],
            "withheld": record["withheld"], "conflicts": record["conflicts"],
            "references": {n: list(all_modes[n]["refs"]) for n in record["active"]},
            "magic_keywords": magic_summary,
            "magic_effects": record["magic_effects"],
        })

    ctx.register_tool(
        name="steering_status", toolset="keyword-steering",
        schema={"name": "steering_status",
                "description": "Report this session's steering modes: requested/active/withheld modes, conflicts, skill/rule references, built-in magic keywords (enabled state, provenance, and the ultrathink effort outcome), and scope/lifetime.",
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: steering_status(**kw),
        description="Inspect steering mode resolution (active/withheld/conflicts, built-in magic keywords) for this session.",
    )

    def _drop_turns(session_id="", previous_session_id="", **_kw):
        for identity in (session_id, previous_session_id):
            if isinstance(identity, str) and identity:
                records.pop(identity, None)

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_reset", _drop_turns)
    ctx.register_hook("on_session_finalize", _drop_turns)
    ctx.register_hook("agent_loop_stopped", _drop_turns)
    logger.info("keyword-steering: active with %d user mode(s) and %d/%d magic keyword(s) enabled",
                len(modes), sum(magic_enabled.values()), len(_MAGIC_KEYWORDS))
