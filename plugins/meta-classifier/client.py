"""The classifier transport is classifier-core's; this is the plugin's adapter over it.

meta-classifier owns no HTTP, no endpoint, no served model and no key environment variable:
those settings live on the classifier-core entry, and the process-wide client defined there
carries the single breaker every consumer shares, so a dead service is discovered once
instead of once per plugin. What stays here is only this plugin's call surface, unchanged in
shape:

- ``classify(text, schema, timeout)`` -> ``ClassifierClient.classify``: the same
  label-schema-to-typed-questions translation the private transport performed, ported
  verbatim into classifier-core, so a head that abstains still yields no key.
- ``ask(state, questions, timeout)`` -> ``ClassifierClient.ask``: the raw answer envelope,
  probabilities included (the shield compares P(B) with its own threshold and never trusts
  the returned choice).
- ``status()`` -> classifier-core's breaker snapshot, tagged with this plugin's caller name
  so declines are attributed here and not to some other consumer.

An absent or unusable classifier-core is the documented degraded path, never an error: every
call returns ``None``, which is exactly what a dead endpoint returned, so each caller keeps
its own posture -- fail-open for the risk gate, addons and signals; fail-closed for the
shield's downgrade lines.

Stdlib logging, not structlog: the Hermes host ships no structlog (it is not an installed
dependency) and owns plugin logging; see plugins-doctor/checks.py.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol, cast

logger = logging.getLogger(__name__)

CLASSIFIER_CORE_PLUGIN: Final[str] = "classifier-core"
CLIENT_MODULE_KEY: Final[str] = "hermes_plugins_classifier_core"
# Distinct per consumer, so classifier_status attributes a decline to the right plugin.
CALLER: Final[str] = "meta-classifier"
TRANSPORT_SHARED: Final[str] = "classifier-core"
CLASSIFIER_CORE_UNAVAILABLE: Final[str] = "classifier_core_unavailable"
DEFAULT_API_KEY_ENV: Final[str] = "CLASSIFIER_API_KEY"
DEFAULT_TIMEOUT: Final[float] = 2.5
_CLIENT_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / CLASSIFIER_CORE_PLUGIN / "client.py")


class _Snapshot(Protocol):
    """The part of ``ClientStatus`` this plugin publishes; never built here."""

    def as_dict(self) -> dict[str, object]: ...


class _Transport(Protocol):
    """The two classifier-core calls this plugin makes, plus its status snapshot."""

    def classify(self, text: str, schema: Mapping[str, object], *, caller: str,
                 timeout: float | None = None) -> dict[str, object] | None: ...

    def ask(self, state: str, questions: dict[str, object], *, caller: str,
            timeout: float | None = None) -> dict[str, object] | None: ...

    def status(self) -> _Snapshot: ...


class _Settings(Protocol):
    """``ClassifierSettings`` as the tool gate sees it: the key's variable name only."""

    api_key_env: str


class _SettingsClass(Protocol):
    """``ClassifierSettings`` as this consumer sees it: parsed once, at register."""

    def from_config(self, get_config: Callable[[str, object], object]) -> _Settings: ...


class _ClientModule(Protocol):
    """The surface of classifier-core's ``client.py`` this plugin depends on."""

    ClassifierSettings: _SettingsClass

    def shared_client(self, settings: _Settings) -> _Transport: ...


