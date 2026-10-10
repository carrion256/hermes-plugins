"""Steering modes — explicit, turn-local modes referencing skills/rules.

Clean cutover from the removed ``keywords`` prompt-copy table: each configured
mode is activated when one of its triggers appears in genuine prose (fenced
code, inline code, quoted text, and identifier/path contexts are excluded),
and injects a deterministic consult pointer naming the mode's skill/rule
references — never a copy of their text. Modes declared mutually conflicting
surface the conflict and BOTH sides' guidance is withheld (symmetric, order-
independent). Everything is deterministic and inspectable via the
``steering_status`` tool. Guidance-only: no messages or tool args are ever
rewritten, and no semantic classifier or model/effort mutation is involved.
Lifetime is per user turn (host pre_llm_call context is turn-ephemeral).
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
        # Whole-word AND path-safe: no word char, and no path separator or
        # extension dot touching the trigger ("src/deploy/hooks.py" never hits).
        patterns = tuple(
            re.compile(rf"(?<![\w./\\-]){re.escape(t.strip())}(?!\w)(?![./\\][A-Za-z0-9])",
                       re.IGNORECASE)
            for t in triggers
        )
        modes[name] = {
            "refs": tuple(r.strip() for r in refs),
            "patterns": patterns,
            "conflicts_with": frozenset(str(c).strip() for c in conflicts),
        }
    # Second pass: dangling conflict targets (targets defined after this mode).
    for name, spec in modes.items():
        dangling = [c for c in spec["conflicts_with"] if c not in modes]
        if dangling:
            raise ValueError(
                f"keyword-steering: mode {name!r} conflicts_with unknown mode(s) {sorted(dangling)}"
            )
    return modes


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
    guidance = [f"Steering mode `{n}` active this turn (turn-local). Consult: "
                f"{', '.join(modes[n]['refs'])}." for n in active]
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
        "injected": False,
    }


def register(ctx) -> None:
    modes = _read_modes(ctx)
    if not modes:
        logger.info("keyword-steering: no modes configured; hook registered but idle")

    # ponytail: per-process in-memory per-session turn record; if steering
    # ever must survive restarts, persist per-session rows in the session DB.
    records: dict = {}  # session_id -> current-turn record

    def pre_llm_call(user_message: str = "", session_id: str = "",
                     turn_id: str = "", **_kw):
        sess, turn = str(session_id or ""), str(turn_id or "")
        record = records.get(sess)
        if record is None or not turn or record["turn_id"] != turn:
            record = _evaluate(modes, str(user_message or ""), sess, turn)
            records[sess] = record
        if record["injected"] or not record["guidance"]:
            return None
        record["injected"] = True
        return {"context": record["guidance"]}

    def steering_status(session_id: str = "", **_kw):
        record = records.get(str(session_id or ""))
        if record is None:
            return json.dumps({
                "success": True, "session_id": str(session_id or ""),
                "scope": "session", "lifetime": _LIFETIME, "state": "inactive",
                "requested": [], "active": [], "withheld": [], "conflicts": [],
                "references": {},
            })
        return json.dumps({
            "success": True, "session_id": record["session_id"],
            "turn_id": record["turn_id"],
            "scope": "session", "lifetime": _LIFETIME,
            "state": "conflict" if record["conflicts"] else "active" if record["active"] else "inactive",
            "requested": record["requested"], "active": record["active"],
            "withheld": record["withheld"], "conflicts": record["conflicts"],
            "references": {n: list(modes[n]["refs"]) for n in record["active"]},
        })

    ctx.register_tool(
        name="steering_status", toolset="keyword-steering",
        schema={"name": "steering_status",
                "description": "Report this session's steering modes: requested/active/withheld modes, conflicts, skill/rule references, and scope/lifetime.",
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: steering_status(**kw),
        description="Inspect steering mode resolution (active/withheld/conflicts) for this session.",
    )

    def _drop_turns(session_id="", previous_session_id="", **_kw):
        for identity in (session_id, previous_session_id):
            if isinstance(identity, str) and identity:
                records.pop(identity, None)

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_reset", _drop_turns)
    ctx.register_hook("on_session_finalize", _drop_turns)
    ctx.register_hook("agent_loop_stopped", _drop_turns)
    logger.info("keyword-steering: active with %d steering modes", len(modes))
