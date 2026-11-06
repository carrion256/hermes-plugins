"""Shadow-gate ledger: append-only decision evidence plus a pure promotion evaluator.

A *shadow* decision (classifier tier, routing suggestion, gate verdict — anything
whose suggestion is recorded but never applied) is only promotable with evidence:
paired verdict/outcome rows per decision, an explicit operator policy, and gates
that fail loudly. This module owns the storage and the arithmetic. It decides
nothing by itself and NEVER qualifies a decision without a policy.

Storage law
-----------
One JSONL file per decision at ``root / f"{decision}.jsonl"`` — the decision name
IS the filename, so it is validated against ``^[a-z][a-z0-9_.-]{0,63}$`` before
any path is built (a traversal attempt raises ``ValueError``, it is not silently
sanitized). Rows are appended with ``O_APPEND`` and one ``os.write`` per row, so
concurrent producers interleave whole lines and never partial ones. No fsync is
performed: a torn tail line is skipped and counted as corrupt on read, which is
the documented tolerance. Rotation keeps the newest ``max_rows_per_decision``
physical rows and rewrites through ``os.replace``, so a reader sees either the
old file or the new one, never a truncated one.

Process-wide sharing
--------------------
Producers live in other plugins and load modules by file path, so this module
registers itself in ``sys.modules`` under ``hermes_plugins_shadow_gate`` at
import time. Loaders MUST check ``sys.modules`` first and reuse that instance:
re-executing this file forks the per-decision row-count cache and the append
lock, and two ledgers rotating the same file would do it from stale counts.

Join law
--------
An outcome pairs with the LATEST verdict for the same key that appears earlier in
the file and is not stamped after the outcome (equal timestamps are allowed; file
order breaks the tie). Every verdict that supplied no pair — including one
superseded by a later verdict — is reported as an unpaired verdict, so no row
silently disappears from the accounting.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, NewType

# Stdlib logging, not structlog: the Hermes host ships no structlog (it is not an
# installed dependency), and this module must import under the plugin loader with
# stdlib-only dependencies. See the repo house posture on no new dependencies.
logger = logging.getLogger(__name__)

MODULE_NAME: Final = "hermes_plugins_shadow_gate"
# Self-registration: one ledger module instance per process, shared by every
# path-loader that checks sys.modules first (see "Process-wide sharing" above).
if MODULE_NAME not in sys.modules:
    sys.modules[MODULE_NAME] = sys.modules[__name__]

SCHEMA_VERSION: Final = 1
DECISION_PATTERN: Final = r"^[a-z][a-z0-9_.-]{0,63}$"
REASON_NO_POLICY: Final = "no_policy"
REASON_INSUFFICIENT_PAIRS: Final = "insufficient_pairs"
REASON_LOW_AGREEMENT: Final = "low_agreement"
REASON_FALSE_POSITIVE_RATE: Final = "false_positive_rate"
NOT_QUALIFIED: Final = "NOT_QUALIFIED"
QUALIFIED: Final = "QUALIFIED"

_DECISION_RE: Final = re.compile(DECISION_PATTERN)
_SUFFIX: Final = ".jsonl"
_KIND_VERDICT: Final = "verdict"
_KIND_OUTCOME: Final = "outcome"

DecisionId = NewType("DecisionId", str)
Status = Literal["NOT_QUALIFIED", "QUALIFIED"]


def utc_now() -> datetime:
    """Aware UTC instant; the default ledger clock."""
    return datetime.now(UTC)


def validate_decision(name: str) -> DecisionId:
    """Validate a decision name, which is also its ledger filename.

    Rejects traversal, absolute paths, empty names, uppercase and over-long
    names outright — an invalid name is a caller bug, never a filtered path.
    """
    if not isinstance(name, str):
        raise TypeError(
            f"invalid shadow-gate decision name: expected a string "
            f"(got {type(name).__name__})"
        )
    if _DECISION_RE.fullmatch(name) is None:
        raise ValueError(
            f"invalid shadow-gate decision name {name!r}: expected {DECISION_PATTERN} "
            "(the name is the ledger filename)"
        )
    return DecisionId(name)


def _require_text(raw: object, label: str) -> str:
    if not isinstance(raw, str):
        raise TypeError(f"shadow-gate: {label} must be a string (got {type(raw).__name__})")
    if not raw:
        raise ValueError(f"shadow-gate: {label} must be a non-empty string (got {raw!r})")
    return raw


def _require_utc(instant: datetime, label: str) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(f"shadow-gate: {label} must be timezone-aware UTC")
    return instant.astimezone(UTC)


def _require_meta(meta: Mapping[str, str] | None) -> Mapping[str, str]:
    if meta is None:
        return MappingProxyType({})
    frozen: dict[str, str] = {}
    for key, value in meta.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError(
                "shadow-gate: meta must map strings to strings "
                f"(got {type(key).__name__} -> {type(value).__name__})"
            )
        frozen[key] = value
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class VerdictRecord:
    """One recorded shadow verdict. ``v`` trails the fields so it can default."""

    decision: str
    key: str
    input_digest: str
    verdict: str
    recorded_at: datetime
    meta: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    v: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "v": self.v,
            "kind": _KIND_VERDICT,
            "decision": self.decision,
            "key": self.key,
            "input_digest": self.input_digest,
            "verdict": self.verdict,
            "recorded_at": self.recorded_at.astimezone(UTC).isoformat(),
            "meta": dict(self.meta),
        }


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One recorded real-world outcome for a key. ``v`` trails the fields so it can default."""

    decision: str
    key: str
    outcome: str
    recorded_at: datetime
    meta: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    v: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "v": self.v,
            "kind": _KIND_OUTCOME,
            "decision": self.decision,
            "key": self.key,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at.astimezone(UTC).isoformat(),
            "meta": dict(self.meta),
        }