def _load_client() -> _ClientModule | None:
    """Path-load ``client.py``, preferring the process-wide shared registration.

    A second copy of the module would mean a second breaker, so a module already registered
    under ``CLIENT_MODULE_KEY`` (which ``client.py`` does itself at import) is reused
    verbatim. An absent or broken sibling is ``None``: the same no-answer path as a dead
    endpoint, never an exception at register.
    """
    shared = sys.modules.get(CLIENT_MODULE_KEY)
    if isinstance(shared, ModuleType):
        return cast(_ClientModule, shared)
    if not _CLIENT_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location(CLIENT_MODULE_KEY, _CLIENT_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE execution, in the import system's own order: a frozen dataclass
    # resolves its string annotations through the module entry in ``sys.modules``, and the
    # shared-state registration inside ``client.py`` depends on that entry existing too.
    sys.modules[CLIENT_MODULE_KEY] = module
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:  # narrow: degraded
        sys.modules.pop(CLIENT_MODULE_KEY, None)  # never share a half-built module
        logger.warning("meta-classifier: classifier-core failed to load (%s: %s)",
                       type(exc).__name__, exc, exc_info=True)
        return None
    return cast(_ClientModule, module)


def _classifier_config() -> Callable[[str, object], object]:
    """classifier-core's own settings reader; this plugin has no transport settings."""
    from hermes_cli.config import cfg_get, load_config_readonly

    config = load_config_readonly()

    def reader(key: str, default: object) -> object:
        return cfg_get(config, "plugins", "entries", CLASSIFIER_CORE_PLUGIN, "settings",
                       key, default=default)

    return reader


def _resolve() -> tuple[_Transport | None, str]:
    """The process-wide client, or ``None`` with the single reason it is unusable.

    Resolved once, at register: a per-call resolve would re-read the operator's settings on
    every request, and a per-plugin client would fork the breaker the whole design rests on.
    """
    module = _load_client()
    if module is None:
        logger.warning("meta-classifier: sibling plugin %s is unavailable (%s); every "
                       "classification returns no answer", CLASSIFIER_CORE_PLUGIN, _CLIENT_PATH)
        return None, CLASSIFIER_CORE_UNAVAILABLE
    try:
        client = module.shared_client(module.ClassifierSettings.from_config(_classifier_config()))
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError,
            ValueError) as exc:  # narrow: degraded, never fatal
        logger.warning("meta-classifier: classifier-core settings are unusable (%s: %s); "
                       "every classification returns no answer", type(exc).__name__, exc,
                       exc_info=True)
        return None, CLASSIFIER_CORE_UNAVAILABLE
    return client, ""


def has_service_key() -> bool:
    """Whether the service has a Bearer key to answer with.

    The gate is this plugin's -- an agent-facing tool that can only ever fail must not be
    advertised to the model -- but the variable's NAME is a classifier-core setting, so it is
    read from there and never copied into this plugin's config schema. The value is read
    from the environment here and now, and never stored, logged or returned.
    """
    named = _classifier_config()("api_key_env", DEFAULT_API_KEY_ENV)
    name = named.strip() if isinstance(named, str) and named.strip() else DEFAULT_API_KEY_ENV
    return bool(os.environ.get(name, "").strip())


class Classifier:
    """This plugin's classifier call surface, forwarded to the shared transport.

    Constructed once, at register. Every method returns ``None`` when there is nothing to
    ask with -- no transport, no key, a dead or unusable service, a text outside the service
    limits -- which is the answer each caller already had for a dead endpoint, so no decision
    logic lives or dies here.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = float(timeout)
        self._client, self.problem = _resolve()

    @property
    def available(self) -> bool:
        """Whether a transport was resolved; the plugin's own register-time flag."""
        return self._client is not None

    def classify(self, text: str, schema: Mapping[str, object],
                 timeout: float | None = None) -> dict[str, object] | None:
        """Score a label schema in one request and return its labels, or ``None``."""
        if self._client is None:
            return None
        return self._client.classify(text, schema, caller=CALLER,
                                     timeout=self._deadline(timeout))

    def ask(self, state: str, questions: dict[str, object],
            timeout: float | None = None) -> dict[str, object] | None:
        """Send typed questions and return the raw answer envelope, or ``None``."""
        if self._client is None:
            return None
        return self._client.ask(state, questions, caller=CALLER,
                                timeout=self._deadline(timeout))

    def status(self) -> dict[str, object]:
        """This plugin's view of the transport: the shared breaker, attributed to this caller.

        A diagnostic surface, so an unavailable transport is reported by name rather than
        pretending to be a transport.
        """
        if self._client is None:
            return {"caller": CALLER, "transport": CLASSIFIER_CORE_UNAVAILABLE,
                    "problem": self.problem}
        snapshot = dict(self._client.status().as_dict())
        snapshot["transport"] = TRANSPORT_SHARED
        snapshot["caller"] = CALLER
        return snapshot

    def _deadline(self, timeout: float | None) -> float:
        """This call's wait budget: the caller's explicit timeout, else the plugin's floor.

        The budget's other half -- the text's measured inference cost and the ceiling -- is
        classifier-core's ``ClassifierSettings.budget``, which is where the engine's cost was
        actually measured; an explicit timeout overrides it there too.
        """
        return self._timeout if timeout is None else float(timeout)
