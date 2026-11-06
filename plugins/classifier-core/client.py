"""One `/v1/systemone` transport with a process-wide breaker.

Every classifier consumer in this repository used to own its own duplicate of this
transport: the same POST, the same Bearer-from-the-environment rule and a private
per-process cooldown dictionary, so a dead service was discovered once per plugin and
per endpoint. This module is the shared one: a single `ClassifierClient` per endpoint,
handed out by `shared_client`, carrying ONE breaker so a service that is down is down
for everybody at once and a service that recovers is probed once, not once per caller.

Contract, unchanged from the transports this replaces:

* POST ``{"model", "state", "questions"}`` to ``<endpoint>/v1/systemone`` and read the
  answers from the envelope's ``"answers"`` object.
* The Bearer key is read from the environment named by ``api_key_env`` at CALL time. It
  is never stored on the client, never logged, and scrubbed from any captured error text.
* Failure never raises: ``ask``/``classify`` return ``None`` and the reason is counted
  against the caller, so a fail-open path is visible in ``status()`` instead of silent.
* ``classify`` keeps the historical label-schema API by translating each head into one
  typed choice question, scoring all heads in a single request, and back-mapping each
  answer to its original label key. ``multi_label`` heads are asked as a choice among
  their labels plus an explicit ``none`` and answered as a list of at most one label.

What this module adds is the shared breaker and the accounting: ``breaker_failures``
consecutive service errors open the breaker for ``breaker_reset`` seconds, exactly one
half-open probe request decides whether it closes again, and every transition is logged
once at WARNING with the endpoint only.

Shared state: this module registers itself in ``sys.modules["hermes_plugins_classifier_core"]``
on import, so a sibling plugin that loads it by file path (the repository's cross-plugin
idiom) gets the SAME module object — and therefore the same client registry — instead of
a second copy with its own breaker.

Logging is stdlib ``logging``, not structlog: structlog is not a host dependency (it is not
importable in the host environment), every existing plugin module in this repository logs
through stdlib ``logging``, and a plugin that cannot import its logger must not load.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType, ModuleType
from typing import Final, Literal, assert_never

logger = logging.getLogger(__name__)

MODULE_KEY: Final[str] = "hermes_plugins_classifier_core"
DEFAULT_ENDPOINT: Final[str] = "http://127.0.0.1:8102"
DEFAULT_MODEL: Final[str] = "qwen3.8-flash-next"
DEFAULT_API_KEY_ENV: Final[str] = "CLASSIFIER_API_KEY"

_PREDICT_PATH: Final[str] = "/v1/systemone"
_HEALTH_PATH: Final[str] = "/health"
# The service's contract: these two statuses are worth exactly one more attempt.
_RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 503})

DeclineReason = Literal[
    "not_configured", "breaker_open", "text_limits", "unusable_body", "service_error", "cancelled",
]
DECLINE_REASONS: Final[tuple[DeclineReason, ...]] = (
    "not_configured", "breaker_open", "text_limits", "unusable_body", "service_error", "cancelled",
)
BreakerState = Literal["closed", "open", "half_open"]

# First load wins: a second path-load of this file must not fork the client registry.
_self: ModuleType | None = sys.modules.get(__name__)
if _self is not None and MODULE_KEY not in sys.modules:
    sys.modules[MODULE_KEY] = _self


# ---------------------------------------------------------------------------- settings
@dataclass(frozen=True, slots=True)
class ClassifierSettings:
    """Frozen transport settings, built once from ``ctx.get_config`` at register time."""

    endpoint: str = DEFAULT_ENDPOINT
    served_model: str = DEFAULT_MODEL
    api_key_env: str = DEFAULT_API_KEY_ENV
    timeout_floor: float = 2.5
    max_timeout: float = 30.0
    char_seconds: float = 0.00025
    cooldown: float = 45.0
    breaker_failures: int = 3
    breaker_reset: float = 60.0
    max_text: int = 100_000
    read_cap: int = 262_144

    @classmethod
    def from_config(cls, get_config: Callable[[str, object], object]) -> ClassifierSettings:
        """Read every setting once through ``ctx.get_config``.

        A missing or ``None`` value takes the documented default. A value that is present
        but cannot be coerced, or that violates an invariant, raises ``ValueError`` at
        register time: an unusable configuration is loud, never silently reinterpreted.
        """
        settings = cls(
            endpoint=_as_str(get_config, "endpoint", DEFAULT_ENDPOINT).rstrip("/"),
            served_model=_as_str(get_config, "served_model", DEFAULT_MODEL),
            api_key_env=_as_str(get_config, "api_key_env", DEFAULT_API_KEY_ENV),
            timeout_floor=_as_float(get_config, "timeout_floor", 2.5, minimum=0.0),
            max_timeout=_as_float(get_config, "max_timeout", 30.0, minimum=0.0),
            char_seconds=_as_float(get_config, "char_seconds", 0.00025, minimum=0.0),
            cooldown=_as_float(get_config, "cooldown", 45.0, minimum=0.0),
            breaker_failures=_as_int(get_config, "breaker_failures", 3, minimum=1),
            breaker_reset=_as_float(get_config, "breaker_reset", 60.0, minimum=0.0, exclusive=True),
            max_text=_as_int(get_config, "max_text", 100_000, minimum=1),
            read_cap=_as_int(get_config, "read_cap", 262_144, minimum=1024),
        )
        if not settings.endpoint:
            raise ValueError("classifier-core: 'endpoint' must be a non-empty URL")
        if not settings.served_model:
            raise ValueError("classifier-core: 'served_model' must be non-empty")
        if not settings.api_key_env:
            raise ValueError("classifier-core: 'api_key_env' must name an environment variable")
        if settings.timeout_floor > settings.max_timeout:
            raise ValueError(
                "classifier-core: 'timeout_floor' must not exceed 'max_timeout' "
                f"({settings.timeout_floor:g} > {settings.max_timeout:g})"
            )
        return settings

    def budget(self, text_len: int, timeout: float | None = None) -> float:
        """Seconds to wait for this payload: like the transports it replaces, the floor is
        the configured timeout, the text's measured inference cost is the budget, and
        ``max_timeout`` is the ceiling. An explicit ``timeout`` wins outright."""
        if timeout is not None:
            return float(timeout)
        return min(
            self.max_timeout,
            max(self.timeout_floor, 0.5 + text_len * self.char_seconds),
        )


def _present(get_config: Callable[[str, object], object], key: str) -> object | None:
    """``get_config`` value, or ``None`` when the operator configured nothing."""
    value = get_config(key, None)
    return None if value is None else value


def _as_str(get_config: Callable[[str, object], object], key: str, default: str) -> str:
    value = _present(get_config, key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise TypeError(
            f"classifier-core: {key!r} must be a string, got {type(value).__name__}"
        )
    return value.strip()


def _as_float(
    get_config: Callable[[str, object], object],
    key: str,
    default: float,
    *,
    minimum: float,
    exclusive: bool = False,
) -> float:
    value = _present(get_config, key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"classifier-core: {key!r} must be a number, got {type(value).__name__}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"classifier-core: {key!r} must be a number, got {value!r}") from exc
    if math.isnan(number):  # NaN survives every comparison; refuse it explicitly
        raise ValueError(f"classifier-core: {key!r} must be a number, got NaN")
    if number < minimum or (exclusive and number == minimum):
        bound = ">" if exclusive else ">="
        raise ValueError(f"classifier-core: {key!r} must be {bound} {minimum:g}, got {number:g}")
    return number


def _as_int(
    get_config: Callable[[str, object], object], key: str, default: int, *, minimum: int,
) -> int:
    value = _present(get_config, key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"classifier-core: {key!r} must be an integer, got {type(value).__name__}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"classifier-core: {key!r} must be an integer, got {value!r}") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"classifier-core: {key!r} must be an integer, got {value!r}")
    if number < minimum:
        raise ValueError(f"classifier-core: {key!r} must be >= {minimum}, got {number}")
    return number


# ------------------------------------------------------------------------------ status
@dataclass(frozen=True, slots=True)
class HealthReport:
    """Result of one ``GET /health`` probe."""

    ok: bool
    status_code: int | None
    error: str | None
    latency_ms: float

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status_code": self.status_code,
            "error": self.error,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True, slots=True)
class CallerCounters:
    """Accepted requests and declines-by-reason for one caller name."""

    ok: int
    declined: Mapping[str, int]

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "declined": dict(self.declined)}


@dataclass(frozen=True, slots=True)
class ClientStatus:
    """One snapshot of the shared client: breaker, totals and per-caller counters."""

    endpoint: str
    breaker: BreakerState
    open_until: float
    consecutive_failures: int
    requests: int
    ok: int
    declined: int
    callers: Mapping[str, CallerCounters]
    last_error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "breaker": self.breaker,
            "open_until": self.open_until,
            "consecutive_failures": self.consecutive_failures,
            "totals": {"requests": self.requests, "ok": self.ok, "declined": self.declined},
            "callers": {name: counters.as_dict() for name, counters in self.callers.items()},
            "last_error": self.last_error,
        }


class _CallerState:
    """Mutable per-caller tallies; snapshotted into frozen ``CallerCounters``."""

    __slots__ = ("declined", "ok")

    def __init__(self) -> None:
        self.ok = 0
        self.declined: dict[str, int] = {reason: 0 for reason in DECLINE_REASONS}


class _Breaker:
    """Consecutive-failure breaker with exactly one half-open probe.

    Opened by ``breaker_failures`` consecutive service errors; after ``breaker_reset``
    seconds a single probe is admitted, and its verdict decides closed or open again.
    Every transition logs once at WARNING with the endpoint only.
    """

    __slots__ = ("_failures", "_open_until", "_probe_inflight", "_state")

    def __init__(self) -> None:
        self._state: BreakerState = "closed"
        self._failures = 0
        self._open_until = 0.0
        self._probe_inflight = False

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def failures(self) -> int:
        return self._failures

    def remaining(self, now: float) -> float:
        """Seconds until the breaker may probe again (0.0 unless it is open)."""
        if self._state == "closed":
            return 0.0
        return max(0.0, self._open_until - now)

    def admit(self, endpoint: str, now: float) -> bool:
        """True when this request may go out; the open -> half-open flip is the probe."""
        match self._state:
            case "closed":
                return True
            case "open":
                if now < self._open_until:
                    return False
                self._state = "half_open"
                self._probe_inflight = True
                logger.warning(
                    "classifier-core: breaker half-open for %s; sending one probe", endpoint,
                )
                return True
            case "half_open":
                return False  # the in-flight probe owns the decision
            case _:
                assert_never(self._state)

    def record_success(self, endpoint: str) -> None:
        was = self._state
        self._state, self._failures, self._open_until, self._probe_inflight = "closed", 0, 0.0, False
        if was != "closed":
            logger.warning("classifier-core: breaker closed for %s after a successful request", endpoint)

    def record_failure(
        self, endpoint: str, now: float, *, failures: int, reset_after: float,
    ) -> None:
        was = self._state
        self._failures += 1
        self._probe_inflight = False
        if was == "closed" and self._failures < failures:
            return
        self._state = "open"
        self._open_until = now + reset_after
        logger.warning(
            "classifier-core: breaker open for %s after %d consecutive service errors "
            "(probe in %gs)", endpoint, self._failures, reset_after,
        )


# --------------------------------------------------------------------------- transport
@dataclass(frozen=True, slots=True)
class _Payload:
    """A completed HTTP exchange, whatever its status code."""

    status: int
    raw: bytes


@dataclass(frozen=True, slots=True)
class _TransportError:
    """A failed exchange; ``cancelled`` separates host cancellation from a service error."""

    message: str
    cancelled: bool = False


_Outcome = _Payload | _TransportError


_REGISTRY: Final[dict[str, ClassifierClient]] = {}
_REGISTRY_LOCK: Final[threading.Lock] = threading.Lock()


class ClassifierClient:
    """Shared `/v1/systemone` client for one endpoint.

    ``ask`` sends the raw typed-question envelope; ``classify`` scores a label schema;
    ``health`` probes liveness; ``status`` reports the breaker, totals and per-caller
    declines. Nothing here raises on a service failure: the caller gets ``None`` and the
    reason is counted.
    """

    def __init__(
        self,
        settings: ClassifierSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        self.settings = settings
        self._clock = clock
        self._opener = opener
        self._breaker = _Breaker()
        self._counters: dict[str, _CallerState] = {}
        self._requests = 0
        self._ok = 0
        self._declined = 0
        self._last_error: str | None = None

    # ------------------------------------------------------------------ public API
    def ask(
        self,
        state: str,
        questions: dict[str, object],
        *,
        caller: str,
        timeout: float | None = None,
    ) -> dict[str, object] | None:
        """POST ``{model, state, questions}`` to ``<endpoint>/v1/systemone``.

        Returns the raw parsed envelope (``{"answers": {...}}``) or ``None``. Never
        raises; every decline is counted against ``caller`` and visible in ``status()``.
        """
        envelope, reason = self._exchange(
            state, questions, text_len=len(state), timeout=timeout,
        )
        self._record(caller, reason)
        return envelope

    def classify(
        self,
        text: str,
        schema: Mapping[str, object],
        *,
        caller: str,
        timeout: float | None = None,
    ) -> dict[str, object] | None:
        """Translate a label schema to typed questions, score them in ONE pass, and
        back-map each answer to its original label key; ``None`` on any failure.

        Each head ``{labels: {key: description}}`` becomes one choice question whose
        criteria carry the descriptions verbatim; the answer key is validated against the
        offered set. ``multi_label`` heads also see a ``none`` option and answer as a list
        of at most one label. A head with no usable answer yields no key, which is the
        shape callers already tolerate when a head abstains.
        """
        translated = _questions_from_schema(schema)
        if translated is None:
            self._record(caller, "text_limits")
            return None
        questions, multiheads = translated
        envelope, reason = self._exchange(text, questions, text_len=len(text), timeout=timeout)
        if envelope is None:
            self._record(caller, reason)
            return None
        labels_out = _labels_from_answers(questions, multiheads, envelope.get("answers"))
        if not labels_out:
            self._unusable("no answerable head in the returned envelope")
            self._record(caller, "unusable_body")
            return None
        self._record(caller, None)
        return labels_out

    def health(self, timeout: float = 2.0) -> HealthReport:
        """GET ``<endpoint>/health``.

        A liveness probe, never scored: it does not touch the breaker, the totals or the
        per-caller counters, and it carries no Authorization header — the key must not
        ride a request that cannot use it.
        """
        endpoint = self.settings.endpoint
        if not endpoint:
            return HealthReport(ok=False, status_code=None, error="not_configured", latency_ms=0.0)
        request = urllib.request.Request(endpoint + _HEALTH_PATH, method="GET")
        started = self._clock()
        try:
            with contextlib.closing(self._opener(request, timeout=timeout)) as reply:
                status = _reply_status(reply)
        except asyncio.CancelledError:
            return HealthReport(
                ok=False, status_code=None, error="cancelled",
                latency_ms=_elapsed_ms(self._clock, started),
            )
        except urllib.error.HTTPError as exc:
            return HealthReport(
                ok=False, status_code=exc.code,
                error=self._scrub(f"{type(exc).__name__}: HTTP {exc.code}"),
                latency_ms=_elapsed_ms(self._clock, started),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return HealthReport(
                ok=False, status_code=None,
                error=self._scrub(f"{type(exc).__name__}: {exc}"),
                latency_ms=_elapsed_ms(self._clock, started),
            )
        ok = 200 <= status < 300
        return HealthReport(
            ok=ok, status_code=status, error=None if ok else f"HTTP {status}",
            latency_ms=_elapsed_ms(self._clock, started),
        )

    def status(self) -> ClientStatus:
        """Snapshot of the breaker, totals and every caller's counters."""
        now = self._clock()
        callers = {
            name: CallerCounters(ok=state.ok, declined=MappingProxyType(dict(state.declined)))
            for name, state in sorted(self._counters.items())
        }
        return ClientStatus(
            endpoint=self.settings.endpoint,
            breaker=self._breaker.state,
            open_until=round(self._breaker.remaining(now), 3),
            consecutive_failures=self._breaker.failures,
            requests=self._requests,
            ok=self._ok,
            declined=self._declined,
            callers=MappingProxyType(callers),
            last_error=self._last_error,
        )

    # ------------------------------------------------------------------- internals
    def _key(self) -> str:
        """Bearer key from the environment, read at CALL time; never stored or logged."""
        return os.environ.get(self.settings.api_key_env, "").strip()

    def _scrub(self, text: str) -> str:
        """Remove the live Bearer key from text that may reach a status field or a log."""
        key = self._key()
        return text.replace(key, "***") if key and key in text else text

    def _record(self, caller: str, reason: DeclineReason | None) -> None:
        self._requests += 1
        state = self._counters.setdefault(caller, _CallerState())
        if reason is None:
            state.ok += 1
            self._ok += 1
            return
        state.declined[reason] += 1
        self._declined += 1

    def _unusable(self, message: str) -> None:
        """Record an unusable body; the breaker is untouched (the service answered)."""
        self._last_error = self._scrub(message)

    def _trip(self, endpoint: str, message: str) -> DeclineReason:
        """Record a service error and let the breaker decide whether to open."""
        self._last_error = self._scrub(message)
        self._breaker.record_failure(
            endpoint,
            self._clock(),
            failures=self.settings.breaker_failures,
            reset_after=self.settings.breaker_reset,
        )
        return "service_error"

    def _exchange(
        self,
        state: str,
        questions: Mapping[str, object],
        *,
        text_len: int,
        timeout: float | None,
    ) -> tuple[dict[str, object] | None, DeclineReason | None]:
        """Guards, breaker admission and one scored request; returns (envelope, reason)."""
        endpoint = self.settings.endpoint
        key = self._key()
        if not endpoint or not key:
            return None, "not_configured"
        if text_len <= 0 or text_len > self.settings.max_text:
            return None, "text_limits"
        if not self._breaker.admit(endpoint, self._clock()):
            return None, "breaker_open"
        budget = self.settings.budget(text_len, timeout)
        outcome = self._send(endpoint, key, state, questions, budget)
        match outcome:
            case _TransportError(cancelled=True):
                return None, "cancelled"
            case _TransportError(message=message):
                return None, self._trip(endpoint, message)
            case _Payload(status=status, raw=raw):
                return self._decode(endpoint, status, raw)
            case _:
                assert_never(outcome)

    def _send(
        self,
        endpoint: str,
        key: str,
        state: str,
        questions: Mapping[str, object],
        budget: float,
        *,
        attempt: int = 0,
    ) -> _Outcome:
        """One POST; 429/503 get exactly one more attempt, everything else ends here."""
        request = urllib.request.Request(
            endpoint + _PREDICT_PATH,
            data=json.dumps(
                {"model": self.settings.served_model, "state": state, "questions": questions},
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with contextlib.closing(self._opener(request, timeout=budget)) as reply:
                status = _reply_status(reply)
                raw = reply.read(self.settings.read_cap)
        except asyncio.CancelledError:
            return _TransportError("request cancelled before a reply", cancelled=True)
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRY_STATUSES and attempt == 0:
                return self._send(endpoint, key, state, questions, budget, attempt=1)
            return _TransportError(self._scrub(f"{type(exc).__name__}: HTTP {exc.code}"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return _TransportError(self._scrub(f"{type(exc).__name__}: {exc}"))
        if status in _RETRY_STATUSES and attempt == 0:
            return self._send(endpoint, key, state, questions, budget, attempt=1)
        return _Payload(status=status, raw=raw)

    def _decode(
        self, endpoint: str, status: int, raw: bytes,
    ) -> tuple[dict[str, object] | None, DeclineReason | None]:
        """Turn one completed exchange into an envelope or a counted decline."""
        if not 200 <= status < 300:
            return None, self._trip(endpoint, f"HTTPError: HTTP {status}")
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            self._unusable("response body is not JSON")
            return None, "unusable_body"
        answers = body.get("answers") if isinstance(body, dict) else None
        if not isinstance(answers, dict):
            self._unusable(f"response body {type(body).__name__} carries no answers object")
            return None, "unusable_body"
        self._breaker.record_success(endpoint)
        return body, None


def _reply_status(reply: object) -> int:
    """HTTP status of a urllib reply, tolerating the objects tests inject."""
    status = getattr(reply, "status", None)
    if status is None:
        getcode = getattr(reply, "getcode", None)
        status = getcode() if callable(getcode) else 200
    return status if isinstance(status, int) else 200


def _elapsed_ms(clock: Callable[[], float], started: float) -> float:
    """Duration in milliseconds from an injected monotonic clock."""
    return round((clock() - started) * 1000.0, 3)


def _questions_from_schema(
    schema: Mapping[str, object],
) -> tuple[dict[str, dict[str, object]], dict[str, bool]] | None:
    """Translate a label schema into typed questions, verbatim from the transport this
    replaces. ``None`` when the schema declares nothing answerable."""
    if not schema:
        return None
    questions: dict[str, dict[str, object]] = {}
    multiheads: dict[str, bool] = {}
    for head, spec in schema.items():
        if isinstance(spec, Mapping) and "labels" in spec:
            labels = spec["labels"]
            multi = bool(spec.get("multi_label"))
        elif isinstance(spec, list):
            labels, multi = {str(k): str(k) for k in spec}, False
        else:
            return None
        if isinstance(labels, list):  # bare label lists carry no descriptions
            labels = {str(k): str(k) for k in labels}
        if not isinstance(labels, Mapping) or not labels:
            return None
        criteria = {str(label): str(labels.get(label, label)) for label in labels}
        if multi:
            criteria["none"] = "none of the listed labels applies"
            multiheads[str(head)] = True
        questions[str(head)] = {"type": "choice", "criteria": criteria}
    return questions, multiheads


def _labels_from_answers(
    questions: Mapping[str, Mapping[str, object]],
    multiheads: Mapping[str, bool],
    answers: object,
) -> dict[str, object]:
    """Back-map answered heads to label keys; an unanswered head stays absent."""
    if not isinstance(answers, Mapping):
        return {}
    labels_out: dict[str, object] = {}
    for head, question in questions.items():
        answer = answers.get(head)
        choice = answer.get("choice") if isinstance(answer, Mapping) else None
        key = str(choice) if choice is not None else None
        criteria = question.get("criteria")
        if key is None or not isinstance(criteria, Mapping) or key not in criteria:
            continue  # head unanswered or off-menu: absent key, never a guess
        if multiheads.get(head):
            labels_out[head] = [] if key == "none" else [key]
        else:
            labels_out[head] = key
    return labels_out


def shared_client(
    settings: ClassifierSettings,
    *,
    clock: Callable[[], float] = time.monotonic,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> ClassifierClient:
    """The process-wide client for ``settings.endpoint``.

    Keyed by endpoint ONLY (trailing slashes are not a second service): an endpoint that
    already has a client returns that client, ignoring a later ``clock``/``opener``,
    because the whole point of the registry is one breaker shared by every consumer instead
    of one private outage per plugin.
    """
    endpoint = settings.endpoint.rstrip("/")
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(endpoint)
        if existing is not None:
            return existing
        client = ClassifierClient(settings, clock=clock, opener=opener)
        _REGISTRY[endpoint] = client
        return client


__all__ = [
    "DECLINE_REASONS",
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_ENDPOINT",
    "DEFAULT_MODEL",
    "MODULE_KEY",
    "BreakerState",
    "CallerCounters",
    "ClassifierClient",
    "ClassifierSettings",
    "ClientStatus",
    "DeclineReason",
    "HealthReport",
    "shared_client",
]
