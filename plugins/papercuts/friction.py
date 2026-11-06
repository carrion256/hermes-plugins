"""Gate friction intake: one typed, process-wide sink for other plugins' refusals.

Continuation gates, loop guards and chain-compaction bailouts all know *why* a turn
stopped; papercuts already knows how to file that durably. This module is the seam
between them: producers build a frozen ``GateFriction``, call ``report``, and get a
frozen ``FrictionReceipt`` back. Papercuts owns the filing policy (category, blame
signature, redaction, the cross-process daily quota) and installs the sink here.

Sharing contract: this module registers itself as ``sys.modules`` entry
``hermes_plugins_papercuts_friction`` on import. Every path-based loader checks
``sys.modules`` first, so two plugins loading this file share ONE sink slot, ONE
dedupe window and ONE set of counters instead of two silent copies. Loaders that
skip that check get a private copy and are the bug.

Fail-open is visible, never silent: with no sink installed nothing raises, and each
report is counted per source and per reason in ``stats()`` (bounded: three named
sources plus ``unknown``, six reasons). ``papercuts`` exposes ``stats()`` as the
``friction`` tool mode, so "no sink" and "quota exhausted" are observable numbers
rather than a swallowed call. There is deliberately no logger here: structlog is not
a host dependency and this repo's house rules forbid stdlib logging in new modules,
so an unusable sink records ``last_error`` in ``stats()`` instead of a log line.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal, NewType, TypeAlias

# Shared-state registry name: loaders must look this up in sys.modules before
# exec'ing this file, otherwise the process ends up with two independent sinks.
SHARED_MODULE: Final = "hermes_plugins_papercuts_friction"

FrictionSource: TypeAlias = Literal["continuation", "loop-guard", "chain-compaction"]
FrictionReason: TypeAlias = Literal[
    "filed", "no_sink", "quota", "duplicate", "disabled", "error"
]
# Domain ids: a caller's dedupe key and a session id are not interchangeable
# strings, and neither is free text.
GateIdentity: TypeAlias = NewType("GateIdentity", str)
SessionId: TypeAlias = NewType("SessionId", str)

SOURCES: Final[tuple[FrictionSource, ...]] = (
    "continuation",
    "loop-guard",
    "chain-compaction",
)
REASONS: Final[tuple[FrictionReason, ...]] = (
    "filed",
    "no_sink",
    "quota",
    "duplicate",
    "disabled",
    "error",
)
UNKNOWN_SOURCE: Final = "unknown"
# stats() always exposes these keys, counted or not: a consumer must be able to
# read a stable shape, and an absent source must not look like a crash.
SOURCE_KEYS: Final[tuple[str, ...]] = (*SOURCES, UNKNOWN_SOURCE)
# Bounded dedupe window: the oldest identity is evicted first, so a long-lived
# process cannot grow a set keyed by caller-controlled strings without limit.
SEEN_LIMIT: Final = 1024
ERROR_TEXT_LIMIT: Final = 200


@dataclass(frozen=True, slots=True)
class GateFriction:
    """One gate refusal, already parsed by the plugin that observed it.

    ``identity`` is the caller's own dedupe key (a rule key, a thread id, a plan
    id); ``summary`` is the human title papercuts files, ``detail`` the evidence
    excerpt. Neither is trusted for shape — papercuts redacts before egress.
    """

    source: FrictionSource
    kind: str
    identity: GateIdentity
    summary: str
    detail: str
    session_id: SessionId


@dataclass(frozen=True, slots=True)
class FrictionReceipt:
    """Outcome of one ``report``: ``filed`` never disagrees with ``reason``."""

    filed: bool
    reason: FrictionReason


SinkFn: TypeAlias = Callable[[GateFriction], FrictionReceipt]


def _receipt(reason: FrictionReason) -> FrictionReceipt:
    return FrictionReceipt(filed=reason == "filed", reason=reason)


class FrictionState:
    """Owner of the sink slot, the dedupe window and the counters.

    State lives on an instance, not on module globals, so a test (or a profile
    reload) can construct and reset its own window instead of reaching into
    another module's internals.
    """

    __slots__ = ("_last_error", "_reasons", "_seen", "_sink", "_sources")

    def __init__(self) -> None:
        self._sink: SinkFn | None = None
        self._seen: dict[tuple[FrictionSource, str, GateIdentity], None] = {}
        self._sources: dict[str, int] = dict.fromkeys(SOURCE_KEYS, 0)
        self._reasons: dict[str, int] = dict.fromkeys(REASONS, 0)
        self._last_error = ""

    @property
    def installed(self) -> bool:
        return self._sink is not None

    def install(self, sink: SinkFn) -> None:
        """Replace the sink. The latest installer wins; there is one filing policy."""
        self._sink = sink

    def uninstall(self) -> None:
        self._sink = None

    def reset(self) -> None:
        """Drop the sink, the dedupe window and the counters (tests, reloads)."""
        self._sink = None
        self._seen.clear()
        self._sources = dict.fromkeys(SOURCE_KEYS, 0)
        self._reasons = dict.fromkeys(REASONS, 0)
        self._last_error = ""

    def report(self, friction: GateFriction) -> FrictionReceipt:
        """File ``friction`` through the sink, or count it. Never raises.

        Without a sink the identity is NOT remembered: a plugin that reports during
        boot must not lose its papercut when papercuts enables its sink a moment
        later. Duplicates are only suppressed once something actually filed, so a
        quota or redaction refusal stays retryable.
        """
        source = friction.source if friction.source in SOURCES else UNKNOWN_SOURCE
        sink = self._sink
        if sink is None:
            return self._settle(source, "no_sink")
        key = (source, friction.kind, friction.identity)
        if key in self._seen:
            return self._settle(source, "duplicate")
        try:
            receipt = sink(friction)
        except Exception as exc:  # noqa: BLE001 - a foreign sink is untrusted code;
            # a refusal to file is an outcome, never an exception back into the gate
            self._last_error = f"{type(exc).__name__}: {exc}"[:ERROR_TEXT_LIMIT]
            return self._settle(source, "error")
        if not isinstance(receipt, FrictionReceipt):
            self._last_error = f"sink returned {type(receipt).__name__}, not FrictionReceipt"
            return self._settle(source, "error")
        if receipt.reason not in REASONS:
            self._last_error = f"sink reported unknown reason {receipt.reason!r}"
            return self._settle(source, "error")
        if receipt.reason == "filed":
            self._remember(key)
        return self._settle(source, receipt.reason)

    def stats(self) -> dict[str, object]:
        """Bounded, JSON-safe counters: the visible half of fail-open."""
        return {
            "sink_installed": self.installed,
            "sources": dict(self._sources),
            "reasons": dict(self._reasons),
            "deduped": len(self._seen),
            "last_error": self._last_error,
        }

    def _settle(self, source: str, reason: FrictionReason) -> FrictionReceipt:
        self._sources[source] = self._sources.get(source, 0) + 1
        self._reasons[reason] = self._reasons.get(reason, 0) + 1
        return _receipt(reason)

    def _remember(self, key: tuple[FrictionSource, str, GateIdentity]) -> None:
        self._seen[key] = None
        while len(self._seen) > SEEN_LIMIT:
            self._seen.pop(next(iter(self._seen)))


_STATE: Final = FrictionState()


def install_sink(sink: SinkFn) -> None:
    _STATE.install(sink)


def uninstall_sink() -> None:
    _STATE.uninstall()


def reset() -> None:
    _STATE.reset()


def report(friction: GateFriction) -> FrictionReceipt:
    return _STATE.report(friction)


def stats() -> dict[str, object]:
    return _STATE.stats()


# Register the shared slot: the second loader of this file sees the first module.
sys.modules.setdefault(SHARED_MODULE, sys.modules[__name__])
