"""Optional classifier tie-break for ambiguous keyword hits.

The only semantic seam in keyword-steering: asked solely when a hit is a common
word or negated. The transport is classifier-core's shared client -- this plugin
declares no endpoint, no served model and no key environment variable, and the
Bearer key is read from the environment there, per call, never stored or logged.
Sharing that client is the point: an outage is the service's, so the breaker that
keeps a dead endpoint from being re-probed by every call is one breaker for every
consumer, and :meth:`Tiebreak.status` reports *that* breaker rather than a private
outage timer nobody else agrees with.

Any failure returns ``None`` and the caller declines the ambiguous hit; the
deterministic path never comes here.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol, cast

logger = logging.getLogger(__name__)

LABELS = ("directive", "mention", "negated")
# The classifier-core entry: its settings, not this plugin's config, describe the service.
_CLASSIFIER_PLUGIN: Final[str] = "classifier-core"
_CLASSIFIER_MODULE_KEY: Final[str] = "hermes_plugins_classifier_core"
_CLASSIFIER_CLIENT_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / _CLASSIFIER_PLUGIN / "client.py")
# Distinct per consumer, so the shared status surface attributes a decline to this plugin.
_CALLER: Final[str] = "keyword-steering"
_HEAD: Final[str] = "steering"
DEFAULT_TIMEOUT: Final[float] = 1.5
TRANSPORT_SHARED: Final[str] = "classifier-core"
TRANSPORT_DISABLED: Final[str] = "tiebreak_disabled"
CLIENT_UNAVAILABLE: Final[str] = "classifier_core_unavailable"
STATUS_UNREADABLE: Final[str] = "status_unreadable"

STEERING_SCHEMA = {_HEAD: {"labels": {
    "directive": "The keyword is the user's instruction to the assistant for this turn: they want the named mode applied now.",
    "mention": "The keyword is used as an ordinary word, a name, or in reported/past speech (e.g. 'the CI workflow is failing', 'last time I said ultrathink'); it is not an instruction for this turn.",
    "negated": "The user explicitly says NOT to apply the keyword/mode this turn (e.g. \"don't ultrathink this\", 'no need to orchestrate').",
}}}
# The one typed question the shared endpoint is asked: the label descriptions above are the
# criteria verbatim, so the tie-break's decision boundary is the same one the labels document.
_QUESTIONS: Final[dict[str, object]] = {
    _HEAD: {"type": "choice", "criteria": STEERING_SCHEMA[_HEAD]["labels"]}}


class _Snapshot(Protocol):
    """The part of ``ClientStatus`` this plugin reports; never built here."""

    def as_dict(self) -> dict[str, object]: ...


class _Transport(Protocol):
    """The one classifier-core call this plugin makes, plus its status snapshot."""

    def ask(self, state: str, questions: dict[str, object], *, caller: str,
            timeout: float | None = None) -> dict[str, object] | None: ...

    def status(self) -> _Snapshot: ...


class _SettingsClass(Protocol):
    """``ClassifierSettings`` as this consumer sees it: parsed once, at register."""

    def from_config(self, get_config: Callable[[str, object], object]) -> object: ...


class _ClientModule(Protocol):
    """The surface of classifier-core's ``client.py`` this plugin depends on."""

    ClassifierSettings: _SettingsClass

    def shared_client(self, settings: object) -> _Transport: ...


def _load_client() -> _ClientModule | None:
    """Path-load ``client.py``, preferring the process-wide shared registration.

    A second copy of the module would mean a second breaker, so a module already registered
    under ``_CLASSIFIER_MODULE_KEY`` (which ``client.py`` does itself at import) is reused
    verbatim. An absent or broken sibling is ``None``: the documented degraded path, in which
    every ambiguous hit declines exactly as it does when the tie-break is switched off.
    """
    shared = sys.modules.get(_CLASSIFIER_MODULE_KEY)
    if isinstance(shared, ModuleType):
        return cast(_ClientModule, shared)
    if not _CLASSIFIER_CLIENT_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location(_CLASSIFIER_MODULE_KEY,
                                                  _CLASSIFIER_CLIENT_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE execution, in the import system's own order: a frozen dataclass
    # resolves its string annotations through the module entry in ``sys.modules``, and the
    # shared-state registration inside ``client.py`` depends on that entry existing too.
    sys.modules[_CLASSIFIER_MODULE_KEY] = module
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:  # narrow: degraded
        sys.modules.pop(_CLASSIFIER_MODULE_KEY, None)  # never share a half-built module
        logger.warning("keyword-steering: classifier-core failed to load (%s: %s)",
                       type(exc).__name__, exc, exc_info=True)
        return None
    return cast(_ClientModule, module)


def _classifier_config() -> Callable[[str, object], object]:
    """classifier-core's own settings reader; this plugin has no transport settings."""
    from hermes_cli.config import cfg_get, load_config_readonly

    config = load_config_readonly()

    def reader(key: str, default: object) -> object:
        return cfg_get(config, "plugins", "entries", _CLASSIFIER_PLUGIN, "settings",
                       key, default=default)

    return reader


def _shared_client() -> _Transport | None:
    """The shared client, or ``None``: with no transport every ambiguous hit is declined."""
    module = _load_client()
    if module is None:
        logger.warning("keyword-steering: sibling plugin %s is unavailable (%s); every "
                       "ambiguous hit is declined", _CLASSIFIER_PLUGIN, _CLASSIFIER_CLIENT_PATH)
        return None
    try:
        return module.shared_client(module.ClassifierSettings.from_config(_classifier_config()))
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError,
            ValueError) as exc:  # narrow: degraded, never fatal
        logger.warning("keyword-steering: classifier-core settings are unusable (%s: %s); "
                       "every ambiguous hit is declined", type(exc).__name__, exc, exc_info=True)
        return None


