"""Isolated native Hermes loader/dispatcher; no test context reimplements its SDK."""
from pathlib import Path
from types import SimpleNamespace
import socket

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins"


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
    from tools.registry import registry
    from tools import file_tools  # Register native consumers before plugin overrides.

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
    from hermes_cli.plugins import PluginContext
    from hermes_cli.plugins_discovery import scan_directory
    from hermes_cli import middleware, plugins

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
