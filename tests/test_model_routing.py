"""Contract coverage for the model-routing plugin.

Config validation, the six-key resolver receipts, the seam-optional
registration, route_status determinism/audit purity, and route_apply
materialization through the native config writers (temp HERMES_HOME via the
autouse isolated fixture).
"""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

PLUGIN_ID = "model-routing"
PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins"
RECEIPT_KEYS = {"resolved", "role", "model", "provider", "source", "reason"}

LILITH = {"base_url": "http://127.0.0.1:9999/v1"}

APPLY_SETTINGS = {
    "roles": {
        "executor": {"model": "glm-5.3", "provider": "zai"},
        "observer": {"model": "qwen3.8-flash-next"},
        "reviewer": {"model": "mimo-v2.6-pro", "provider": "lilith"},
    },
    "delegation_role": "observer",
    "fallback": "inherit",
}


def _failed_load(isolated_hermes, settings):
    """Load model-routing natively with ``settings``; return the LoadedPlugin
    (which carries .error/.enabled instead of raising)."""
    from hermes_cli.plugins_discovery import scan_directory
    manifests = {m.name: m for m in scan_directory(PLUGIN_ROOT, "user")}
    cfg = isolated_hermes.config
    cfg["plugins"]["entries"][PLUGIN_ID] = {
        "enabled": True, "allow_tool_override": True, "settings": settings,
    }
    if PLUGIN_ID not in cfg["plugins"]["enabled"]:
        cfg["plugins"]["enabled"].append(PLUGIN_ID)
    (isolated_hermes.home / "config.yaml").write_text(yaml.safe_dump(cfg))
    isolated_hermes.manager._load_plugin(manifests[PLUGIN_ID])
    return isolated_hermes.manager._plugins[PLUGIN_ID]


class _FakeCtx:
    def __init__(self, with_seam=True):
        self.tools = {}
        self.resolver = None
        self.plugin_id = PLUGIN_ID
        if with_seam:
            self.register_role_resolver = self._register_role_resolver

    def register_tool(self, *, name, toolset, schema, handler, description="", **_kw):
        self.tools[name] = handler

    def _register_role_resolver(self, fn):
        self.resolver = fn


