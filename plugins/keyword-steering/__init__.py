"""Steering modes — explicit, turn-local modes referencing skills/rules.

Clean cutover from the removed ``keywords`` prompt-copy table: each configured
mode is activated when one of its triggers appears in genuine prose (fenced
code, inline code, quoted text, and identifier/path contexts are excluded),
and injects a deterministic consult pointer naming the mode's skill/rule
references — never a copy of their text. References are resolved once at
registration: an unresolved one is warned about and annotated, never refused.
Modes declared mutually conflicting surface the conflict and BOTH sides'
guidance is withheld (symmetric, order-independent). Everything is
deterministic and inspectable via the ``steering_status`` tool. Lifetime is
per user turn (host pre_llm_call context is turn-ephemeral).

OMP-compatible magic keywords — ``ultrathink``, ``orchestrate``,
``workflow`` — ship as built-in modes, enabled by default and configurable
via the ``magic_keywords`` mapping (name -> bool or {enabled, bare}, plus
optional top-level ``enabled``). They ride the same deterministic machinery
(prose-only, whole-word, path-safe triggers; conflict rules; per-turn
lifetime; ``steering_status`` visibility) and their names are reserved: a
user mode shadowing one fails loudly at registration. ``ultrathink``
additionally requests the host-sanctioned effort raise through
``PluginContext.set_turn_reasoning("ultra", ...)`` when the host provides
it, degrading to guidance-only (surfaced in the reminder and
``steering_status``) when it does not.

Common-word keywords (``orchestrate`` and ``workflow`` by default) match only
in sigil form (``/workflow`` or ``#workflow``) unless the mode sets
``bare: true``; a negated bare hit ("don't ultrathink this") is ambiguous too.
The one optional semantic seam is a classifier tie-break, consulted only for
ambiguous hits when ``classifier`` is enabled; when it is disabled or
unavailable the ambiguous hit is declined. The deterministic path is
unchanged, and no message or tool argument is ever rewritten.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from . import refs
from .tiebreak import Tiebreak

logger = logging.getLogger(__name__)

# A fence opens on a backtick run whose info string holds no backtick
# (CommonMark) or on a tilde run; _prose_only decides where it closes.
_FENCE_RX = re.compile(r"(`{3,}(?=[^`]*$)|~{3,})")
_INLINE_RX = re.compile(r"(`+)(.+?)\1")
# Double quotes always delimit; an apostrophe delimits only outside a word, so
# "don't" and "it's" never open a quoted span.
_QUOTE_RX = re.compile(r"\"[^\"\n]*\"|(?<!\w)'[^'\n]*'(?!\w)")
_NEGATION_RX = re.compile(
    r"\b(?:not|don'?t|do not|never|no|without|skip|avoid|isn'?t|aren'?t"
    r"|won'?t|instead of|rather than|stop)\b",
    re.IGNORECASE,
)

_LIFETIME = "turn"
_MODE_FIELDS = {"refs", "triggers", "conflicts_with", "bare"}

# Built-in OMP-compatible magic keywords: enabled by default, configurable
# via the 'magic_keywords' mapping, and a reserved namespace — user modes
# may not shadow them (registration fails loudly) but may name them in
# conflicts_with.
_MAGIC_KEYWORDS = ("ultrathink", "orchestrate", "workflow")
# Common words: a bare hit is ambiguous unless the keyword is configured bare.
_MAGIC_BARE = {"ultrathink": True, "orchestrate": False, "workflow": False}
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
        "has a delegate tool — and batch independent requests."
    ),
    "workflow": (
        "Magic keyword 'workflow' is active this turn (built-in, "
        "OMP-compatible; whole-word match in prose): follow a structured, "
        "staged workflow for this turn — explicit plan first, then "
        "verification, then a closing review."
    ),
}
# Appended to the orchestrate guidance only when the host registers route_status.
_ROUTING_NOTE = (
    " When model routing is configured, delegate tasks may pass `model_role` to "
    "request a configured role, and the `route_status` tool lists the "
    "configured roles."
)
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


def _sigil_pattern(trigger: str) -> re.Pattern:
    # Explicit form for common words: "/workflow" or "#workflow" at the start
    # of a line or after whitespace; path-safe like the bare form, so
    # "see /workflow.py" and "/workflow/run.sh" never hit.
    return re.compile(
        rf"(?:^|(?<=\s))[/#]{re.escape(trigger)}(?!\w)(?![./\\][A-Za-z0-9])",
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
        ref_list = body.get("refs")
        if (not isinstance(ref_list, list) or not ref_list
                or any(not isinstance(r, str) or not r.strip() for r in ref_list)):
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
        bare = body.get("bare", True)
        if not isinstance(bare, bool):
            raise ValueError(f"keyword-steering: mode {name!r} 'bare' must be a boolean")
        modes[name] = {
            "refs": tuple(r.strip() for r in ref_list),
            "triggers": tuple(t.strip() for t in triggers),
            "patterns": tuple(_trigger_pattern(t.strip()) for t in triggers),
            "sigil_patterns": tuple(_sigil_pattern(t.strip()) for t in triggers),
            "bare": bare,
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
    """Parse 'magic_keywords' into {name: {"enabled": bool, "bare": bool}}.

    Each keyword is a bool, or a mapping with optional 'enabled' and 'bare'
    booleans; the top-level 'enabled' is the default for keywords left unset.
    Every built-in defaults to enabled.
    """
    raw = ctx.get_config("magic_keywords")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            "keyword-steering: 'magic_keywords' must be a mapping of keyword -> "
            f"boolean or {{enabled, bare}} (built-ins: {list(_MAGIC_KEYWORDS)}), plus "
            "optional top-level 'enabled'"
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
    magic = {}
    for name in _MAGIC_KEYWORDS:
        value = raw.get(name, default)
        if isinstance(value, bool):
            value = {"enabled": value}
        if not isinstance(value, dict):
            raise ValueError(
                f"keyword-steering: magic_keywords[{name!r}] must be a boolean or a "
                "mapping with 'enabled'/'bare' booleans"
            )
        unknown = sorted(str(k) for k in value if k not in ("enabled", "bare"))
        if unknown:
            raise ValueError(
                f"keyword-steering: magic_keywords[{name!r}] has unknown key(s) {unknown}; "
                "allowed: ['enabled', 'bare']"
            )
        enabled = value.get("enabled", default)
        bare = value.get("bare", _MAGIC_BARE[name])
        if not isinstance(enabled, bool) or not isinstance(bare, bool):
            raise ValueError(
                f"keyword-steering: magic_keywords[{name!r}] 'enabled' and 'bare' must be booleans"
            )
        magic[name] = {"enabled": enabled, "bare": bare}
    return magic


def _prose_only(message: str) -> str:
    """Strip fenced blocks, inline code, and quoted spans; prose stays."""
    kept = []
    fence = None  # (char, length) of the open fence, or None
    for line in message.splitlines():
        stripped = line.strip()
        if fence is not None:
            # A fence closes only on its own char repeated at least as long.
            char, size = fence
            if len(stripped) >= size and stripped == char * len(stripped):
                fence = None
            continue
        m = _FENCE_RX.match(stripped)
        if m:
            fence = (m.group(1)[0], len(m.group(1)))
            continue
        line = _INLINE_RX.sub(" ", line)
        line = _QUOTE_RX.sub(" ", line)
        kept.append(line)
    return "\n".join(kept)


def _negated(prose: str, start: int) -> bool:
    """True when a negation word sits in the three tokens before ``start``, same sentence."""
    before = prose[:start]
    sentence = before[max(before.rfind(c) for c in ".!?;:\n") + 1:]
    return bool(_NEGATION_RX.search(" ".join(sentence.split()[-3:])))


def _hits(name: str, spec: dict, prose: str, tiebreak) -> list[dict]:
    """One record per (trigger, form) hit in prose, each with its verdict."""
    hits = []
    for trigger, bare_rx, sigil_rx in zip(spec["triggers"], spec["patterns"],
                                          spec["sigil_patterns"]):
        sigils = [m.span() for m in sigil_rx.finditer(prose)]
        if sigils:
            hits.append({"mode": name, "trigger": trigger, "form": "sigil",
                         "ambiguity": None, "verdict": "active",
                         "confirmed_by": "regex", "reason": None})
        # A bare match inside a sigil match is that sigil ("#workflow").
        starts = [m.start() for m in bare_rx.finditer(prose)
                  if not any(lo <= m.start() < hi for lo, hi in sigils)]
        if not starts:
            continue
        if not spec["bare"]:
            ambiguity = "common_word"
        elif all(_negated(prose, s) for s in starts):
            ambiguity = "negated"
        else:
            ambiguity = None
        if ambiguity is None:
            verdict, confirmed_by, reason = "active", "regex", None
        elif tiebreak is None or not tiebreak.enabled:
            verdict, confirmed_by = "declined", "regex"
            reason = "sigil_required" if ambiguity == "common_word" else "negated"
        else:
            label = tiebreak.judge(trigger, prose)
            if label == "directive":
                verdict, confirmed_by, reason = "active", "classifier", None
            elif label is None:
                verdict, confirmed_by, reason = "declined", "regex", "classifier_unavailable"
            else:
                verdict, confirmed_by, reason = "declined", "classifier", f"classifier:{label}"
        hits.append({"mode": name, "trigger": trigger, "form": "bare",
                     "ambiguity": ambiguity, "verdict": verdict,
                     "confirmed_by": confirmed_by, "reason": reason})
    return hits


def _route_status_registered() -> bool:
    """Whether the host registered route_status; probed per evaluation, never raises."""
    try:
        from tools.registry import registry
        return registry.get_entry("route_status") is not None
    except Exception:  # no host registry, or it changed shape: omit the routing note
        return False


def _evaluate(modes: dict, message: str, session_id: str, turn_id: str,
              tiebreak=None) -> dict:
    """Deterministically resolve this turn's active/withheld modes."""
    prose = _prose_only(message)
    hits: list[dict] = []
    requested = []
    for name, spec in modes.items():
        mode_hits = _hits(name, spec, prose, tiebreak)
        hits.extend(mode_hits)
        if any(h["verdict"] == "active" for h in mode_hits):
            requested.append(name)
    req = set(requested)
    conflicts = sorted({(min(a, b), max(a, b))
                        for a in requested
                        for b in modes[a]["conflicts_with"] if b in req})
    withheld = sorted({m for pair in conflicts for m in pair})
    active = [n for n in requested if n not in withheld]
    guidance = []
    for n in active:
        if modes[n].get("magic"):
            text = _MAGIC_GUIDANCE[n]
            if n == "orchestrate" and _route_status_registered():
                text += _ROUTING_NOTE
            guidance.append(text)
        else:
            line = (f"Steering mode `{n}` active this turn (turn-local). Consult: "
                    f"{', '.join(modes[n]['refs'])}.")
            missing = modes[n].get("unresolved", ())
            if missing:
                line += (f" (Unresolved at registration: {', '.join(missing)} — "
                         "the reference may be renamed or profile-local.)")
            guidance.append(line)
    if conflicts:
        guidance.append(
            "Steering conflict: "
            + "; ".join(f"`{a}` vs `{b}` declare mutual exclusion" for a, b in conflicts)
            + ". Guidance for both is withheld this turn. Tell the user which modes "
            "conflicted and ask which one applies before continuing."
        )
        for a, b in conflicts:
            logger.warning("keyword-steering: conflict %s in session %s turn %s",
                           f"{a} vs {b}", session_id, turn_id)
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "lifetime": _LIFETIME,
        "requested": requested,
        "active": active,
        "withheld": withheld,
        "conflicts": [list(p) for p in conflicts],
        "hits": hits,
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
    magic = _read_magic(ctx)
    tiebreak = Tiebreak(ctx)
    # Resolve refs once, here. Unresolved ones are warned about and annotated in
    # guidance and steering_status but never refuse the load: a reference may
    # be renamed or profile-local.
    unresolved = refs.unresolved_refs(modes, refs.roots_from_config(ctx))
    for name, missing in unresolved.items():
        modes[name]["unresolved"] = tuple(missing)
        logger.warning("keyword-steering: mode %r references unresolved ref(s) %s",
                       name, ", ".join(missing))
    all_modes = dict(modes)
    for name in _MAGIC_KEYWORDS:
        if magic[name]["enabled"]:
            all_modes[name] = {
                "refs": (_MAGIC_REF,),
                "triggers": (name,),
                "patterns": (_MAGIC_PATTERNS[name],),
                "sigil_patterns": (_sigil_pattern(name),),
                "bare": magic[name]["bare"],
                "conflicts_with": frozenset(),
                "magic": True,
            }
    if not all_modes:
        logger.info("keyword-steering: no modes configured and no magic keywords "
                    "enabled; hook registered but idle")

    # ponytail: per-process in-memory per-session turn record; if steering
    # ever must survive restarts, persist per-session rows in the session DB.
    records: dict = {}  # session_id -> current-turn record

    def pre_llm_call(user_message: str = "", session_id: str = "",
                     turn_id: str = "", **_kw):
        sess, message = str(session_id or ""), str(user_message or "")
        # Without a host turn id the record is keyed on the message, so guidance
        # and the effort request fire once per message, not once per LLM call.
        # ponytail: identical back-to-back messages share a key when the host
        # sends no turn ids; the fix is a host-supplied turn id.
        key = str(turn_id or "") or ("msg:" + hashlib.sha256(
            message.encode("utf-8", "replace")).hexdigest()[:16])
        record = records.get(sess)
        if record is None or record["turn_id"] != key:
            record = _evaluate(all_modes, message, sess, key, tiebreak)
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
        sess = str(session_id or "")
        record = records.get(sess) or {}
        active = record.get("active", [])
        return json.dumps({
            "success": True, "session_id": sess,
            "turn_id": record.get("turn_id", ""),
            "scope": "session", "lifetime": _LIFETIME,
            "state": "conflict" if record.get("conflicts") else "active" if active else "inactive",
            "requested": record.get("requested", []), "active": active,
            "withheld": record.get("withheld", []), "conflicts": record.get("conflicts", []),
            "hits": record.get("hits", []),
            "references": {n: list(all_modes[n]["refs"]) for n in active},
            "unresolved_refs": unresolved,
            "magic_keywords": {name: {"enabled": magic[name]["enabled"],
                                      "bare": magic[name]["bare"],
                                      "source": "built-in (OMP-compatible)"}
                               for name in _MAGIC_KEYWORDS},
            "magic_effects": record.get("magic_effects", {}),
            "classifier": tiebreak.status(),
        })

    ctx.register_tool(
        name="steering_status", toolset="keyword-steering",
        schema={"name": "steering_status",
                "description": "Report this session's steering modes: requested/active/withheld modes, conflicts, per-hit verdicts (form, ambiguity, regex- or classifier-confirmed), skill/rule references plus any unresolved at registration, built-in magic keywords (enabled state, bare/sigil requirement, provenance, and the ultrathink effort outcome), classifier tie-break status, and scope/lifetime.",
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: steering_status(**kw),
        description="Inspect steering mode resolution (active/withheld/conflicts, hit verdicts, unresolved refs, built-in magic keywords) for this session.",
    )

    def _drop_turns(session_id="", old_session_id="", new_session_id="",
                    session_key="", **_kw):
        # The host rotates with on_session_reset(session_id=<new>, old_session_id=…,
        # new_session_id=…) and interrupts with agent_loop_stopped(session_key=…); it never
        # sent the previous_session_id this used to key on, so the outgoing session's
        # record leaked for the life of the process. Drop every identifier it does send.
        for identity in (session_id, old_session_id, new_session_id, session_key):
            if isinstance(identity, str) and identity:
                records.pop(identity, None)

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_reset", _drop_turns)
    ctx.register_hook("on_session_finalize", _drop_turns)
    ctx.register_hook("agent_loop_stopped", _drop_turns)
    logger.info("keyword-steering: active with %d user mode(s) and %d/%d magic keyword(s) enabled",
                len(modes), sum(m["enabled"] for m in magic.values()), len(_MAGIC_KEYWORDS))
