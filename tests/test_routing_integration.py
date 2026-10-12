"""Cross-repo integration: the REAL model-routing plugin drives the REAL host seams.

The real plugin is loaded with real settings through the real host
PluginManager/PluginContext (tests/conftest.py ``load_plugin``). Only external
boundaries are faked:

- ``hermes_cli.runtime_provider.resolve_runtime_provider`` — per-provider fake
  runtimes carrying api_key/base_url/api_mode/capabilities (no network);
- child agent construction/run on the delegate path
  (``_build_child_preserving_parent_tools`` / ``_run_single_child``, the same
  injection points the host tree's tests/tools/test_delegate_model_role.py
  uses);
- OpenAI client construction for the live switch (``_create_openai_client``) —
  the native ``agent_runtime_helpers.switch_model`` pipeline itself is real.

Everything else — resolver registration and validation, delegate model_role
routing, the prewalk arm/ready/replan flow, background_review precedence,
keyword-steering's set_turn_reasoning call — is the shipped host + plugin code.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tools.delegate_tool as dt
from agent.subagent_lifecycle import bind_subagent_parent

DEST_PROVIDER = "zai"        # canonical slug: passes model-routing provider validation
PARENT_PROVIDER = "openai"   # live planner's provider (never validated by name)
PIN_PROVIDER = "minimax"     # canonical slug for the explicit-pin contrast
ROUTED_MODEL = "route-dest-model"
REVIEWER_MODEL = "route-reviewer-model"
PIN_MODEL = "pinned-review-model"
SIX_KEYS = {"resolved", "role", "model", "provider", "source", "reason"}

# Parent's own trust map: the destination map must never leak this across a
# provider/base_url change (#94036/#97292).
PARENT_CAPS = {"openai_native_compaction": True}

ROLES = {
    "observer": {"model": ROUTED_MODEL, "provider": DEST_PROVIDER},
    "executor": {"model": ROUTED_MODEL, "provider": DEST_PROVIDER},
    "reviewer": {"model": REVIEWER_MODEL, "provider": DEST_PROVIDER},
}

# Fake provider ladder: per-provider runtimes with deliberately distinct
# capability maps (including non-str->bool junk the str->bool filter must drop).
_FAKE_RUNTIMES = {
    DEST_PROVIDER: {
        "provider": DEST_PROVIDER, "model": ROUTED_MODEL,
        "base_url": f"https://{DEST_PROVIDER}.example/v1", "api_key": "dest-key",
        "api_mode": "chat_completions", "request_overrides": None,
        "command": None, "args": [],
        "capabilities": {"web_search": True, "shell": False, "junk": "yes", 7: True},
    },
    PARENT_PROVIDER: {
        "provider": PARENT_PROVIDER, "model": "strong-planner",
        "base_url": f"https://{PARENT_PROVIDER}.example/v1", "api_key": "parent-key",
        "api_mode": "chat_completions", "request_overrides": None,
        "command": None, "args": [],
        "capabilities": {"vision": True},
    },
    PIN_PROVIDER: {
        "provider": PIN_PROVIDER, "model": PIN_MODEL,
        "base_url": f"https://{PIN_PROVIDER}.example/v1", "api_key": "pin-key",
        "api_mode": "chat_completions", "request_overrides": None,
        "command": None, "args": [],
        "capabilities": {"file_io": True},
    },
}
DEST_CAPS = {"web_search": True, "shell": False}


@pytest.fixture(autouse=True)
def _clean_spawn_ring():
    dt._SPAWN_ROUTE_RING.clear()
    yield
    dt._SPAWN_ROUTE_RING.clear()


@pytest.fixture
def fake_runtimes(monkeypatch):
    """Fake ONLY the external provider-resolution boundary; records every call."""
    import hermes_cli.runtime_provider as runtime_provider_mod
    calls = []

    def fake(requested=None, target_model=None, **_kw):
        calls.append({"requested": requested, "target_model": target_model})
        runtime = _FAKE_RUNTIMES.get(str(requested or ""))
        if runtime is None:
            raise ValueError(f"provider {requested!r} is not configured")
        return dict(runtime, model=target_model or runtime["model"])

    monkeypatch.setattr(runtime_provider_mod, "resolve_runtime_provider", fake)
    return calls


@pytest.fixture
def live_agent(monkeypatch):
    """Real agent_runtime_helpers.switch_model; ONLY client construction mocked.

    Mirrors tests/test_planning_integration.py.live_agent (itself mirroring the
    host's tests/agent/test_switch_model_reasoning_override.py._make_fake_agent)
    so the whole native switch pipeline runs offline.
    """
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {})
    agent = MagicMock()
    agent.session_id = "s"
    agent.model = "strong-planner"
    agent.provider = PARENT_PROVIDER
    agent.requested_provider = PARENT_PROVIDER
    agent.base_url = "http://ep"
    agent.api_mode = "chat_completions"
    agent.api_key = "k"
    agent.capabilities = dict(PARENT_CAPS)
    agent._client_kwargs = {"api_key": "k", "base_url": "http://ep"}
    agent._use_prompt_caching = False
    agent._use_native_cache_layout = False
    agent.reasoning_config = None
    agent._fallback_activated = False
    agent._fallback_index = 0
    agent._fallback_chain = []
    agent._fallback_model = None
    agent._config_context_length = None
    agent._transport_cache = {}
    agent.context_compressor = None
    agent._cached_system_prompt = None
    agent._anthropic_api_key = ""
    agent._anthropic_base_url = None
    agent._is_anthropic_oauth = False
    agent._anthropic_prompt_cache_policy = MagicMock(return_value=(False, False))
    agent._ensure_lmstudio_runtime_loaded = MagicMock()
    agent._create_openai_client = MagicMock(return_value=MagicMock())
    agent._credential_pool = MagicMock()
    agent._read_reasoning_echo_from_config = MagicMock(return_value=False)
    agent._persist_disabled = False
    agent._parent_session_id = None
    return agent


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Tmp workspace for prewalk_ready path validation."""
    (tmp_path / "widget.py").write_text("VALUE = 1\n")
    monkeypatch.setattr("agent.runtime_cwd.resolve_context_cwd", lambda: tmp_path)
    return tmp_path


def _handoff(files=None):
    return {"files": files or ["widget.py"], "symbols": ["widget"],
            "callers": ["main"], "constraints": ["keep API stable"],
            "non_goals": ["no schema change"],
            "chosen_approach": "guard the crash site",
            "rejected_approaches": ["swallow the error"],
            "falsification_checks": ["pytest tests/test_widget.py fails without the fix"],
            "replan_triggers": ["stack trace points elsewhere"],
            "acceptance_ids": ["c1"], "rule_refs": []}


def _planned_todo(agent):
    from tools.todo_tool import TodoStore, todo_tool
    agent._todo_store = TodoStore()
    todo_tool(todos=[{"id": "1", "content": "plan", "status": "pending"}],
              store=agent._todo_store)


def _delegate_parent():
    """Parent agent for the delegate path (host test_delegate_model_role shape),
    with a real provider/model/capabilities binding the route must NOT inherit."""
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "parent-session"
    parent.model = "parent-model"
    parent.provider = PARENT_PROVIDER
    parent.capabilities = dict(PARENT_CAPS)
    parent.request_overrides = {}
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    return parent


@pytest.fixture
def builds(monkeypatch):
    """Capture _build_child_preserving_parent_tools kwargs (child construction
    would spawn a real subagent); return the list."""
    captured = []

    def fake_build(**kwargs):
        captured.append(kwargs)
        return MagicMock()

    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", fake_build)
    return captured


@pytest.fixture
def runs(monkeypatch):
    """Fake the child RUN loop (network boundary) with a completed row."""
    monkeypatch.setattr(
        dt, "_run_single_child",
        lambda *a, **k: {"task_index": 0, "status": "completed", "summary": "ok",
                         "api_calls": 1, "duration_seconds": 0.1, "model": "m",
                         "exit_reason": "completed"},
    )


# --- 1. real plugin registers its resolver ------------------------------------


def test_real_plugin_resolver_returns_six_key_route_receipt(load_plugin):
    mr = load_plugin("model-routing", settings={"roles": ROLES})
    from hermes_cli import plugins as plugins_mod

    receipt = plugins_mod.resolve_role_model(
        "observer", session_id="sess-1",
        parent_model="parent-model", parent_provider=PARENT_PROVIDER)

    assert set(receipt) == SIX_KEYS
    assert receipt["resolved"] is True
    assert receipt["role"] == "observer"
    assert receipt["model"] == ROUTED_MODEL
    assert receipt["provider"] == DEST_PROVIDER
    assert receipt["source"] == "route"
    assert ROUTED_MODEL in receipt["reason"]
    # the plugin's own surface agrees the host seam is live, and the resolution
    # landed in its audit ring with the caller's session
    status = json.loads(mr.tool("route_status", {}))
    assert status["host_seam"] == "registered"
    assert status["audit"][-1]["receipt"] == receipt
    assert status["audit"][-1]["session_id"] == "sess-1"


# --- 2. delegate model_role through the real host delegate path ---------------


def test_delegate_model_role_routes_child_to_destination_provider(load_plugin, fake_runtimes,
                                                                  builds, runs):
    load_plugin("model-routing", settings={"roles": ROLES})
    parent = _delegate_parent()

    out = dt.delegate_task(
        tasks=[{"goal": "observe the deploy", "model_role": "observer"}],
        background=True, parent_agent=parent,
    )

    assert json.loads(out)["status"] == "dispatched"
    kwargs = builds[0]
    # the child is built on the routed provider/model with the destination
    # runtime's own credentials — exactly like a delegation.provider pin
    assert kwargs["model"] == ROUTED_MODEL
    assert kwargs["override_provider"] == DEST_PROVIDER
    assert kwargs["override_base_url"] == f"https://{DEST_PROVIDER}.example/v1"
    assert kwargs["override_api_key"] == "dest-key"
    assert kwargs["override_api_mode"] == "chat_completions"
    # destination capabilities: str->bool filtered destination map, NEVER the
    # parent's openai_native_compaction trust
    assert kwargs["override_capabilities"] == DEST_CAPS
    assert "openai_native_compaction" not in kwargs["override_capabilities"]
    # the per-spawn audit row records the route and its posture
    record = dt.recent_spawn_routes()[-1]
    assert record["capability_posture"] == "destination"
    assert record["model_role"] == "observer"
    assert record["child_model"] == ROUTED_MODEL
    assert record["child_provider"] == DEST_PROVIDER
    assert set(record["receipt"]) == SIX_KEYS
    assert record["receipt"]["source"] == "route"
    assert record["receipt"]["model"] == ROUTED_MODEL


def test_explicit_refused_role_rejects_delegate_task(load_plugin, fake_runtimes, builds):
    load_plugin("model-routing", settings={"roles": {}, "fallback": "refuse"})
    parent = _delegate_parent()

    out = dt.delegate_task(
        tasks=[{"goal": "refused role task", "model_role": "observer"}],
        background=True, parent_agent=parent,
    )

    result = json.loads(out)
    assert "model_role 'observer' refused" in result["error"]
    assert builds == []          # no child was built
    assert dt.recent_spawn_routes() == []  # and nothing was spawned


def test_implicit_review_reviewer_refusal_inherits(load_plugin, fake_runtimes, builds, runs):
    """fallback: refuse applies ONLY to explicit model_role requests: the
    implicit /review reviewer lookup refusing must inherit and still spawn."""
    load_plugin("model-routing", settings={"roles": {}, "fallback": "refuse"})
    parent = _delegate_parent()

    out = dt.delegate_task(  # no explicit model_role: /review defaults to 'reviewer'
        goal="review this diff, implicit reviewer refused",
        background=True, parent_agent=parent, credentials_cfg={},
    )

    assert json.loads(out)["status"] == "dispatched"  # the /review child spawned
    kwargs = builds[0]
    assert kwargs["model"] is None            # today's inherit behavior
    assert kwargs["override_provider"] is None
    record = dt.recent_spawn_routes()[-1]
    assert record["capability_posture"] == "inherited"
    assert record["model_role"] == "reviewer"
    assert record["receipt"]["source"] == "refused"  # honest audit of the refusal


# --- 3. prewalk + model-routing ------------------------------------------------


def test_prewalk_arm_validates_cross_provider_route_via_real_seam(
        load_plugin, live_agent, workspace, fake_runtimes):
    agent = live_agent
    load_plugin("model-routing", settings={"roles": ROLES})
    pw = load_plugin("prewalk", settings={"replan_enabled": True})

    with bind_subagent_parent(agent):
        armed = json.loads(pw.tool("prewalk_arm", {}, session_id="s"))

    assert armed["success"] is True
    assert armed["target"] == ROUTED_MODEL
    assert armed["target_provider"] == DEST_PROVIDER
    assert armed["target_source"] == "route"
    # ARM-time validation resolved the destination through the real host seam
    # (dry run) — the fake ladder saw the cross-provider target before ready.
    assert fake_runtimes == [{"requested": DEST_PROVIDER, "target_model": ROUTED_MODEL}]
    # a dry run mutates nothing
    assert agent.model == "strong-planner" and agent.provider == PARENT_PROVIDER
    st = json.loads(pw.tool("prewalk_status", {}, session_id="s"))
    assert st["state"] == "armed"
    assert st["target_model"] == ROUTED_MODEL
    assert st["target_provider"] == DEST_PROVIDER
    assert st["target_source"] == "route"


def test_prewalk_ready_hands_off_cross_provider_and_replan_restores(
        load_plugin, live_agent, workspace, fake_runtimes):
    agent = live_agent
    _planned_todo(agent)
    load_plugin("model-routing", settings={"roles": ROLES})
    pw = load_plugin("prewalk", settings={"replan_enabled": True})

    with bind_subagent_parent(agent):
        assert json.loads(pw.tool("prewalk_arm", {}, session_id="s"))["success"] is True
        handed = json.loads(pw.tool("prewalk_ready", _handoff(), session_id="s"))

    assert handed["success"] is True and handed["handed_off"] is True
    assert handed["target"] == ROUTED_MODEL
    assert handed["target_provider"] == DEST_PROVIDER
    assert handed["target_source"] == "route"
    # real seam + real native switch_model: the agent now runs on the routed
    # destination runtime's key/base_url
    assert agent.model == ROUTED_MODEL
    assert agent.provider == DEST_PROVIDER
    assert agent.base_url == f"https://{DEST_PROVIDER}.example/v1"
    assert agent.api_key == "dest-key"
    # the seam replaced the trust map with the destination runtime's own map
    assert agent.capabilities == DEST_CAPS
    assert "openai_native_compaction" not in agent.capabilities

    with bind_subagent_parent(agent):
        replanned = json.loads(pw.tool(
            "prewalk_replan",
            {"failed_assumption": "the guard covers the crash path",
             "failed_check": "pytest tests/test_widget.py still segfaults"},
            session_id="s"))

    assert replanned["success"] is True and replanned["replanned"] is True
    # replan restores the ORIGINAL model AND provider
    assert replanned["original_model"] == "strong-planner"
    assert replanned["original_provider"] == PARENT_PROVIDER
    assert agent.model == "strong-planner"
    assert agent.provider == PARENT_PROVIDER
    st = json.loads(pw.tool("prewalk_status", {}, session_id="s"))
    assert st["original_model"] == "strong-planner"
    assert st["original_provider"] == PARENT_PROVIDER
    assert st["replan_used"] is True


# --- 4. background_review precedence with the real resolver --------------------


def _review_parent():
    agent = MagicMock()
    agent.session_id = "s"
    agent.model = "parent-model"
    agent.provider = PARENT_PROVIDER
    agent._current_main_runtime = MagicMock(return_value={
        "api_key": "pk", "base_url": "http://parent", "api_mode": "chat_completions"})
    return agent


def test_background_review_reviewer_routed_and_explicit_pin_beats_it(load_plugin, fake_runtimes):
    from agent.background_review import _resolve_review_runtime
    load_plugin("model-routing", settings={"roles": ROLES})
    agent = _review_parent()

    # no explicit pin: roles.reviewer routes the review runtime
    routed = _resolve_review_runtime(agent)
    assert routed["routed"] is True
    assert routed["provider"] == DEST_PROVIDER
    assert routed["model"] == REVIEWER_MODEL
    assert routed["base_url"] == f"https://{DEST_PROVIDER}.example/v1"
    assert routed["api_key"] == "dest-key"

    # an explicit auxiliary.background_review pin beats the reviewer route
    pinned = _resolve_review_runtime(agent, {"provider": PIN_PROVIDER, "model": PIN_MODEL})
    assert pinned["routed"] is True
    assert pinned["provider"] == PIN_PROVIDER
    assert pinned["model"] == PIN_MODEL
    assert pinned["base_url"] == f"https://{PIN_PROVIDER}.example/v1"


# --- 5. AC9: ultrathink on a routed agent, route visible in route_status -------


def test_ultrathink_applies_on_routed_agent_and_route_visible_in_audit(
        load_plugin, live_agent, fake_runtimes):
    agent = live_agent
    mr = load_plugin("model-routing", settings={"roles": ROLES})
    ks = load_plugin("keyword-steering", settings={})
    # keyword-steering's set_turn_reasoning resolves the CLI-surface agent
    mr.manager._cli_ref = SimpleNamespace(agent=agent)

    # route the agent through the real seams (real resolver -> real switch)
    receipt = mr.context.resolve_role_model("executor", agent=agent)
    assert receipt["source"] == "route"
    switch = mr.context.switch_agent_model(
        receipt["model"], receipt["provider"], reason="route executor", agent=agent)
    assert switch["switched"] is True
    assert switch["capability_posture"] == "destination"
    assert agent.model == ROUTED_MODEL and agent.provider == DEST_PROVIDER

    # ultrathink on the routed agent: the REAL host set_turn_reasoning applies
    guidance = ks.hook(
        "pre_llm_call", session_id="s", user_message="please ultrathink this design",
        conversation_history=[], is_first_turn=False, model=agent.model,
        platform="cli", turn_id="turn-1",
    )[0]["context"]
    assert "raised to 'ultra'" in guidance
    assert agent._plugin_reasoning_override == {"effort": "ultra",
                                                "reason": "magic keyword: ultrathink"}
    effect = json.loads(ks.tool("steering_status", {}, session_id="s"))["magic_effects"]["ultrathink"]
    assert effect["applied"] is True and effect["degraded"] is False
    assert effect["receipt"]["applied"] is True
    assert effect["receipt"]["effort"] == "ultra"

    # the routing receipt is visible in model-routing's route_status audit
    audit = json.loads(mr.tool("route_status", {}))["audit"]
    assert any(entry["receipt"]["role"] == "executor"
               and entry["receipt"]["source"] == "route"
               and entry["receipt"]["model"] == ROUTED_MODEL
               and entry["session_id"] == "s"
               for entry in audit)


# --- 6. plugin absent -----------------------------------------------------------


def test_plugin_absent_unavailable_and_delegate_matches_no_role(fake_runtimes, builds, runs):
    from tools.delegate_tool_config import _CAPABILITIES_UNSET
    from hermes_cli import plugins as plugins_mod

    receipt = plugins_mod.resolve_role_model(
        "observer", session_id="s", parent_model="parent-model",
        parent_provider=PARENT_PROVIDER)
    assert receipt == {"resolved": False, "role": "observer", "model": "parent-model",
                       "provider": PARENT_PROVIDER, "source": "unavailable",
                       "reason": "no role resolver registered"}

    parent = _delegate_parent()
    with_role = json.loads(dt.delegate_task(
        tasks=[{"goal": "role without the plugin", "model_role": "observer"}],
        background=True, parent_agent=parent))
    assert with_role["status"] == "dispatched"
    record = dt.recent_spawn_routes()[-1]
    assert record["receipt"]["source"] == "unavailable"
    assert record["model_role"] == "observer"  # requested-but-unresolved still visible
    assert record["child_model"] == "parent-model"
    assert record["child_provider"] == PARENT_PROVIDER
    assert record["capability_posture"] == "inherited"

    # ...and the child construction decision is byte-identical to no role at all
    plain = json.loads(dt.delegate_task(
        tasks=[{"goal": "no role at all"}], background=True, parent_agent=parent))
    assert plain["status"] == "dispatched"
    routing_keys = ("model", "override_provider", "override_base_url", "override_api_key",
                    "override_api_mode", "override_capabilities",
                    "override_request_overrides", "override_acp_command", "override_acp_args")
    assert ({k: builds[0][k] for k in routing_keys}
            == {k: builds[1][k] for k in routing_keys})
    assert builds[0]["override_provider"] is None
    assert builds[0]["override_capabilities"] is _CAPABILITIES_UNSET