def _unit(monkeypatch, settings, *, with_seam=True, providers=("zai", "lilith")):
    spec = importlib.util.spec_from_file_location(
        "model_routing_unit", PLUGIN_ROOT / PLUGIN_ID / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_entry_settings", lambda ctx: dict(settings))
    monkeypatch.setattr(
        mod, "_known_providers", lambda: None if providers is None else set(providers))
    ctx = _FakeCtx(with_seam=with_seam)
    mod.register(ctx)
    return mod, ctx


def _status(ctx):
    return json.loads(ctx.tools["route_status"]())


# --- fail-loud registration (native loader) ---------------------------------

INVALID_CASES = [
    ({"roles": {"executor": {}}}, "model"),
    ({"roles": {"executor": {"model": ""}}}, "model"),
    ({"roles": {"executor": {"model": "m", "tier": "high"}}}, "tier"),
    ({"roles": {"Executor": {"model": "m"}}}, "Executor"),
    ({"roles": {}, "router": "static"}, "router"),
    ({"roles": {}, "fallback": "smart"}, "fallback"),
    ({"roles": {"executor": {"model": "m", "provider": "definitely-not-a-provider"}}},
     "definitely-not-a-provider"),
    ({"roles": {}, "delegation_role": "observer"}, "delegation_role"),
]


@pytest.mark.parametrize("settings,offending", INVALID_CASES)
def test_invalid_settings_fail_registration_naming_the_key(
        isolated_hermes, settings, offending):
    loaded = _failed_load(isolated_hermes, settings)
    assert not loaded.enabled
    assert offending in (loaded.error or "")


def test_valid_config_loads_and_reports(isolated_hermes, load_plugin):
    isolated_hermes.config["providers"] = {"lilith": LILITH}
    harness = load_plugin(PLUGIN_ID, settings=APPLY_SETTINGS)
    status = _json(harness, "route_status")
    assert status["provider_validation"] == "checked"
    assert status["host_seam"] in ("registered", "unavailable")
    assert status["config"]["roles"]["executor"]["provider"] == "zai"
    assert status["effective"]["observer"]["provider"] == "parent"


def _json(harness, tool, args=None):
    return json.loads(harness.tool(tool, args or {}))


# --- resolver receipts (unit ctx; host seam presence is irrelevant) --------

def _resolver(monkeypatch, settings, **kwargs):
    mod, ctx = _unit(monkeypatch, settings, **kwargs)
    return mod, ctx, ctx.resolver


def test_route_with_explicit_provider(monkeypatch):
    _mod, ctx, resolve = _resolver(monkeypatch, APPLY_SETTINGS)
    receipt = resolve("executor", session_id="s1", parent_model="parent-m",
                      parent_provider="parent-prov")
    assert receipt == {
        "resolved": True, "role": "executor", "model": "glm-5.3",
        "provider": "zai", "source": "route",
        "reason": "role 'executor' routed to glm-5.3",
    }
    assert set(receipt) == RECEIPT_KEYS


def test_route_with_omitted_provider_uses_parent_provider(monkeypatch):
    _mod, ctx, resolve = _resolver(monkeypatch, APPLY_SETTINGS)
    receipt = resolve("observer", session_id="s1", parent_model="parent-m",
                      parent_provider="parent-prov")
    assert receipt["source"] == "route"
    assert receipt["provider"] == "parent-prov"


def test_route_parentless_uses_config_main_provider(monkeypatch):
    mod, ctx, resolve = _resolver(monkeypatch, APPLY_SETTINGS)
    monkeypatch.setattr(mod, "_main_provider", lambda: "main-prov")
    receipt = resolve("observer", session_id="s1")
    assert receipt["provider"] == "main-prov"


def test_unknown_role_inherits_parent_model(monkeypatch):
    _mod, ctx, resolve = _resolver(monkeypatch, APPLY_SETTINGS)
    receipt = resolve("ghost", session_id="s1", parent_model="parent-m",
                      parent_provider="parent-prov")
    assert receipt["resolved"] is False
    assert receipt["source"] == "inherit"
    assert receipt["model"] == "parent-m"
    assert "ghost" in receipt["reason"]


def test_unknown_role_refused_under_refuse_fallback(monkeypatch):
    settings = dict(APPLY_SETTINGS, fallback="refuse")
    _mod, ctx, resolve = _resolver(monkeypatch, settings)
    receipt = resolve("ghost", session_id="s1", parent_model="parent-m",
                      parent_provider="parent-prov")
    assert receipt["resolved"] is False
    assert receipt["source"] == "refused"
    assert "ghost" in receipt["reason"]
    assert set(receipt) == RECEIPT_KEYS


# --- seam handling ----------------------------------------------------------

def test_resolver_registered_when_host_seam_exists(monkeypatch):
    _mod, ctx, resolve = _resolver(monkeypatch, APPLY_SETTINGS)
    assert callable(resolve)
    assert _status(ctx)["host_seam"] == "registered"


def test_plugin_loads_cleanly_without_host_seam(monkeypatch):
    _mod, ctx = _unit(monkeypatch, APPLY_SETTINGS, with_seam=False)
    status = _status(ctx)
    assert status["host_seam"] == "unavailable"
    assert "route_status" in ctx.tools and "route_apply" in ctx.tools


def test_provider_validation_skipped_without_host_api(monkeypatch):
    settings = {"roles": {"executor": {"model": "m", "provider": "whatever-we-cannot-check"}}}
    mod, ctx = _unit(monkeypatch, settings, providers=None)
    assert _status(ctx)["provider_validation"] == "skipped"
    assert callable(ctx.resolver)  # loaded despite the unverifiable provider


# --- route_status determinism and audit purity ------------------------------

def test_route_status_config_and_effective_are_byte_identical(monkeypatch):
    _mod, ctx = _unit(monkeypatch, APPLY_SETTINGS)
    first = _status(ctx)
    second = _status(ctx)
    assert json.dumps(first["config"], sort_keys=True) == \
        json.dumps(second["config"], sort_keys=True)
    assert json.dumps(first["effective"], sort_keys=True) == \
        json.dumps(second["effective"], sort_keys=True)


def test_route_status_config_byte_identical_native(isolated_hermes, load_plugin):
    isolated_hermes.config["providers"] = {"lilith": LILITH}
    harness = load_plugin(PLUGIN_ID, settings=APPLY_SETTINGS)
    first = harness.tool("route_status", {})
    second = harness.tool("route_status", {})
    assert json.loads(first)["config"] == json.loads(second)["config"]
    assert json.loads(first)["effective"] == json.loads(second)["effective"]


def test_audit_grows_by_resolutions_only(monkeypatch):
    _mod, ctx, resolve = _resolver(monkeypatch, APPLY_SETTINGS)
    for _ in range(3):
        ctx.tools["route_apply"]({"apply": False})
    assert len(_status(ctx)["audit"]) == 0
    resolve("executor", session_id="s1", parent_model="m", parent_provider="p")
    resolve("ghost", session_id="s2")
    audit = _status(ctx)["audit"]
    assert len(audit) == 2
    assert set(audit[0]) == {"receipt", "session_id", "time"}
    assert audit[0]["session_id"] == "s1"
    assert audit[1]["receipt"]["source"] == "inherit"


# --- route_apply materialization (native, temp HERMES_HOME) -----------------

EXPECTED_DIFF = {
    "prewalk.target_model": {"old": None, "new": "glm-5.3"},
    "prewalk.target_provider": {"old": None, "new": "zai"},
    "delegation.model": {"old": None, "new": "qwen3.8-flash-next"},
    "auxiliary.curator.model": {"old": None, "new": "mimo-v2.6-pro"},
    "auxiliary.curator.provider": {"old": None, "new": "lilith"},
    "auxiliary.background_review.model": {"old": None, "new": "mimo-v2.6-pro"},
    "auxiliary.background_review.provider": {"old": None, "new": "lilith"},
    "auxiliary.review.model": {"old": None, "new": "mimo-v2.6-pro"},
    "auxiliary.review.provider": {"old": None, "new": "lilith"},
}


@pytest.fixture()
def routing(isolated_hermes, load_plugin):
    isolated_hermes.config["providers"] = {"lilith": LILITH}
    return load_plugin(PLUGIN_ID, settings=APPLY_SETTINGS)


def test_route_apply_dry_run_reports_exact_diff_and_writes_nothing(routing):
    path = routing.home / "config.yaml"
    before = path.read_bytes()
    result = _json(routing, "route_apply")
    assert result["dry_run"] is True
    assert result["applied"] is False
    assert result["diff"] == EXPECTED_DIFF
    assert path.read_bytes() == before


def test_route_apply_writes_only_the_listed_keys(routing):
    path = routing.home / "config.yaml"
    result = _json(routing, "route_apply", {"apply": True})
    assert result["applied"] is True
    assert result["provenance"]["plugin"] == PLUGIN_ID
    on_disk = yaml.safe_load(path.read_text())
    prewalk = on_disk["plugins"]["entries"]["prewalk"]["settings"]
    assert prewalk["target_model"] == "glm-5.3"
    assert prewalk["target_provider"] == "zai"
    assert on_disk["delegation"]["model"] == "qwen3.8-flash-next"
    assert "provider" not in on_disk["delegation"]  # observer omitted provider
    for surface in ("curator", "background_review", "review"):
        assert on_disk["auxiliary"][surface]["model"] == "mimo-v2.6-pro"
        assert on_disk["auxiliary"][surface]["provider"] == "lilith"
    # unrelated keys untouched
    assert on_disk["terminal"]["backend"] == "local"
    assert on_disk["agent"]["verify_on_stop"] is True
    assert on_disk["plugins"]["entries"][PLUGIN_ID]["settings"] == APPLY_SETTINGS


def test_route_apply_is_idempotent_and_drift_free(routing):
    _json(routing, "route_apply", {"apply": True})
    again = _json(routing, "route_apply")
    assert again["dry_run"] is True
    assert again["diff"] == {}


# --- guard -------------------------------------------------------------------

def test_plugin_source_never_selects_by_measurement_or_inference():
    source = (PLUGIN_ROOT / PLUGIN_ID / "__init__.py").read_text()
    for token in ("classifier", "cost", "quota"):
        assert token not in source
    assert "subagent_effort" not in source
