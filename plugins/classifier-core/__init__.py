"""classifier-core: the shared classifier transport's own status surface.

Registers no hooks and makes no network call: the plugin exists so that every consumer of
the shared ``/v1/systemone`` client has one place to ask what that client is currently
doing — breaker state, consecutive failures, totals and per-caller decline counters —
instead of each plugin inventing its own. ``probe: true`` (or ``--probe``) adds one fresh
``GET /health`` result, because a breaker snapshot alone cannot tell an operator whether
the service is down or merely idle.

Deliberately no logger here: structlog is not a host dependency, and a degraded path that
would want one is instead returned as an explicit ``{"available": false, "error": ...}``
payload by the tool and the command — fail-open stays visible, never silent.

``client.py`` is loaded by file path, the repository's cross-plugin reuse idiom, and that
module registers itself once in ``sys.modules["hermes_plugins_classifier_core"]`` so every
loader in the process shares ONE module object and therefore ONE client registry and ONE
breaker per endpoint. An absent or broken ``client.py`` degrades to the honest payload
above; it never crashes at register.

Settings are read once at register through ``ClassifierSettings.from_config``; a value that
is present but the wrong type raises ``TypeError`` there and one that is the right type but
unusable raises ``ValueError``, so the host reports the plugin as refused rather than running
it on a silently reinterpreted configuration.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol, cast

CLIENT_MODULE_KEY: Final[str] = "hermes_plugins_classifier_core"
TOOLSET: Final[str] = "classifier-core"
TOOL_NAME: Final[str] = "classifier_status"
COMMAND_NAME: Final[str] = "classifier-status"
CLIENT_UNAVAILABLE: Final[str] = "client_unavailable"


class _Value(Protocol):
    """A status or health snapshot: the frozen dataclasses in ``client.py``."""

    def as_dict(self) -> dict[str, object]: ...


class _StatusReporter(Protocol):
    """The part of ``ClassifierClient`` this plugin calls; injected, never constructed here."""

    def status(self) -> _Value: ...

    def health(self) -> _Value: ...


class _SettingsClass(Protocol):
    """``ClassifierSettings`` as the host sees it: parse settings once, at register."""

    def from_config(self, get_config: Callable[[str, object], object]) -> object: ...


class _ClientModule(Protocol):
    """The surface of the path-loaded ``client.py`` this plugin depends on."""

    ClassifierSettings: _SettingsClass

    def shared_client(self, settings: object) -> _StatusReporter: ...


@dataclass(frozen=True, slots=True)
class _ClientAccess:
    """The process-shared client module, or the single reason it is unusable."""

    module: _ClientModule | None
    error: str | None

    @property
    def available(self) -> bool:
        return self.module is not None


def _load_client() -> _ClientAccess:
    """Load ``client.py`` by path, preferring the process-wide shared registration.

    A second copy of the module would mean a second breaker, so a module already registered
    under ``CLIENT_MODULE_KEY`` (which ``client.py`` does itself at import) is reused
    verbatim; the by-path load registers it the same way for the next loader.
    """
    shared = sys.modules.get(CLIENT_MODULE_KEY)
    if isinstance(shared, ModuleType):
        return _ClientAccess(module=cast(_ClientModule, shared), error=None)
    path = Path(__file__).resolve().with_name("client.py")
    if not path.is_file():
        return _ClientAccess(module=None, error=f"{CLIENT_UNAVAILABLE}: client.py is absent")
    spec = importlib.util.spec_from_file_location(CLIENT_MODULE_KEY, path)
    if spec is None or spec.loader is None:
        return _ClientAccess(module=None, error=f"{CLIENT_UNAVAILABLE}: client.py is not loadable")
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE execution, in the import system's own order: a frozen dataclass
    # resolves its string annotations through the module entry in ``sys.modules``, and the
    # shared-state registration in ``client.py`` depends on that entry existing too.
    sys.modules[CLIENT_MODULE_KEY] = module
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:  # narrow: degraded, never fatal
        sys.modules.pop(CLIENT_MODULE_KEY, None)  # never leave a half-built module shared
        return _ClientAccess(module=None, error=f"{CLIENT_UNAVAILABLE}: {type(exc).__name__}: {exc}")
    return _ClientAccess(module=cast(_ClientModule, module), error=None)


def _exit_code(payload: Mapping[str, object]) -> int:
    """0 only when the breaker is closed and no caller was ever declined as not_configured.

    Decided from the exact payload that gets printed, so the exit code cannot disagree with
    what the operator just read.
    """
    if payload.get("breaker") != "closed":
        return 1
    callers = payload.get("callers")
    if not isinstance(callers, Mapping):
        return 1
    for counters in callers.values():
        if not isinstance(counters, Mapping):
            return 1
        declined = counters.get("declined")
        if not isinstance(declined, Mapping):
            continue
        count = declined.get("not_configured", 0)
        if isinstance(count, bool) or not isinstance(count, int) or count > 0:
            return 1
    return 0


def register(ctx) -> None:
    """Register ``classifier_status`` and ``classifier-status``; no network at register."""
    access = _load_client()
    client: _StatusReporter | None = None
    problem: str | None = access.error
    if access.module is not None:
        settings = access.module.ClassifierSettings.from_config(ctx.get_config)
        client = access.module.shared_client(settings)

    def report(probe: bool) -> dict[str, object]:
        """The status payload, plus one live ``/health`` probe when asked for."""
        if client is None:
            return {"available": False, "error": problem or CLIENT_UNAVAILABLE}
        payload: dict[str, object] = dict(client.status().as_dict())
        if probe:
            payload["health"] = dict(client.health().as_dict())
        return payload

    def classifier_status(args: object, **_kw: object) -> str:
        probe = args.get("probe") if isinstance(args, Mapping) else False
        return json.dumps(report(bool(probe)), sort_keys=True)

    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema={
            "name": TOOL_NAME,
            "description": (
                "Status of the shared classifier client for the configured endpoint: breaker "
                "state, seconds until it may probe again, consecutive service failures, request "
                "totals and per-caller decline counters (not_configured, breaker_open, "
                "text_limits, unusable_body, service_error, cancelled). Read-only; pass "
                "probe=true to also GET /health once. A closed breaker with zero not_configured "
                "declines means the transport is healthy, not that any verdict was produced."),
            "parameters": {
                "type": "object",
                "properties": {
                    "probe": {
                        "type": "boolean",
                        "description": "Also perform one GET /health against the endpoint.",
                    },
                },
                "required": [],
            },
        },
        handler=classifier_status,
        description="Shared classifier breaker, counters and totals (optionally with a health probe).",
    )

    def status_setup(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--probe", action="store_true",
            help="also perform one GET /health against the configured endpoint")

    def status_handler(args: object) -> int:
        payload = report(bool(getattr(args, "probe", False)))
        print(json.dumps(payload, indent=2, sort_keys=True))
        return _exit_code(payload)

    ctx.register_cli_command(
        name=COMMAND_NAME,
        help="Print the shared classifier client's breaker, counters and totals.",
        setup_fn=status_setup,
        handler_fn=status_handler,
        description=(
            "Exits 0 when the breaker is closed and no caller was declined as not_configured; "
            "exits 1 otherwise, so the same output can gate an operator check."),
    )
