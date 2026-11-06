"""Session-scoped acceptance obligations recorded by rule-file matches.

A rule file may declare the checks its guidance requires (``check_refs`` /
``verification_refs``). Those refs stay opaque text: this store never resolves,
imports, or executes them. It only remembers that a session OWES them after the
rule matched a real mutating tool call, so the continuation gate can hold the
turn until a bound ``acceptance`` declaration covers each ref.

Shared state, one module instance per process:

* importing this file registers it in ``sys.modules`` as
  ``hermes_plugins_rules_obligations``;
* a consumer that path-loads the file (``importlib.util.spec_from_file_location``
  on ``plugins/rules/obligations.py``) MUST check ``sys.modules`` for that name
  first, so one copy owns the counters for the whole process;
* the rows live in the native plugin state of the ``rules`` plugin, so a
  consumer in a *different* process (or one that will not path-load this file)
  sees the same rows by constructing ``PluginState("rules")`` and reading the
  ``obligations:<session_id>`` key. ``PluginContext.state`` is exactly
  ``PluginState(plugin_id, manifest.data_namespace)`` and this manifest declares
  no data namespace, so both constructions resolve the same
  ``$HERMES_HOME/plugin-data/<namespace>/state.json``.

Degradation is counted, never silent: unreadable stores, unparsable rows, and
over-cap drops bump :func:`stats` counters that ``rules_explain`` reports. A
store that cannot be read degrades to "nothing owed" — the gate then holds
nothing, and the counter says so.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, NewType

from hermes_cli.plugins_state import PluginState

MODULE_NAME: Final = "hermes_plugins_rules_obligations"
STATE_VERSION: Final = 1
MAX_PER_SESSION: Final = 64
_MAX_REF_COUNT: Final = 64
_MAX_TEXT: Final = 1024
_STATE_PREFIX: Final = "obligations:"

RuleId = NewType("RuleId", str)
"""A rule's own id (``rule:<16-hex>`` or ``global:<name>``): the dedupe key of
an obligation and the identifier the completion hold names. A real str at
runtime, so stored rows and :func:`stats` are unchanged."""


@dataclass(frozen=True, slots=True)
class Obligation:
    """One rule's owed checks for one session (refs are opaque text)."""

    rule_id: RuleId
    rule_name: str
    source: str
    refs: tuple[str, ...]
    paths: tuple[str, ...]
    recorded_at: str


@dataclass(frozen=True, slots=True)
class StoreStats:
    """Fail-open visibility: what the store could not do."""

    read_failures: int = 0
    write_failures: int = 0
    skipped_rows: int = 0
    dropped_over_cap: int = 0


class _Counters:
    __slots__ = ("dropped_over_cap", "read_failures", "skipped_rows", "write_failures")

    def __init__(self) -> None:
        self.read_failures = 0
        self.write_failures = 0
        self.skipped_rows = 0
        self.dropped_over_cap = 0

    def snapshot(self) -> StoreStats:
        return StoreStats(
            read_failures=self.read_failures,
            write_failures=self.write_failures,
            skipped_rows=self.skipped_rows,
            dropped_over_cap=self.dropped_over_cap,
        )


_COUNTERS: Final = _Counters()


def now_iso() -> str:
    """Aware UTC instant, lexicographically comparable with other rows."""
    return datetime.now(UTC).isoformat()


def state_key(session_id: str) -> str:
    """Native plugin-state key holding this session's obligations."""
    return _STATE_PREFIX + session_id


def _store() -> PluginState:
    return PluginState("rules")


def _text(row: dict[str, object], key: str) -> str:
    raw = row.get(key)
    return raw.strip()[:_MAX_TEXT] if isinstance(raw, str) else ""


def _texts(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    values = [v.strip()[:_MAX_TEXT] for v in raw[:_MAX_REF_COUNT]
              if isinstance(v, str) and v.strip()]
    return tuple(dict.fromkeys(values))


def _decode(row: object) -> Obligation | None:
    """Parse one stored row; ``None`` for anything that is not our record."""
    if not isinstance(row, dict) or row.get("v") != STATE_VERSION:
        return None
    rule_id = _text(row, "rule_id")
    if not rule_id:
        return None
    return Obligation(
        rule_id=RuleId(rule_id),
        rule_name=_text(row, "rule_name"),
        source=_text(row, "source"),
        refs=_texts(row.get("refs")),
        paths=_texts(row.get("paths")),
        recorded_at=_text(row, "recorded_at"),
    )


def _encode(obligation: Obligation) -> dict[str, object]:
    return {
        "v": STATE_VERSION,
        "rule_id": obligation.rule_id,
        "rule_name": obligation.rule_name,
        "source": obligation.source,
        "refs": list(obligation.refs),
        "paths": list(obligation.paths),
        "recorded_at": obligation.recorded_at,
    }


def _rows(session_id: str) -> list[Obligation]:
    try:
        stored = _store().get(state_key(session_id))
    except (OSError, RuntimeError, ValueError):
        _COUNTERS.read_failures += 1
        return []
    if stored is None:
        return []
    if not isinstance(stored, list):
        _COUNTERS.skipped_rows += 1
        return []
    rows: list[Obligation] = []
    for raw in stored:
        parsed = _decode(raw)
        if parsed is None:
            _COUNTERS.skipped_rows += 1
            continue
        rows.append(parsed)
    return rows


def _write(session_id: str, rows: list[Obligation]) -> bool:
    try:
        _store().set(state_key(session_id), [_encode(row) for row in rows])
    except (OSError, RuntimeError, ValueError):
        _COUNTERS.write_failures += 1
        return False
    return True


def record(session_id: str, obligation: Obligation) -> tuple[Obligation, ...]:
    """Record one obligation and return the session's rows.

    Deduplication is by ``rule_id``: a repeated match unions refs and paths and
    keeps the first name/source/instant, so re-editing never grows the ledger.
    A session's rows are bounded at :data:`MAX_PER_SESSION`; over-cap records
    are dropped and counted, never silently evicting an earlier obligation.
    """
    sid = str(session_id or "")
    if not sid or not obligation.rule_id or not obligation.refs:
        return for_session(sid)
    rows = _rows(sid)
    for index, existing in enumerate(rows):
        if existing.rule_id != obligation.rule_id:
            continue
        rows[index] = Obligation(
            rule_id=existing.rule_id,
            rule_name=existing.rule_name or obligation.rule_name,
            source=existing.source or obligation.source,
            refs=_texts([*existing.refs, *obligation.refs]),
            paths=_texts([*existing.paths, *obligation.paths]),
            recorded_at=existing.recorded_at or obligation.recorded_at,
        )
        break
    else:
        if len(rows) >= MAX_PER_SESSION:
            _COUNTERS.dropped_over_cap += 1
            return tuple(rows)
        rows.append(obligation)
    _write(sid, rows)
    return tuple(rows)


def for_session(session_id: str) -> tuple[Obligation, ...]:
    """This session's recorded obligations, in recording order."""
    sid = str(session_id or "")
    if not sid:
        return ()
    return tuple(_rows(sid))


def clear(session_id: str) -> None:
    """Drop a session's obligations (session reset/finalize)."""
    sid = str(session_id or "")
    if not sid:
        return
    try:
        _store().set(state_key(sid), None)
    except (OSError, RuntimeError, ValueError):
        _COUNTERS.write_failures += 1


def stats() -> StoreStats:
    """Degradation counters accumulated by this process."""
    return _COUNTERS.snapshot()


sys.modules.setdefault(MODULE_NAME, sys.modules[__name__])
