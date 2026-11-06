"""Isolated native Hermes loader/dispatcher; no test context reimplements its SDK."""
import importlib.util
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins"
CLASSIFIER_MODULE_KEY = "hermes_plugins_classifier_core"
Reply = Callable[..., dict[str, object] | None]


class ClassifierStub:
    """One fake shared client: records every request, answers with the test's reply.

    Consumers path-load ``plugins/classifier-core/client.py`` and reuse the module
    registered under ``CLASSIFIER_MODULE_KEY``, so a test fakes the classifier
    transport by installing that module — never by patching ``urllib`` (the migrated
    consumers no longer import it). ``calls`` holds one dict per request, tagged with
    ``method`` (``ask`` carries ``questions``, ``classify`` carries ``schema``) plus the
    ``state``/``caller``/``timeout`` the consumer passed.
    """

    def __init__(self, reply: Reply | None = None,
                 labels: Reply | None = None) -> None:
        self._reply = reply
        self._labels = labels
        self.calls: list[dict[str, object]] = []

    def _answer(self, method: str, identity: str, field: str, value: object,
                caller: str, timeout: float | None,
                reply: Reply | None) -> dict[str, object] | None:
        self.calls.append({"method": method, "state": identity, field: value,
                           "caller": caller, "timeout": timeout})
        if reply is None:
            return None  # the declined-request path every consumer must fail open on
        return reply(identity, value, caller=caller, timeout=timeout)

    def ask(self, state: str, questions: dict[str, object], *, caller: str,
            timeout: float | None = None) -> dict[str, object] | None:
        return self._answer("ask", state, "questions", questions, caller, timeout,
                            self._reply)

    def classify(self, text: str, schema: dict[str, object], *, caller: str,
                 timeout: float | None = None) -> dict[str, object] | None:
        return self._answer("classify", text, "schema", schema, caller, timeout,
                            self._labels)


class _StubSettings:
    """Stands in for ``ClassifierSettings``; a consumer only ever calls ``from_config``."""

    @classmethod
    def from_config(cls, get_config: Callable[[str, object], object]) -> "_StubSettings":
        return cls()


@dataclass(frozen=True, slots=True)
class ClassifierStubInstaller:
    """The whole surface the ``classifier_stub`` fixture hands out: ``install``.

    Tests always write ``classifier_stub.install(...)``; the monkeypatch travels with
    it so the registration is undone by pytest along with every other test effect.
    See ``ClassifierStub`` for what a stub answers and records.
    """

    patch: pytest.MonkeyPatch

    def install(self, reply: Reply | None = None, *, absent: bool = False,
                labels: Reply | None = None) -> ClassifierStub:
        """Answer ``ask`` via ``reply``, ``classify`` via ``labels``; with neither, decline.

        ``absent=True`` removes the registered module and blocks the by-path load of
        ``classifier-core/client.py`` only — the documented degraded path, not an error.
        """
        stub = ClassifierStub(reply, labels)
        if absent:
            self.patch.delitem(sys.modules, CLASSIFIER_MODULE_KEY, raising=False)
            real_spec = importlib.util.spec_from_file_location

            def no_client_spec(name: str, location: str | Path, *_args: object,
                               **_kwargs: object) -> ModuleSpec | None:
                # ONLY this sibling is absent: the host's own plugin loader still
                # imports plugin packages by path, so a blanket patch would be wrong.
                if str(location).replace("\\", "/").endswith("classifier-core/client.py"):
                    return None
                return real_spec(name, location)

            self.patch.setattr("importlib.util.spec_from_file_location", no_client_spec)
            return stub
        module = ModuleType(CLASSIFIER_MODULE_KEY)
        # A module's namespace is a dict; a real module object is what the consumers'
        # isinstance check accepts, so the two names go into its namespace directly.
        module.__dict__["ClassifierSettings"] = _StubSettings
        module.__dict__["shared_client"] = lambda settings: stub
        self.patch.setitem(sys.modules, CLASSIFIER_MODULE_KEY, module)
        return stub


@pytest.fixture
def classifier_stub(monkeypatch: pytest.MonkeyPatch) -> ClassifierStubInstaller:
    """Give a test the shared classifier-core stand-in (see ClassifierStubInstaller)."""
    return ClassifierStubInstaller(monkeypatch)


@pytest.fixture(autouse=True)
def isolated_hermes(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    for sub in ("cron", "sessions", "logs", "memories"):
        (home / sub).mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("CLASSIFIER_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_MANAGED_CONFIG", raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    config = {"terminal": {"backend": "local"}, "agent": {"verify_on_stop": True},
              "plugins": {"enabled": [], "entries": {}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))

    def no_network(*_args, **_kwargs):
        raise AssertionError("Integration tests must isolate external network/model boundaries")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginManager
    from tools import file_tools  # Register native consumers before plugin overrides.
    from tools.registry import registry

    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    # Registry native tests sometimes replace entries deliberately. Restore their
    # exact identity, not a reconstructed approximation of native tool metadata.
    original_tools = dict(registry._tools)
    original_scoped = {scope: dict(entries) for scope, entries in registry._scoped_tools.items()}
    from tools.terminal_tool import _session_cwd
    original_cwds = dict(_session_cwd)
    yield SimpleNamespace(home=home, config=config, manager=manager)
    manager.unload()
    with registry._lock:
        registry._tools.clear()
        registry._tools.update(original_tools)
        registry._scoped_tools.clear()
        registry._scoped_tools.update(original_scoped)
    _session_cwd.clear()
    _session_cwd.update(original_cwds)


@pytest.fixture
def load_plugin(isolated_hermes):
    from hermes_cli import middleware, plugins
    from hermes_cli.plugins import PluginContext
    from hermes_cli.plugins_discovery import scan_directory

    runtime = isolated_hermes
    manifests = {manifest.name: manifest for manifest in scan_directory(PLUGIN_ROOT, "user")}

    def load(name, settings=None):
        if name in runtime.manager._plugins:
            runtime.manager.unload(name)
        runtime.config["plugins"]["entries"][name] = {
            "enabled": True, "allow_tool_override": True,
            "settings": settings or {},
        }
        runtime.config["plugins"]["enabled"].append(name)
        (runtime.home / "config.yaml").write_text(yaml.safe_dump(runtime.config))
        manifest = manifests[name]
        runtime.manager._load_plugin(manifest)
        loaded = runtime.manager._plugins[name]
        assert loaded.enabled, f"Native plugin loader refused {name}: {loaded.error}"
        context = PluginContext(manifest, runtime.manager)

        def invoke_middleware(kind, **payload):
            if kind == "tool_execution":
                return middleware.run_tool_execution_middleware(**payload)
            if kind == "tool_request":
                return middleware.apply_tool_request_middleware(**payload)
            if kind == "llm_request":
                return middleware.apply_llm_request_middleware(**payload)
            raise ValueError(f"Unsupported integration middleware {kind}")

        return SimpleNamespace(
            module=loaded.module, manager=runtime.manager, context=context,
            hook=runtime.manager.invoke_hook, tool=context.dispatch_tool,
            middleware=invoke_middleware,
            command=lambda name, raw="": plugins.get_plugin_command_handler(name)(raw),
            home=runtime.home,
        )

    return load
