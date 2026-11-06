"""Typed, read-only completion-closure reader for sibling plugins.

``plugins/continuation`` owns the completion contract in its native plugin
state (one JSON record per session, key ``session:<session_id>``). Other
plugins need the answer — did this session actually close? — without
re-deriving it from raw dicts. This module is the single reader: continuation's
own ``acceptance status`` action uses it, and so does any sibling plugin.
Nothing here writes; the authoritative store stays the host's.

What ``closed`` means (the same condition the stop gate applies): an
independent review returned ``clean`` on the reviewed evidence, EVERY declared
acceptance criterion is bound to native evidence (except one the ledger shows
as explicitly cancelled/blocked), and NO blocker is outstanding. Because
staleness is reported by the gate as a blocker, ``bound`` here is "a native
evidence binding is recorded" — a session whose evidence went stale is not
``closed``, it is ``closed=False`` with a blocker naming the drift.

Reading another plugin's namespace: ``PluginContext.state`` is exactly
``PluginState(plugin_id, manifest.data_namespace)``, and the continuation
manifest declares no ``data_namespace``, so any plugin can construct the
host's own facade for that namespace and pass its bound ``get``::

    from hermes_cli.plugins_state import PluginState
    from plugins.continuation.closure import read_closure   # or path-load this file

    closure = read_closure(session_id, state_reader=PluginState("continuation").get)

That resolves the same ``$HERMES_HOME/plugin-data/<namespace>/state.json`` the
continuation plugin writes. A caller that must not touch the host API may read
that file itself (``json.loads(path.read_text(encoding="utf-8"))``) and pass
``lambda key: stored.get(key)``. Whatever the reader is, this module never
swallows its failure: a corrupt or unreadable store raises to the caller, which
owns how the degradation is reported.

Like the obligation store, importing this file registers it as
``sys.modules["hermes_plugins_continuation_closure"]`` so path-loaders in one
process share one copy.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final, Literal, NewType, Protocol

MODULE_NAME: Final = "hermes_plugins_continuation_closure"
STATE_PREFIX: Final = "session:"
_MAX_TEXT: Final = 4096

SessionId = NewType("SessionId", str)
"""One Hermes session id, as the continuation plugin keys its own state."""
CriterionId = NewType("CriterionId", str)
"""A declared acceptance criterion id (the native todo id where applicable)."""
Verdict = Literal["clean", "findings", "unverified"]
CheckKind = Literal["terminal", "browser", "external"]
Disposition = Literal["cancelled", "blocked", "clear"]
# Excused dispositions: the ledger itself says this proof is not owed.
EXCUSED_DISPOSITIONS: Final[tuple[Disposition, ...]] = ("cancelled", "blocked")


class StateReader(Protocol):
    """Reads one key of the continuation plugin's state (``PluginState.get``)."""

    def __call__(self, key: str) -> object: ...


@dataclass(frozen=True, slots=True)
class AcceptanceStatus:
    """One declared acceptance criterion as the durable record states it."""

    id: CriterionId
    check_kind: CheckKind | None
    check_identity: str
    bound: bool
    disposition: Disposition | None


@dataclass(frozen=True, slots=True)
class Closure:
    """The completion state of one session, derived from the durable record."""

    session_id: SessionId
    review_verdict: Verdict | None
    review_identity: str | None
    review_paths: tuple[str, ...]
    acceptance: tuple[AcceptanceStatus, ...]
    blockers: tuple[str, ...]
    rounds: int
    closed: bool

    @property
    def unmet(self) -> tuple[AcceptanceStatus, ...]:
        """Declared criteria that still need proof and do not have it."""
        return tuple(a for a in self.acceptance
                     if not a.bound and a.disposition not in EXCUSED_DISPOSITIONS)


def _text(value: object) -> str:
    return value.strip()[:_MAX_TEXT] if isinstance(value, str) else ""


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(text for text in (_text(item) for item in value) if text)


def _rounds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _kind(value: object) -> CheckKind | None:
    """Parse the closed check-kind set; anything else is no kind at all, which
    a caller reads as 'this record is not a well-formed declaration'."""
    match value:
        case "terminal" | "browser" | "external":
            return value
        case _:
            return None


def _disposition(value: object) -> Disposition | None:
    """Parse the closed disposition set; an absent or unknown value is ``None``
    (no explicit judgement), never an empty string."""
    match value:
        case "cancelled" | "blocked" | "clear":
            return value
        case _:
            return None


def _verdict(recorded: object, findings: tuple[str, ...],
             blockers: tuple[str, ...]) -> Verdict | None:
    """The review outcome as recorded, or honestly inferred from what the
    gate left behind (carried findings / outstanding blockers). ``None``
    means no review result has ever been recorded for this session."""
    match recorded:
        case "clean":
            return "clean"
        case "findings":
            return "findings"
        case "unverified":
            return "unverified"
        case _:
            pass
    if findings:
        return "findings"
    return "unverified" if blockers else None


def _status(cid: object, crit: object) -> AcceptanceStatus:
    data = crit if isinstance(crit, dict) else {}
    disposition = data.get("disposition")
    return AcceptanceStatus(
        id=CriterionId(_text(cid)),
        check_kind=_kind(data.get("check_kind")),
        check_identity=_text(data.get("check_identity")),
        bound=bool(data.get("evidence")),
        disposition=_disposition(disposition.get("status") if isinstance(
            disposition, dict) else None),
    )


def read_closure(session_id: str, *, state_reader: StateReader) -> Closure | None:
    """Read one session's completion closure, or ``None`` when no record exists.

    ``state_reader`` is any callable matching :class:`StateReader` — pass
    ``PluginState("continuation").get`` (see the module docstring). Malformed
    fields degrade to empty/zero, a missing record returns ``None``, and a
    failing reader raises to the caller.
    """
    sid = str(session_id or "")
    if not sid:
        return None
    record = state_reader(STATE_PREFIX + sid)
    if not isinstance(record, dict):
        return None
    review_value = record.get("review")
    review = review_value if isinstance(review_value, dict) else {}
    blockers = _texts(record.get("blockers"))
    findings = _texts(record.get("findings"))
    criteria_value = record.get("acceptance")
    criteria = criteria_value if isinstance(criteria_value, dict) else {}
    acceptance = tuple(
        _status(cid, crit)
        for cid, crit in sorted(criteria.items(), key=lambda item: _text(item[0])))
    verdict = _verdict(review.get("verdict"), findings, blockers)
    closed = (verdict == "clean" and not blockers
              and all(a.bound or a.disposition in EXCUSED_DISPOSITIONS
                      for a in acceptance))
    return Closure(
        session_id=SessionId(sid),
        review_verdict=verdict,
        review_identity=_text(review.get("identity")) or None,
        review_paths=_texts(review.get("paths")),
        acceptance=acceptance,
        blockers=blockers,
        rounds=_rounds(record.get("rounds")),
        closed=closed,
    )


sys.modules.setdefault(MODULE_NAME, sys.modules[__name__])
