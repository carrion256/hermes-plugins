"""Process-wide, bounded store of the current turn's steering activations.

``keyword-steering`` writes it from ``pre_llm_call`` and clears it when the turn
ends; ``meta-classifier`` reads it from ``pre_tool_call`` to enforce ``/dry`` and
records what it withheld. The store is in-memory only: it holds exactly one
turn's worth of state, so there is nothing worth persisting and a restart must
start clean.

Cross-plugin access is by module identity. This file registers itself once in
``sys.modules`` under :data:`MODULE_KEY` at import; every loader MUST check that
key first and load the sibling file only when it is absent. Loading the file
twice would give each consumer its own copy of the store, and a call withheld by
``meta-classifier`` would then be invisible to the ``steering_status`` that is
supposed to report it.

Bounds: at most :data:`MAX_SESSIONS` sessions are tracked (least recently
updated is evicted) and at most :data:`MAX_WITHHELD` withheld calls are kept per
session, so a long-lived process cannot grow this without limit.
"""

from __future__ import annotations

import sys
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Final

MODULE_KEY: Final = "hermes_plugins_keyword_steering_turnstate"
MAX_SESSIONS: Final = 256
MAX_WITHHELD: Final = 64

if __name__ in sys.modules:
    sys.modules.setdefault(MODULE_KEY, sys.modules[__name__])


@dataclass(frozen=True, slots=True)
class TurnActivation:
    """The names active for one session's current turn."""

    turn_id: str
    names: frozenset[str]


@dataclass(frozen=True, slots=True)
class Withheld:
    """One tool call a consumer refused while the turn was active."""

    turn_id: str
    tool_name: str
    reason: str


_lock = threading.Lock()
_activations: OrderedDict[str, TurnActivation] = OrderedDict()
_withheld: OrderedDict[str, deque[Withheld]] = OrderedDict()


def _session(session_id: str) -> str:
    return str(session_id or "")


def set_active(session_id: str, turn_id: str, names: frozenset[str]) -> None:
    """Record this turn's active steering names for ``session_id``.

    A different ``turn_id`` starts a new turn and drops the previous turn's
    withheld log; re-recording the same turn keeps it.
    """
    session = _session(session_id)
    turn = str(turn_id or "")
    with _lock:
        previous = _activations.get(session)
        if previous is None or previous.turn_id != turn or session not in _withheld:
            _withheld[session] = deque(maxlen=MAX_WITHHELD)
        _activations[session] = TurnActivation(turn_id=turn, names=frozenset(names))
        _activations.move_to_end(session)
        _withheld.move_to_end(session)
        while len(_activations) > MAX_SESSIONS:
            _activations.popitem(last=False)
        while len(_withheld) > MAX_SESSIONS:
            _withheld.popitem(last=False)


def active(session_id: str) -> TurnActivation | None:
    """This session's current activation, ``None`` when no turn is recorded."""
    with _lock:
        return _activations.get(_session(session_id))


def clear(session_id: str) -> None:
    """Forget everything recorded for one session."""
    session = _session(session_id)
    with _lock:
        _activations.pop(session, None)
        _withheld.pop(session, None)


def record_withheld(session_id: str, tool_name: str, reason: str) -> None:
    """Record one withheld call against this session's current turn.

    Recording without an activation is kept rather than dropped: a consumer
    that withheld a call must never become invisible to ``steering_status``.
    """
    session = _session(session_id)
    with _lock:
        activation = _activations.get(session)
        turn = activation.turn_id if activation is not None else ""
        log = _withheld.get(session)
        if log is None:
            log = deque(maxlen=MAX_WITHHELD)
            _withheld[session] = log
            while len(_withheld) > MAX_SESSIONS:
                _withheld.popitem(last=False)
        log.append(Withheld(turn_id=turn, tool_name=str(tool_name), reason=str(reason)))


def withheld(session_id: str) -> tuple[Withheld, ...]:
    """Every withheld call recorded for this session, oldest first."""
    with _lock:
        log = _withheld.get(_session(session_id))
        return tuple(log) if log is not None else ()