@dataclass(frozen=True, slots=True)
class Pair:
    """One joined verdict/outcome observation."""

    decision: str
    key: str
    verdict: VerdictRecord
    outcome: OutcomeRecord


@dataclass(frozen=True, slots=True)
class LedgerView:
    """Everything one decision's file says, with the unpaired remainder kept visible."""

    pairs: tuple[Pair, ...]
    unpaired_verdicts: tuple[VerdictRecord, ...]
    unpaired_outcomes: tuple[OutcomeRecord, ...]
    corrupt_lines: int


def _parse_instant(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # Tolerated and normalized: a naive stamp can only have come from a
        # hand-written row, and refusing it would lose otherwise usable evidence.
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_meta(raw: object) -> Mapping[str, str] | None:
    if raw is None:
        return MappingProxyType({})
    if not isinstance(raw, Mapping):
        return None
    parsed: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        parsed[key] = value
    return MappingProxyType(parsed)


def _parse_verdict(raw: Mapping[str, object]) -> VerdictRecord | None:
    if raw.get("v") != SCHEMA_VERSION:
        return None
    decision = raw.get("decision")
    key = raw.get("key")
    digest = raw.get("input_digest")
    verdict = raw.get("verdict")
    if not isinstance(decision, str) or not isinstance(key, str):
        return None
    if not isinstance(digest, str) or not isinstance(verdict, str):
        return None
    if not decision or not key or not digest or not verdict:
        return None
    recorded_at = _parse_instant(raw.get("recorded_at"))
    meta = _parse_meta(raw.get("meta"))
    if recorded_at is None or meta is None:
        return None
    return VerdictRecord(
        decision=decision,
        key=key,
        input_digest=digest,
        verdict=verdict,
        recorded_at=recorded_at,
        meta=meta,
    )


def _parse_outcome(raw: Mapping[str, object]) -> OutcomeRecord | None:
    if raw.get("v") != SCHEMA_VERSION:
        return None
    decision = raw.get("decision")
    key = raw.get("key")
    outcome = raw.get("outcome")
    if not isinstance(decision, str) or not isinstance(key, str) or not isinstance(outcome, str):
        return None
    if not decision or not key or not outcome:
        return None
    recorded_at = _parse_instant(raw.get("recorded_at"))
    meta = _parse_meta(raw.get("meta"))
    if recorded_at is None or meta is None:
        return None
    return OutcomeRecord(
        decision=decision, key=key, outcome=outcome, recorded_at=recorded_at, meta=meta
    )


def _row_kind(raw: Mapping[str, object]) -> str | None:
    """``kind`` discriminator; inferred only for hand-written rows that lack it."""
    declared = raw.get("kind")
    if isinstance(declared, str):
        return declared if declared in (_KIND_VERDICT, _KIND_OUTCOME) else None
    if "verdict" in raw:
        return _KIND_VERDICT
    if "outcome" in raw:
        return _KIND_OUTCOME
    return None


def _atomic_write(path: Path, text: str) -> None:
    """tmp + ``os.replace`` in the same directory: readers never see a partial file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        # Best-effort removal of our own partial temp file; the write error itself
        # is re-raised unchanged so the caller sees the real failure.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _row_line(record: VerdictRecord | OutcomeRecord) -> str:
    """Serialize one record as exactly one JSONL line (shared by both row kinds)."""
    return f"{json.dumps(record.to_dict(), ensure_ascii=False)}\n"


class ShadowLedger:
    """Append-only per-decision evidence store with atomic rotation.

    ``now`` is injected so callers (and tests) can control stamps; the filesystem
    is the only other effect. ``max_rows_per_decision`` bounds each decision's
    file; exceeding it rewrites the newest rows atomically.
    """

    def __init__(
        self,
        root: Path,
        *,
        now: Callable[[], datetime],
        max_rows_per_decision: int = 5000,
    ) -> None:
        if max_rows_per_decision < 1:
            raise ValueError(
                "shadow-gate: max_rows_per_decision must be >= 1 "
                f"(got {max_rows_per_decision!r})"
            )
        self._root = Path(root).expanduser()
        self._now = now
        self._max_rows = max_rows_per_decision
        self._lock = threading.Lock()
        # Lazily primed per decision from the file itself; guarded by _lock.
        self._rows: dict[DecisionId, int] = {}

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_rows_per_decision(self) -> int:
        return self._max_rows

    def _path(self, decision: DecisionId) -> Path:
        return self._root / f"{decision}{_SUFFIX}"

    def record_verdict(
        self,
        decision: str,
        key: str,
        input_digest: str,
        verdict: str,
        *,
        meta: Mapping[str, str] | None = None,
        recorded_at: datetime | None = None,
    ) -> VerdictRecord:
        """Append one verdict row and return the record as persisted."""
        validated = validate_decision(decision)
        record = VerdictRecord(
            decision=str(validated),
            key=_require_text(key, "key"),
            input_digest=_require_text(input_digest, "input_digest"),
            verdict=_require_text(verdict, "verdict"),
            recorded_at=self._stamp(recorded_at),
            meta=_require_meta(meta),
        )
        self._append(validated, _row_line(record))
        return record

    def record_outcome(
        self,
        decision: str,
        key: str,
        outcome: str,
        *,
        meta: Mapping[str, str] | None = None,
        recorded_at: datetime | None = None,
    ) -> OutcomeRecord:
        """Append one outcome row and return the record as persisted."""
        validated = validate_decision(decision)
        record = OutcomeRecord(
            decision=str(validated),
            key=_require_text(key, "key"),
            outcome=_require_text(outcome, "outcome"),
            recorded_at=self._stamp(recorded_at),
            meta=_require_meta(meta),
        )
        self._append(validated, _row_line(record))
        return record

    def _stamp(self, recorded_at: datetime | None) -> datetime:
        return _require_utc(self._now() if recorded_at is None else recorded_at, "recorded_at")

    def _append(self, decision: DecisionId, line: str) -> None:
        with self._lock:
            path = self._path(decision)
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o644
            )
            try:
                view = memoryview(line.encode("utf-8"))
                while view:
                    view = view[os.write(descriptor, view):]
            finally:
                os.close(descriptor)
            count = self._rows.get(decision)
            count = self._count_rows(path) if count is None else count + 1
            self._rows[decision] = count
            if count > self._max_rows:
                self._rotate(decision)

    def _count_rows(self, path: Path) -> int:
        try:
            with path.open("rb") as handle:
                return sum(1 for _line in handle)
        except FileNotFoundError:
            return 0

    def _rotate(self, decision: DecisionId) -> None:
        """Keep the newest ``max_rows_per_decision`` physical rows. Caller holds the lock."""
        path = self._path(decision)
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                lines = handle.readlines()
        except FileNotFoundError:
            self._rows[decision] = 0
            return
        if len(lines) <= self._max_rows:
            self._rows[decision] = len(lines)
            return
        kept = lines[-self._max_rows:]
        if kept and not kept[-1].endswith("\n"):
            kept[-1] = f"{kept[-1]}\n"
        _atomic_write(path, "".join(kept))
        self._rows[decision] = len(kept)
        logger.info("shadow-gate: rotated %s ledger, kept %d newest rows",
                    decision, len(kept))

    def snapshot(self, decision: str) -> LedgerView:
        """Read one decision's file: joined pairs plus everything left unpaired.

        Corruption is tolerated by design — bad lines are skipped and counted, and
        a single warning names how many were dropped (fail-open, but visible).
        """
        validated = validate_decision(decision)
        verdict_rows: list[tuple[int, VerdictRecord]] = []
        outcome_rows: list[tuple[int, OutcomeRecord]] = []
        corrupt = 0
        with self._lock:
            path = self._path(validated)
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for index, line in enumerate(handle):
                        text = line.strip()
                        if not text:
                            continue
                        try:
                            raw: object = json.loads(text)
                        except json.JSONDecodeError:
                            corrupt += 1
                            continue
                        if not isinstance(raw, Mapping):
                            corrupt += 1
                            continue
                        kind = _row_kind(raw)
                        if kind == _KIND_VERDICT:
                            verdict = _parse_verdict(raw)
                            if verdict is None:
                                corrupt += 1
                            else:
                                verdict_rows.append((index, verdict))
                        elif kind == _KIND_OUTCOME:
                            outcome = _parse_outcome(raw)
                            if outcome is None:
                                corrupt += 1
                            else:
                                outcome_rows.append((index, outcome))
                        else:
                            corrupt += 1
            except FileNotFoundError:
                return LedgerView((), (), (), 0)
        if corrupt:
            logger.warning("shadow-gate: %s ledger skipped %d corrupt row(s)",
                           validated, corrupt)
        return _join(validated, verdict_rows, outcome_rows, corrupt)

    def pairs(self, decision: str) -> tuple[Pair, ...]:
        """The joined verdict/outcome pairs of one decision."""
        return self.snapshot(decision).pairs

    def decisions(self) -> tuple[str, ...]:
        """Decision names with a ledger file, sorted; invalid filenames are ignored."""
        if not self._root.exists():
            return ()
        names: list[str] = []
        for entry in sorted(self._root.iterdir()):
            if not entry.name.endswith(_SUFFIX) or not entry.is_file():
                continue
            stem = entry.name[: -len(_SUFFIX)]
            if _DECISION_RE.fullmatch(stem) is not None:
                names.append(stem)
        return tuple(names)


def _join(
    decision: DecisionId,
    verdict_rows: Sequence[tuple[int, VerdictRecord]],
    outcome_rows: Sequence[tuple[int, OutcomeRecord]],
    corrupt: int,
) -> LedgerView:
    """Pair each outcome with the latest eligible verdict; keep the remainder visible."""
    by_key: dict[str, list[tuple[int, VerdictRecord]]] = {}
    for index, verdict in verdict_rows:
        by_key.setdefault(verdict.key, []).append((index, verdict))
    pairs: list[Pair] = []
    used: set[int] = set()
    unpaired_outcomes: list[OutcomeRecord] = []
    for index, outcome in outcome_rows:
        winner: tuple[datetime, int, VerdictRecord] | None = None
        # ponytail: linear scan of one key's verdicts per outcome; the file is
        # capped at max_rows_per_decision so the worst case stays bounded. A
        # per-key timestamp index is the upgrade path if the cap ever grows.
        for verdict_index, verdict in by_key.get(outcome.key, ()):
            if verdict_index >= index or verdict.recorded_at > outcome.recorded_at:
                continue
            candidate = (verdict.recorded_at, verdict_index, verdict)
            if winner is None or candidate[:2] > winner[:2]:
                winner = candidate
        if winner is None:
            unpaired_outcomes.append(outcome)
            continue
        used.add(winner[1])
        pairs.append(
            Pair(decision=str(decision), key=outcome.key, verdict=winner[2], outcome=outcome)
        )
    unpaired_verdicts = tuple(
        verdict for index, verdict in verdict_rows if index not in used
    )
    return LedgerView(
        pairs=tuple(pairs),
        unpaired_verdicts=unpaired_verdicts,
        unpaired_outcomes=tuple(unpaired_outcomes),
        corrupt_lines=corrupt,
    )


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    """An operator's explicit promotion gates for one decision.

    Empty positive sets are refused: with nothing positive, "agreement" would be
    vacuously perfect and a decision would qualify itself.
    """

    min_pairs: int
    min_agreement: float
    max_false_positive: float
    positive_verdicts: frozenset[str]
    positive_outcomes: frozenset[str]

    def __post_init__(self) -> None:
        if self.min_pairs < 1:
            raise ValueError(
                f"shadow-gate policy: min_pairs must be >= 1 (got {self.min_pairs!r})"
            )
        for label, value in (
            ("min_agreement", self.min_agreement),
            ("max_false_positive", self.max_false_positive),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(
                    f"shadow-gate policy: {label} must be within [0, 1] (got {value!r})"
                )
        for label, values in (
            ("positive_verdicts", self.positive_verdicts),
            ("positive_outcomes", self.positive_outcomes),
        ):
            if not values:
                raise ValueError(f"shadow-gate policy: {label} must not be empty")
            if any(not isinstance(item, str) or not item for item in values):
                raise ValueError(
                    f"shadow-gate policy: {label} must contain non-empty strings"
                )


def promotion_policy(
    *,
    min_pairs: int,
    min_agreement: float,
    max_false_positive: float,
    positive_verdicts: Iterable[str],
    positive_outcomes: Iterable[str],
) -> PromotionPolicy:
    """Build a validated policy from already-parsed config values.

    The frozensets are made here so a caller that parsed a list cannot later
    mutate the thresholds out from under an in-flight evaluation.
    """
    return PromotionPolicy(
        min_pairs=min_pairs,
        min_agreement=min_agreement,
        max_false_positive=max_false_positive,
        positive_verdicts=frozenset(positive_verdicts),
        positive_outcomes=frozenset(positive_outcomes),
    )


@dataclass(frozen=True, slots=True)
class PromotionReport:
    """The measured evidence for one decision, with every failed gate named."""

    pairs: int
    agreement: float
    false_positive_rate: float
    unpaired_verdicts: int
    unpaired_outcomes: int
    status: Status
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "pairs": self.pairs,
            "agreement": self.agreement,
            "false_positive_rate": self.false_positive_rate,
            "unpaired_verdicts": self.unpaired_verdicts,
            "unpaired_outcomes": self.unpaired_outcomes,
            "status": self.status,
            "reasons": list(self.reasons),
        }


def evaluate(
    pairs: Sequence[Pair],
    policy: PromotionPolicy | None,
    *,
    unpaired_verdicts: int = 0,
    unpaired_outcomes: int = 0,
) -> PromotionReport:
    """Apply ``policy`` to ``pairs``. A missing policy is never a qualification.

    Rate gates are only meaningful once ``min_pairs`` observations exist, so an
    under-sampled decision reports ``insufficient_pairs`` alone; the measured
    agreement/false-positive values are still reported for inspection.
    """
    agreement = 0.0
    false_positive_rate = 0.0
    if policy is None:
        return PromotionReport(
            pairs=len(pairs),
            agreement=agreement,
            false_positive_rate=false_positive_rate,
            unpaired_verdicts=unpaired_verdicts,
            unpaired_outcomes=unpaired_outcomes,
            status=NOT_QUALIFIED,
            reasons=(REASON_NO_POLICY,),
        )
    agree = 0
    verdict_positive = 0
    false_positive = 0
    for pair in pairs:
        verdict_is_positive = pair.verdict.verdict in policy.positive_verdicts
        outcome_is_positive = pair.outcome.outcome in policy.positive_outcomes
        if verdict_is_positive == outcome_is_positive:
            agree += 1
        if verdict_is_positive:
            verdict_positive += 1
            if not outcome_is_positive:
                false_positive += 1
    if pairs:
        agreement = agree / len(pairs)
    if verdict_positive:
        false_positive_rate = false_positive / verdict_positive
    reasons: list[str] = []
    if len(pairs) < policy.min_pairs:
        reasons.append(REASON_INSUFFICIENT_PAIRS)
    else:
        if agreement < policy.min_agreement:
            reasons.append(REASON_LOW_AGREEMENT)
        if false_positive_rate > policy.max_false_positive:
            reasons.append(REASON_FALSE_POSITIVE_RATE)
    return PromotionReport(
        pairs=len(pairs),
        agreement=agreement,
        false_positive_rate=false_positive_rate,
        unpaired_verdicts=unpaired_verdicts,
        unpaired_outcomes=unpaired_outcomes,
        status=NOT_QUALIFIED if reasons else QUALIFIED,
        reasons=tuple(reasons),
    )


_SHARED: dict[Path, ShadowLedger] = {}
_SHARED_LOCK = threading.Lock()


def shared_ledger(
    root: Path,
    *,
    now: Callable[[], datetime] | None = None,
    max_rows_per_decision: int = 5000,
) -> ShadowLedger:
    """The process-wide ledger for ``root`` (later calls reuse the first instance).

    Sharing matters across plugins: producers and readers must agree on one row
    cache and one append lock, or rotation races against a stale count.
    """
    key = Path(root).expanduser().resolve()
    with _SHARED_LOCK:
        existing = _SHARED.get(key)
        if existing is not None:
            return existing
        created = ShadowLedger(
            key,
            now=now if now is not None else utc_now,
            max_rows_per_decision=max_rows_per_decision,
        )
        _SHARED[key] = created
        return created