class Tiebreak:
    """The optional semantic seam, judged by the shared classifier transport.

    Registration reads two things only: whether the seam is enabled, and its own wait budget.
    Everything about the service -- endpoint, model, key, breaker -- is classifier-core's.
    """

    def __init__(self, ctx) -> None:
        cfg = ctx.get_config("classifier")
        if not isinstance(cfg, dict):
            cfg = {}
        enabled = cfg.get("enabled", False)
        if not isinstance(enabled, bool):
            raise TypeError("keyword-steering: classifier.enabled must be a boolean")
        self.enabled = enabled
        self._timeout = float(cfg.get("timeout", DEFAULT_TIMEOUT))
        self._client: _Transport | None = _shared_client() if enabled else None
        self._last_error: str | None = None

    def judge(self, trigger: str, prose: str) -> str | None:
        """``directive``/``mention``/``negated`` for one ambiguous hit, or ``None``.

        ``None`` means the caller declines the hit: disabled, no transport, nothing safe to
        send (redaction refused), no answer, or an answer that is not one of the three labels.
        The judge never raises -- an unjudgeable hit is a declined hit, never a failed turn.
        """
        try:
            if not self.enabled or self._client is None:
                return None
            redacted = _redact(prose)
            if redacted is None:
                self._last_error = "redaction unavailable"
                return None
            envelope = self._client.ask(f"Keyword: {trigger}\nMessage: {redacted}",
                                        dict(_QUESTIONS), caller=_CALLER,
                                        timeout=self._timeout)
            label = _label(envelope)
            if label is None:
                self._last_error = "unusable classifier body"
                return None
            self._last_error = None
            return label
        except Exception as exc:  # noqa: BLE001 - boundary: the judge's contract is to never
                                  # raise, and a transport or host failure has no shape this
                                  # plugin may depend on; the decision is already "decline".
            self._last_error = type(exc).__name__
            return None

    def status(self) -> dict[str, object]:
        """This plugin's tie-break posture plus the shared breaker it now depends on.

        Reported, not inferred: the old private outage timer is gone because the outage is not
        this plugin's to own -- a status that disagreed with the service every other consumer
        talks to would send an operator chasing a dead endpoint that is not dead.
        """
        if not self.enabled:
            return {"enabled": False, "transport": TRANSPORT_DISABLED}
        if self._client is None:
            return {"enabled": True, "transport": CLIENT_UNAVAILABLE,
                    "last_error": self._last_error}
        return {"enabled": True, "transport": TRANSPORT_SHARED,
                "last_error": self._last_error, **self._breaker(self._client)}

    def _breaker(self, client: _Transport) -> dict[str, object]:
        """The shared client's breaker fields and this caller's counters.

        A diagnostic surface: an unreadable report is named rather than raised, because
        ``steering_status`` must still answer when the transport's own report cannot be read.
        """
        try:
            report = client.status().as_dict()
        except Exception as exc:  # boundary: the status tool must never raise,
                                 # and the failure shape is the shared client's, not ours.
            logger.warning("keyword-steering: classifier status is unreadable (%s: %s)",
                           type(exc).__name__, exc, exc_info=True)
            return {"breaker": STATUS_UNREADABLE}
        callers = report.get("callers")
        counters = callers.get(_CALLER) if isinstance(callers, dict) else None
        return {
            "breaker": report.get("breaker"),
            "open_until": report.get("open_until"),
            "consecutive_failures": report.get("consecutive_failures"),
            "service_error": report.get("last_error"),
            "counters": counters,
        }


def _redact(prose: str) -> str | None:
    """The message safe to send, or ``None`` when nothing is.

    Redaction runs before egress and an unavailable redactor abstains entirely: a keyword hit
    the plugin cannot see safely is not worth leaking a secret to judge.
    """
    try:
        from agent.redact import REDACTION_UNAVAILABLE, redact_for_egress

        redacted = redact_for_egress(prose)
    except Exception:  # noqa: BLE001 - boundary: the host's redaction module is the authority
                       # on whether text may leave the process, and its failure modes are its
                       # own; refusing egress is the only safe answer this plugin may invent.
        return None
    if not isinstance(redacted, str) or REDACTION_UNAVAILABLE in redacted:
        return None
    return redacted


def _label(envelope: object) -> str | None:
    """The judged label out of an answer envelope, or ``None`` for any unusable reply.

    Parsed once, at this boundary: an envelope without answers, a head that was not answered,
    or a choice outside the three labels are all the same thing here -- no answer. The
    returned string is one of ``LABELS``, so every caller may compare it directly.
    """
    answers = envelope.get("answers") if isinstance(envelope, dict) else None
    answer = answers.get(_HEAD) if isinstance(answers, dict) else None
    choice = answer.get("choice") if isinstance(answer, dict) else None
    return choice if isinstance(choice, str) and choice in LABELS else None
