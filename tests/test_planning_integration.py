"""prewalk + subagent-effort integration on native Hermes contracts.

Hooks fire through the native manager (harness .hook -> manager.invoke_hook),
tools through context.dispatch_tool, commands through the native registered
handler. Agent binding uses the real bind_subagent_parent contextvar. The
The prewalk ready/replan tests drive the REAL agent_runtime_helpers.switch_model
— only the client constructor is mocked (the repo's own switch_model tests use
this exact pattern, tests/agent/test_switch_model_reasoning_override.py).
The effort plugin is pure shadow: the child's reasoning_config must never
change while the plugin watches.
"""

import json
from inspect import getclosurevars
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

from agent.api_request_hooks import ApiRequestHooksMixin
from agent.subagent_lifecycle import bind_subagent_parent

TARGET = "cheap-exec-model"


class _NativeUsageHookAgent(ApiRequestHooksMixin):
    """Bare carrier for the REAL hook normalizer: provider/api_mode attrs
    only. Mirrors how AIAgent dispatches post_api_request usage."""

    def __init__(self, provider, api_mode):
        self.provider = provider
        self.api_mode = api_mode


def _native_usage(raw_usage, *, provider, api_mode):
    """post_api_request payload exactly as the native emitter builds it:
    _usage_summary_for_api_request_hook over a provider-shaped response."""
    return _NativeUsageHookAgent(provider, api_mode)\
        ._usage_summary_for_api_request_hook(SimpleNamespace(usage=raw_usage))


@pytest.fixture
def no_live_agent(monkeypatch):
    monkeypatch.setattr("agent.subagent_lifecycle.get_active_subagent_parent", lambda: None)


@pytest.fixture
def live_agent(monkeypatch):
    """Real agent_runtime_helpers.switch_model; ONLY client construction mocked.

    Mirrors tests/agent/test_switch_model_reasoning_override.py._make_fake_agent
    so the entire switch pipeline (snapshot, swap, rollback guard, primary
    runtime, billing route) runs natively offline.
    """
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {})
    agent = MagicMock()
    agent.session_id = "s"
    agent.model = "strong-planner"
    agent.provider = "openai"
    agent.requested_provider = "openai"
    agent.base_url = "http://ep"
    agent.api_mode = "chat_completions"
    agent.api_key = "k"
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

# --- prewalk -----------------------------------------------------------------


def test_arm_carries_same_turn_nudge(load_plugin, live_agent):
    agent = live_agent
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    with bind_subagent_parent(agent):
        result = json.loads(h.tool("prewalk_arm", {}, session_id="s"))
        assert result["armed"] is True
        assert "PREWALK" in result["plan_nudge"]  # planner sees it THIS turn
        # arm is refused once armed (one-shot)
        assert json.loads(h.tool("prewalk_arm", {}, session_id="s"))["success"] is False
        # pre_llm_call nudge is also one-shot
        assert h.hook("pre_llm_call", session_id="s", user_message="do X")
        assert h.hook("pre_llm_call", session_id="s", user_message="again") == []


def test_scratch_edit_never_hands_off(load_plugin, live_agent, workspace):
    """No payload shape — failed, absent, malformed, blocked, or even a
    confirmed successful edit/write — may hand off: prewalk_ready is the
    only switch path."""
    agent = live_agent
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    h.tool("prewalk_arm", {}, session_id="s")
    with bind_subagent_parent(agent):
        for payload in (
            dict(tool_name="write_file", result='{"error": "disk full"}'),
            dict(tool_name="write_file", result=None),                      # absent result
            dict(tool_name="patch", result="not json at all"),              # malformed result
            dict(tool_name="patch", result='{"success": true}'),            # CONFIRMED success
            dict(tool_name="write_file", result='{"bytes_written": 7}',
                 error_type="tool_error"),                                  # hook-level failure
            dict(tool_name="write_file", result='{"bytes_written": 7}',
                 status="blocked"),
        ):
            h.hook("post_tool_call", session_id="s", args={}, **payload)
        assert agent.model == "strong-planner"  # switch_model never ran
        assert json.loads(h.tool("prewalk_status", {}, session_id="s"))["state"] == "armed"


def test_ready_requires_full_handoff_and_native_plan(load_plugin, live_agent, workspace):
    agent = live_agent
    from tools.todo_tool import TodoStore, todo_tool
    agent._todo_store = TodoStore()
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    h.tool("prewalk_arm", {}, session_id="s")
    with bind_subagent_parent(agent):
        # genuine native plan present, handoff incomplete -> refused
        todo_tool(todos=[{"id": "1", "content": "plan", "status": "pending"}],
                  store=agent._todo_store)
        # evidence path validation happens without executing the prose
        refused = json.loads(h.tool(
            "prewalk_ready", _handoff(files=["widget.py", "does/not/exist.py"]),
            session_id="s"))
        assert refused["success"] is False
        assert any("does/not/exist.py" in p for p in refused["problems"])
        refused = json.loads(h.tool(
            "prewalk_ready", _handoff(files=["../outside.py"]), session_id="s"))
        assert refused["success"] is False
        assert any("workspace-relative" in p for p in refused["problems"])
        # complete handoff but NO native plan -> refused
        agent._todo_store = TodoStore()
        refused = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
        assert refused["success"] is False and "todo plan" in refused["error"]
        assert agent.model == "strong-planner"
        # falsification_checks/replan_triggers/acceptance_ids are optional:
        # omitting them defaults to empty and the handoff validates.
        todo_tool(todos=[{"id": "1", "content": "plan", "status": "pending"}],
                  store=agent._todo_store)
        minimal = {k: v for k, v in _handoff().items()
                   if k not in ("falsification_checks", "replan_triggers",
                                "acceptance_ids")}
        handed = json.loads(h.tool("prewalk_ready", minimal, session_id="s"))
        assert handed["success"] is True
        assert handed["handoff"]["falsification_checks"] == []
        assert handed["handoff"]["acceptance_ids"] == []
        assert agent.model == TARGET


def test_ready_rejects_unconfined_and_evidence_less_handoff(load_plugin, live_agent,
                                                            workspace, tmp_path):
    """Located evidence must be real: directories and symlink-escaping
    entries are rejected; symbols are mandatory; empty callers need no
    reason (advisory only) — never an invented reference."""
    agent = live_agent
    from tools.todo_tool import TodoStore, todo_tool
    agent._todo_store = TodoStore()
    todo_tool(todos=[{"id": "1", "content": "plan", "status": "pending"}],
              store=agent._todo_store)
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    h.tool("prewalk_arm", {}, session_id="s")
    with bind_subagent_parent(agent):
        (workspace / "adir").mkdir()
        refused = json.loads(h.tool("prewalk_ready", _handoff(files=["adir"]),
                                    session_id="s"))
        assert refused["success"] is False
        assert any("regular file" in p for p in refused["problems"])
        outside = tmp_path.parent / "outside-evidence.py"
        outside.write_text("SECRET = 1\n")
        (workspace / "escape.py").symlink_to(outside)
        refused = json.loads(h.tool("prewalk_ready", _handoff(files=["escape.py"]),
                                    session_id="s"))
        assert refused["success"] is False
        assert any("symlink escape" in p for p in refused["problems"])
        bad = _handoff()
        bad.pop("symbols")
        refused = json.loads(h.tool("prewalk_ready", bad, session_id="s"))
        assert refused["success"] is False
        assert any("symbols" in p for p in refused["problems"])
        bad = _handoff()
        bad["callers"] = []
        handed = json.loads(h.tool("prewalk_ready", bad, session_id="s"))
        assert handed["success"] is True, \
            "empty callers are advisory: no callers_absence_reason required"
        assert handed["handoff"]["callers"] == []
        assert agent.model == TARGET


def test_tool_entry_migrates_rotated_session_without_nudge(load_plugin, live_agent):
    """Tool entry points follow the same-agent session rotation on their
    own: with plan_nudge disabled and no pre_llm_call in between, the armed
    state carries to the rotated session id instead of orphaning."""
    agent = live_agent
    h = load_plugin("prewalk", settings={"target_model": TARGET,
                                         "plan_nudge": False})
    with bind_subagent_parent(agent):
        h.tool("prewalk_arm", {}, session_id="s")
        agent.session_id = "rotated"
        st = json.loads(h.tool("prewalk_status", {}, session_id="rotated"))
        assert st["state"] == "armed"
        reset = json.loads(h.tool("prewalk_reset", {}, session_id="rotated"))
        assert reset["reset"] is True
        st = json.loads(h.tool("prewalk_status", {}, session_id="rotated"))
        assert st["state"] == "inactive"


def test_bounded_replan_returns_original_model_when_opted_in(load_plugin, live_agent, workspace):
    agent = live_agent
    from tools.todo_tool import TodoStore, todo_tool
    agent._todo_store = TodoStore()
    todo_tool(todos=[{"id": "1", "content": "plan", "status": "pending"}],
              store=agent._todo_store)
    h = load_plugin("prewalk", settings={"target_model": TARGET, "replan_enabled": True})
    h.tool("prewalk_arm", {}, session_id="s")
    with bind_subagent_parent(agent):
        h.tool("prewalk_ready", _handoff(), session_id="s")
        assert agent.model == TARGET
        refused = json.loads(h.tool("prewalk_replan", {}, session_id="s"))
        assert refused["success"] is False  # concrete disconfirmation required
        back = json.loads(h.tool("prewalk_replan", {
            "failed_assumption": "the widget guard covers reads",
            "failed_check": "pytest tests/test_widget.py::test_read"}, session_id="s"))
        assert back["success"] is True
        assert agent.model == "strong-planner"   # ORIGINAL planner model
        assert agent.provider == "openai"        # same endpoint, never cross-provider
        # bounded: the budget is spent — no oscillation, no re-arming
        assert json.loads(h.tool("prewalk_replan", {
            "failed_assumption": "again", "failed_check": "again"},
            session_id="s"))["success"] is False
        assert json.loads(h.tool("prewalk_arm", {}, session_id="s"))["success"] is False
        assert agent.model == "strong-planner"


def test_replan_default_off(load_plugin, live_agent, workspace):
    agent = live_agent
    from tools.todo_tool import TodoStore, todo_tool
    agent._todo_store = TodoStore()
    todo_tool(todos=[{"id": "1", "content": "plan", "status": "pending"}],
              store=agent._todo_store)
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    h.tool("prewalk_arm", {}, session_id="s")
    with bind_subagent_parent(agent):
        h.tool("prewalk_ready", _handoff(), session_id="s")
        assert agent.model == TARGET
        refused = json.loads(h.tool("prewalk_replan", {
            "failed_assumption": "x", "failed_check": "y"}, session_id="s"))
        assert refused["success"] is False and "operator-disabled" in refused["error"]
        assert agent.model == TARGET  # replan never switched anything


def test_valid_ready_switches_natively_once(load_plugin, live_agent, workspace):
    agent = live_agent
    from tools.todo_tool import TodoStore, todo_tool
    agent._todo_store = TodoStore()
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    h.tool("prewalk_arm", {}, session_id="s")
    with bind_subagent_parent(agent):
        # terminal/bash never switches (OMP parity E1)
        h.hook("post_tool_call", tool_name="terminal", session_id="s", result='{"output": "rm -rf"}')
        todo_tool(todos=[{"id": "1", "content": "plan step",
                          "status": "pending"}], store=agent._todo_store)
        # the implicit first-write switch is GONE: even a planned, confirmed
        # successful write leaves the planner steering
        h.hook("post_tool_call", tool_name="write_file", session_id="s",
               result='{"bytes_written": 12, "verified": true}')
        assert agent.model == "strong-planner"

        # validated handoff on the genuine native plan: REAL switch_model
        handed = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
        assert handed["success"] is True and handed["handed_off"] is True
        assert handed["handoff"]["chosen_approach"] == "guard the crash site"
        # REAL switch_model executed: identity preserved, model swapped,
        # OpenAI client constructed for the destination (same endpoint)
        assert agent.model == TARGET
        assert agent.provider == "openai"
        agent._create_openai_client.assert_called_once()
        # one-way, one-shot: ready cannot switch twice
        assert json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))["success"] is False
        agent._create_openai_client.assert_called_once()  # still exactly one switch
        assert json.loads(h.tool("prewalk_status", {}, session_id="s"))["state"] == "handed_off"


def test_detached_agent_never_switches(load_plugin, live_agent, workspace):
    agent = live_agent
    from tools.todo_tool import TodoStore, todo_tool
    agent._todo_store = TodoStore()
    todo_tool(todos=[{"id": "1", "content": "plan", "status": "pending"}],
              store=agent._todo_store)
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    h.tool("prewalk_arm", {}, session_id="s")
    with bind_subagent_parent(agent):
        agent._persist_disabled = True
        refused = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
        assert refused["success"] is False
        assert agent.model == "strong-planner"
        assert json.loads(h.tool("prewalk_status", {}, session_id="s"))["state"] == "armed"
        agent._persist_disabled = False
        handed = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
        assert handed["success"] is True  # retried once the agent is live again
        assert agent.model == TARGET


def test_explicit_reset_rearms(load_plugin, live_agent):
    agent = live_agent
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    h.tool("prewalk_arm", {}, session_id="s")
    h.hook("on_session_reset", session_id="s")
    assert h.hook("pre_llm_call", session_id="s", user_message="x") == []
    assert json.loads(h.tool("prewalk_arm", {}, session_id="s"))["armed"] is True


def test_compaction_migrates_on_same_agent_session_identity(load_plugin, live_agent, workspace):
    """Compaction rotation reassigns agent.session_id on the SAME object;
    prewalk tracks each live agent's own session identity and carries state
    forward once. _parent_session_id is DELEGATION lineage (agent_init sets
    it once at construction) and must never drive migration."""
    agent = live_agent
    agent._parent_session_id = None
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    with bind_subagent_parent(agent):
        # state sits under the live session id; rotation replaces it in place
        h.tool("prewalk_arm", {}, session_id="s")
        h.hook("pre_llm_call", session_id="s", user_message="x")  # records live id
        agent.session_id = "new"
        # rotation tracked on the agent's own identity; nudged rode forward
        assert h.hook("pre_llm_call", session_id="new", user_message="x") == []
        assert json.loads(h.tool("prewalk_status", {}, session_id="new"))["state"] == "armed"
        from tools.todo_tool import TodoStore, todo_tool
        # migrated session's todo gate is honored: no plan yet -> ready refuses
        agent._todo_store = TodoStore()
        refused = json.loads(h.tool("prewalk_ready", _handoff(), session_id="new"))
        assert refused["success"] is False and "todo plan" in refused["error"]
        assert agent.model == "strong-planner"  # gate held the switch
        # plan + validated handoff: migrated state completes the handoff
        todo_tool(todos=[{"id": "1", "content": "step",
                          "status": "pending"}], store=agent._todo_store)
        handed = json.loads(h.tool("prewalk_ready", _handoff(), session_id="new"))
        assert handed["success"] is True and handed["handed_off"] is True
        assert agent.model == TARGET  # one-way handoff completed on new id


def test_delegated_child_never_steals_parent_state(load_plugin, live_agent, workspace):
    """Native delegation lineage (child._parent_session_id == parent id)
    must NOT migrate: parent keeps its armed state; the child's own
    ready/plan path is honored independently under its own id."""
    from unittest.mock import MagicMock
    parent = live_agent
    child = MagicMock()
    child.session_id = "c1"
    child.reasoning_config = None
    child.model = "strong-planner"
    child.provider = "openai"
    child.api_key = ""
    child.base_url = ""
    child.api_mode = ""
    child._persist_disabled = False
    child._parent_session_id = "s"          # native delegation lineage
    from tools.todo_tool import TodoStore
    child._todo_store = TodoStore()
    # child_prewalk=False: the lane is NOT inherited; the plugin must also
    # never migrate on delegation lineage, or the child's first turn would
    # silently arm itself with the parent's state (the theft bug).
    h = load_plugin("prewalk", settings={"target_model": TARGET,
                                         "child_prewalk": False})
    h.tool("prewalk_arm", {}, session_id="s")
    h.hook("subagent_start", parent_session_id="s", child_session_id="c1",
           child_goal="carry the plan")
    # child's first turn: no nudge (child lane off), parent state untouched
    with bind_subagent_parent(child):
        assert h.hook("pre_llm_call", session_id="c1", user_message="plan") == []
        # Writes NEVER switch: prewalk_ready is the only handoff path, and
        # with the lane off there is no parent state for the child to run on.
        h.hook("post_tool_call", tool_name="write_file", session_id="c1",
               result='{"bytes_written": 3}', status="ok")
        assert child.model == "strong-planner"      # child never switched
        assert parent.model == "strong-planner"     # parent never touched
    with bind_subagent_parent(parent):
        assert json.loads(h.tool("prewalk_status", {}, session_id="s"))["state"] == "armed"
    # parent's state still intact under its own id; child's own explicit
    # arm (its own id, its own choice) works independently
    with bind_subagent_parent(child):
        armed_c1 = json.loads(h.tool("prewalk_arm", {}, session_id="c1"))
        assert armed_c1["success"] is True and armed_c1["armed"] is True
        # child's own todo_gate honored: EMPTY native store -> ready refuses
        # to switch even with a complete, path-valid handoff
        result = json.loads(h.tool("prewalk_ready", _handoff(), session_id="c1"))
        assert result["success"] is False and "todo plan" in result["error"]
        assert child.model == "strong-planner"
        assert json.loads(h.tool("prewalk_status", {}, session_id="c1"))["state"] == "armed"


def test_child_inherits_planning_lane_with_goal(load_plugin, no_live_agent):
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    h.hook("subagent_start", parent_session_id="s", child_session_id="child-1",
           child_goal="carry the plan")
    assert h.hook("pre_llm_call", session_id="child-1", user_message="carry the plan",
                  is_first_turn=True)

    h2 = load_plugin("prewalk", settings={"target_model": TARGET, "child_prewalk": False})
    h2.hook("subagent_start", parent_session_id="s", child_session_id="child-2", child_goal="g")
    assert h2.hook("pre_llm_call", session_id="child-2", user_message="g") == []


# --- prewalk cross-provider routing (target_provider / executor route) ------


def _fake_switch_seam(monkeypatch, *, dry_run_ok=True, fail_reason=None):
    """Fake host PluginContext.switch_agent_model: records calls, dry-runs
    per the host contract, and applies the switch to the agent on success."""
    from hermes_cli.plugins import PluginContext
    calls = []

    def fake(self, model, provider="", *, reason="", dry_run=False, agent=None):
        calls.append({"model": model, "provider": provider, "reason": reason,
                      "dry_run": bool(dry_run)})
        if dry_run:
            return {"switched": False, "resolvable": bool(dry_run_ok),
                    "model": model, "provider": provider,
                    "reason": "ok" if dry_run_ok else
                              f"no api_key configured for provider '{provider}'"}
        if fail_reason:
            return {"switched": False, "reason": fail_reason}
        agent_provider = str(getattr(agent, "provider", "") or "") if agent is not None else ""
        if agent is not None:
            agent.model = model
            if provider:
                agent.provider = provider
        return {"switched": True, "model": model,
                "provider": provider or agent_provider,
                "base_url_changed": bool(provider),
                "capability_posture": "destination", "reason": reason}

    monkeypatch.setattr(PluginContext, "switch_agent_model", fake, raising=False)
    return calls


def _fake_executor_route(monkeypatch, *, model, provider):
    """Fake host resolve_role_model adapter returning a route receipt."""
    from hermes_cli.plugins import PluginContext
    receipt = {"resolved": True, "role": "executor", "model": model,
               "provider": provider, "source": "route",
               "reason": "roles.executor configured"}

    def fake(self, role, *, agent=None):
        return dict(receipt, role=role)

    monkeypatch.setattr(PluginContext, "resolve_role_model", fake, raising=False)
    return receipt


def _planned_todo(agent):
    from tools.todo_tool import TodoStore, todo_tool
    agent._todo_store = TodoStore()
    todo_tool(todos=[{"id": "1", "content": "plan", "status": "pending"}],
              store=agent._todo_store)


def test_explicit_cross_provider_target_hands_off_via_host_seam(load_plugin, live_agent,
                                                                 workspace, monkeypatch):
    agent = live_agent
    _planned_todo(agent)
    h = load_plugin("prewalk", settings={"target_model": TARGET,
                                         "target_provider": "anthropic"})
    calls = _fake_switch_seam(monkeypatch)
    with bind_subagent_parent(agent):
        armed = json.loads(h.tool("prewalk_arm", {}, session_id="s"))
        assert armed["success"] is True
        assert armed["target"] == TARGET
        assert armed["target_provider"] == "anthropic"
        assert armed["target_source"] == "explicit"
        # cross-provider destination validated at ARM time with a dry run
        dry = [c for c in calls if c["dry_run"]]
        assert len(dry) == 1
        assert (dry[0]["model"], dry[0]["provider"]) == (TARGET, "anthropic")
        handed = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
        assert handed["success"] is True and handed["handed_off"] is True
        real = [c for c in calls if not c["dry_run"]]
        assert len(real) == 1
        assert (real[0]["model"], real[0]["provider"]) == (TARGET, "anthropic")
        assert agent.model == TARGET and agent.provider == "anthropic"
        # the host seam performed the switch; the native same-endpoint
        # path was never entered
        agent._create_openai_client.assert_not_called()


def test_route_executor_target_used_when_target_model_unset(load_plugin, live_agent,
                                                            workspace, monkeypatch):
    agent = live_agent
    _planned_todo(agent)
    h = load_plugin("prewalk", settings={})
    _fake_executor_route(monkeypatch, model="route-exec", provider="openai")
    calls = _fake_switch_seam(monkeypatch)
    with bind_subagent_parent(agent):
        armed = json.loads(h.tool("prewalk_arm", {}, session_id="s"))
        assert armed["success"] is True
        assert armed["target"] == "route-exec"
        assert armed["target_source"] == "route"
        handed = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
        assert handed["success"] is True
        real = [c for c in calls if not c["dry_run"]]
        assert len(real) == 1
        assert real[0]["model"] == "route-exec" and real[0]["provider"] == "openai"
        # same provider as the planner: no dry-run arm validation required
        assert all(not c["dry_run"] for c in calls)
        st = json.loads(h.tool("prewalk_status", {}, session_id="s"))
        assert st["state"] == "handed_off"
        assert st["target_model"] == "route-exec"
        assert st["target_provider"] == "openai"
        assert st["target_source"] == "route"


def test_explicit_target_model_beats_executor_route(load_plugin, live_agent,
                                                    workspace, monkeypatch):
    agent = live_agent
    _planned_todo(agent)
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    _fake_executor_route(monkeypatch, model="route-exec", provider="openai")
    calls = _fake_switch_seam(monkeypatch)
    with bind_subagent_parent(agent):
        armed = json.loads(h.tool("prewalk_arm", {}, session_id="s"))
        assert armed["target"] == TARGET and armed["target_source"] == "explicit"
        handed = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
        assert handed["success"] is True
        real = [c for c in calls if not c["dry_run"]]
        assert len(real) == 1
        assert real[0]["model"] == TARGET and real[0]["provider"] == ""
        st = json.loads(h.tool("prewalk_status", {}, session_id="s"))
        assert st["target_source"] == "explicit"
        agent._create_openai_client.assert_not_called()  # seam handled the switch


def test_unresolvable_cross_provider_target_refuses_arm_naming_provider(load_plugin,
                                                                        live_agent,
                                                                        monkeypatch):
    agent = live_agent
    h = load_plugin("prewalk", settings={"target_model": TARGET,
                                         "target_provider": "bedrock"})
    calls = _fake_switch_seam(monkeypatch, dry_run_ok=False)
    with bind_subagent_parent(agent):
        refused = json.loads(h.tool("prewalk_arm", {}, session_id="s"))
        assert refused["success"] is False
        assert "bedrock" in refused["error"]
        assert "no api_key configured" in refused["error"]
        assert agent.model == "strong-planner"
        assert json.loads(h.tool("prewalk_status", {}, session_id="s"))["state"] == "inactive"
        # dry-run probe only: the real switch was never attempted
        assert calls and all(c["dry_run"] for c in calls)


def test_cross_provider_target_without_host_seam_refuses_arm(load_plugin, live_agent,
                                                             monkeypatch):
    agent = live_agent
    from hermes_cli.plugins import PluginContext
    # deterministic regardless of whether the host has grown the seams
    monkeypatch.delattr(PluginContext, "switch_agent_model", raising=False)
    monkeypatch.delattr(PluginContext, "resolve_role_model", raising=False)
    h = load_plugin("prewalk", settings={"target_model": TARGET,
                                         "target_provider": "anthropic"})
    with bind_subagent_parent(agent):
        refused = json.loads(h.tool("prewalk_arm", {}, session_id="s"))
        assert refused["success"] is False
        assert "anthropic" in refused["error"]
        assert "switch_agent_model" in refused["error"]
        assert agent.model == "strong-planner"


def test_same_provider_target_without_seam_uses_native_switch(load_plugin, live_agent,
                                                              workspace, monkeypatch):
    agent = live_agent
    _planned_todo(agent)
    from hermes_cli.plugins import PluginContext
    monkeypatch.delattr(PluginContext, "switch_agent_model", raising=False)
    monkeypatch.delattr(PluginContext, "resolve_role_model", raising=False)
    h = load_plugin("prewalk", settings={"target_model": TARGET,
                                         "target_provider": "openai"})
    with bind_subagent_parent(agent):
        assert json.loads(h.tool("prewalk_arm", {}, session_id="s"))["success"] is True
        handed = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
        assert handed["success"] is True and handed["handed_off"] is True
        assert agent.model == TARGET and agent.provider == "openai"
        # native switch_model ran (client construction is its only mock)
        agent._create_openai_client.assert_called_once()


def test_refused_switch_stays_armed_and_states_reason(load_plugin, live_agent,
                                                      workspace, monkeypatch):
    agent = live_agent
    _planned_todo(agent)
    h = load_plugin("prewalk", settings={"target_model": TARGET,
                                         "target_provider": "anthropic"})
    _fake_switch_seam(monkeypatch, fail_reason="destination runtime rejected the api_key")
    with bind_subagent_parent(agent):
        assert json.loads(h.tool("prewalk_arm", {}, session_id="s"))["success"] is True
        failed = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
        assert failed["success"] is False
        # honest receipt: the refusal reason reaches the planner, not silence
        assert "destination runtime rejected the api_key" in failed["error"]
        assert agent.model == "strong-planner" and agent.provider == "openai"
        st = json.loads(h.tool("prewalk_status", {}, session_id="s"))
        assert st["state"] == "armed"  # stays armed: retry on next ready


def test_replan_restores_original_model_and_provider(load_plugin, live_agent,
                                                     workspace, monkeypatch):
    agent = live_agent
    _planned_todo(agent)
    h = load_plugin("prewalk", settings={"target_model": TARGET,
                                         "target_provider": "anthropic",
                                         "replan_enabled": True})
    calls = _fake_switch_seam(monkeypatch)
    with bind_subagent_parent(agent):
        h.tool("prewalk_arm", {}, session_id="s")
        h.tool("prewalk_ready", _handoff(), session_id="s")
        assert agent.model == TARGET and agent.provider == "anthropic"
        back = json.loads(h.tool("prewalk_replan", {
            "failed_assumption": "the widget guard covers reads",
            "failed_check": "pytest tests/test_widget.py::test_read"}, session_id="s"))
        assert back["success"] is True
        assert back["original_model"] == "strong-planner"
        assert back["original_provider"] == "openai"
        restore = [c for c in calls if c["reason"] == "prewalk_replan"]
        assert len(restore) == 1
        assert (restore[0]["model"], restore[0]["provider"]) == ("strong-planner", "openai")
        assert agent.model == "strong-planner" and agent.provider == "openai"
        assert agent._create_openai_client.call_count == 0  # seam path throughout
        st = json.loads(h.tool("prewalk_status", {}, session_id="s"))
        assert st["original_provider"] == "openai"


def test_child_lane_not_armed_cross_provider_without_seam(load_plugin, live_agent,
                                                          monkeypatch):
    agent = live_agent
    from hermes_cli.plugins import PluginContext
    # deterministic regardless of whether the host has grown the seams
    monkeypatch.delattr(PluginContext, "switch_agent_model", raising=False)
    monkeypatch.delattr(PluginContext, "resolve_role_model", raising=False)
    h = load_plugin("prewalk", settings={"target_model": TARGET,
                                         "target_provider": "anthropic"})
    with bind_subagent_parent(agent):
        h.hook("subagent_start", parent_session_id="s", child_session_id="c1",
               child_goal="carry the plan")
        # refused at ARM time: the child lane stays un-armed, loudly
        st = json.loads(h.tool("prewalk_status", {}, session_id="c1"))
        assert st["state"] == "inactive"
        assert h.hook("pre_llm_call", session_id="c1", user_message="carry the plan",
                      is_first_turn=True) == []
        assert agent.model == "strong-planner"  # no switch attempted


def test_child_lane_not_armed_when_dry_run_unresolvable(load_plugin, live_agent,
                                                        monkeypatch, caplog):
    import logging
    agent = live_agent
    h = load_plugin("prewalk", settings={"target_model": TARGET,
                                         "target_provider": "anthropic"})
    calls = _fake_switch_seam(monkeypatch, dry_run_ok=False)
    caplog.set_level(logging.WARNING)
    with bind_subagent_parent(agent):
        h.hook("subagent_start", parent_session_id="s", child_session_id="c1",
               child_goal="carry the plan")
        # the resolvability probe ran (was not skipped) and refused
        assert calls and all(c["dry_run"] for c in calls)
        assert calls[0]["provider"] == "anthropic"
        st = json.loads(h.tool("prewalk_status", {}, session_id="c1"))
        assert st["state"] == "inactive"
        # refusal is logged, never silent
        assert "refusing to arm child c1" in caplog.text
        assert "anthropic" in caplog.text


def test_child_lane_armed_when_cross_provider_resolvable(load_plugin, live_agent,
                                                         monkeypatch):
    agent = live_agent
    h = load_plugin("prewalk", settings={"target_model": TARGET,
                                         "target_provider": "anthropic"})
    calls = _fake_switch_seam(monkeypatch)
    with bind_subagent_parent(agent):
        h.hook("subagent_start", parent_session_id="s", child_session_id="c1",
               child_goal="carry the plan")
        # validated via dry run: the child lane arms
        assert [c for c in calls if c["dry_run"]]
        st = json.loads(h.tool("prewalk_status", {}, session_id="c1"))
        assert st["state"] == "armed"
        assert st["target_provider"] == "anthropic"
        assert st["target_source"] == "explicit"
        # the plan nudge fires once on the child's first turn
        assert h.hook("pre_llm_call", session_id="c1", user_message="carry the plan",
                      is_first_turn=True)
        assert agent.model == "strong-planner"  # arming never switches


# --- prewalk child lane: routing-role gating + automatic handoff -------------


def _child_agent(session_id="c1", model="strong-planner", provider="openai"):
    child = MagicMock()
    child.session_id = session_id
    child.model = model
    child.provider = provider
    child._persist_disabled = False
    return child


def test_scout_child_not_armed(load_plugin, live_agent, monkeypatch):
    """OMP parity: only task-lane children prewalk. A child running under any
    other routing role (scout, reviewer, ...) arms nothing, is never nudged,
    and its post_tool_call switches nothing."""
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    _fake_switch_seam(monkeypatch)
    child = _child_agent()
    with bind_subagent_parent(child):
        h.hook("subagent_start", parent_session_id="s", child_session_id="c1",
               child_goal="sweep the repo", child_model_role="scout")
        assert json.loads(h.tool("prewalk_status", {}, session_id="c1"))["state"] == "inactive"
        assert h.hook("pre_llm_call", session_id="c1", user_message="sweep",
                      is_first_turn=True) == []
        h.hook("post_tool_call", session_id="c1", tool_name="read_file",
               args={"path": "widget.py"}, result="ok", status="ok")
        assert child.model == "strong-planner"


def test_task_and_plain_children_armed(load_plugin, live_agent):
    """role == "task" arms, no role at all (plain delegation) arms, and the
    configured child_prewalk_roles list extends the lane."""
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    h.hook("subagent_start", parent_session_id="s", child_session_id="c-task",
           child_goal="g", child_model_role="task")
    h.hook("subagent_start", parent_session_id="s", child_session_id="c-plain",
           child_goal="g")
    assert json.loads(h.tool("prewalk_status", {}, session_id="c-task"))["state"] == "armed"
    assert json.loads(h.tool("prewalk_status", {}, session_id="c-plain"))["state"] == "armed"

    h2 = load_plugin("prewalk", settings={"target_model": TARGET,
                                          "child_prewalk_roles": ["researcher"]})
    h2.hook("subagent_start", parent_session_id="s", child_session_id="c2",
            child_goal="g", child_model_role="researcher")
    assert json.loads(h2.tool("prewalk_status", {}, session_id="c2"))["state"] == "armed"
    h2.hook("subagent_start", parent_session_id="s", child_session_id="c3",
            child_goal="g", child_model_role="task")
    assert json.loads(h2.tool("prewalk_status", {}, session_id="c3"))["state"] == "inactive"


def test_child_auto_handoff_once_with_executor_route(load_plugin, live_agent, monkeypatch):
    """The child lane hands off automatically on the child's FIRST post_tool_call
    — no prewalk_ready, no todo gate — using the routed (executor model,
    provider), exactly once; a second post_tool_call is a no-op."""
    agent = live_agent
    route = _fake_executor_route(monkeypatch, model="exec-m", provider="zai")
    calls = _fake_switch_seam(monkeypatch)
    h = load_plugin("prewalk", settings={})  # target comes from the executor route
    child = _child_agent()
    with bind_subagent_parent(agent):
        h.hook("subagent_start", parent_session_id="s", child_session_id="c1",
               child_goal="carry the plan", child_model_role="task")
        st = json.loads(h.tool("prewalk_status", {}, session_id="c1"))
        assert st["state"] == "armed"
        assert st["target"] == "exec-m" and st["target_provider"] == "zai"
        assert st["target_source"] == "route"
    with bind_subagent_parent(child):
        # the planning nudge still fires on the child's first call
        assert h.hook("pre_llm_call", session_id="c1", user_message="plan",
                      is_first_turn=True)
        h.hook("post_tool_call", session_id="c1", tool_name="read_file",
               args={"path": "widget.py"}, result="ok", status="ok")
        assert (child.model, child.provider) == ("exec-m", "zai")
        # second tool result in the same turn: never again
        h.hook("post_tool_call", session_id="c1", tool_name="read_file",
               args={"path": "other.py"}, result="ok", status="ok")
    real = [c for c in calls if not c["dry_run"]]
    assert len(real) == 1
    assert (real[0]["model"], real[0]["provider"]) == (route["model"], route["provider"])
    st = json.loads(h.tool("prewalk_status", {}, session_id="c1"))
    assert st["state"] == "handed_off" and st["handoff"] is False


def test_child_handoff_no_switch_when_already_on_target(load_plugin, live_agent, monkeypatch):
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    calls = _fake_switch_seam(monkeypatch)
    child = _child_agent(model=TARGET)
    with bind_subagent_parent(child):
        h.hook("subagent_start", parent_session_id="s", child_session_id="c1",
               child_goal="g", child_model_role="task")
        h.hook("post_tool_call", session_id="c1", tool_name="read_file",
               args={"path": "widget.py"}, result="ok", status="ok")
    assert [c for c in calls if not c["dry_run"]] == []  # no switch attempted
    assert child.model == TARGET
    assert json.loads(h.tool("prewalk_status", {}, session_id="c1"))["state"] == "handed_off"


def test_child_handoff_follows_same_agent_session_rotation(load_plugin, live_agent,
                                                           monkeypatch):
    """Compaction reassigns agent.session_id on the SAME object while the child is
    mid-turn: its first post_tool_call must still consume the armed lane under the
    rotated id, not be dropped by the child_lane guard with the entry orphaned and
    the child never switched."""
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    calls = _fake_switch_seam(monkeypatch)
    child = _child_agent()
    with bind_subagent_parent(child):
        h.hook("subagent_start", parent_session_id="s", child_session_id="c1",
               child_goal="g", child_model_role="task")
        # the child's first model turn records its live session identity
        assert h.hook("pre_llm_call", session_id="c1", user_message="plan",
                      is_first_turn=True)
        child.session_id = "c1-rotated"
        h.hook("post_tool_call", session_id="c1-rotated", tool_name="read_file",
               args={"path": "widget.py"}, result="ok", status="ok")
        assert child.model == TARGET
    real = [c for c in calls if not c["dry_run"]]
    assert len(real) == 1 and real[0]["model"] == TARGET
    assert json.loads(h.tool("prewalk_status", {},
                             session_id="c1-rotated"))["state"] == "handed_off"
    assert json.loads(h.tool("prewalk_status", {}, session_id="c1"))["state"] == "inactive"


def test_child_handoff_failure_warned_and_never_retried(load_plugin, live_agent,
                                                        monkeypatch, caplog):
    import logging
    reason = "host declined: destination runtime unresolvable"
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    calls = _fake_switch_seam(monkeypatch, fail_reason=reason)
    caplog.set_level(logging.WARNING)
    child = _child_agent()
    with bind_subagent_parent(child):
        h.hook("subagent_start", parent_session_id="s", child_session_id="c1",
               child_goal="g", child_model_role="task")
        h.hook("post_tool_call", session_id="c1", tool_name="read_file",
               args={"path": "widget.py"}, result="ok", status="ok")
        assert child.model == "strong-planner"  # child continues on its current model
        assert "child auto handoff failed for session c1" in caplog.text
        assert reason in caplog.text  # the receipt reason, at warning level
        # never retried: a second post_tool_call attempts nothing
        h.hook("post_tool_call", session_id="c1", tool_name="read_file",
               args={"path": "other.py"}, result="ok", status="ok")
    assert [c for c in calls if not c["dry_run"]] and len(calls) == 1


def test_top_level_lane_still_requires_prewalk_ready(load_plugin, live_agent, workspace,
                                                     monkeypatch):
    """post_tool_call belongs to the CHILD lane only: a top-level session armed
    via prewalk_arm is untouched by the child's first-tool-call handoff — and
    the validated prewalk_ready path still works unchanged."""
    agent = live_agent
    _planned_todo(agent)
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    calls = _fake_switch_seam(monkeypatch)
    with bind_subagent_parent(agent):
        h.tool("prewalk_arm", {}, session_id="s")
        h.hook("post_tool_call", session_id="s", tool_name="read_file",
               args={"path": "widget.py"}, result="ok", status="ok")
        assert agent.model == "strong-planner"  # no auto switch for the parent
        assert json.loads(h.tool("prewalk_status", {}, session_id="s"))["state"] == "armed"
        assert [c for c in calls if not c["dry_run"]] == []
        result = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
        assert result["success"] is True and result["handed_off"] is True
        assert agent.model == TARGET


# --- subagent-effort (pure shadow) ---------------------------------------------


@pytest.fixture
def classifier(monkeypatch):
    requests = []

    class Reply:  # real context manager — urlopen result is used with `with`
        def __init__(self, verdict):
            self._body = json.dumps({"difficulty": verdict}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, timeout=None):
        requests.append(req)
        verdict = "complex" if "complex-marker" in req.data.decode() else "simple"
        return Reply(verdict)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("CLASSIFIER_API_KEY", "test-key")
    return requests


def test_shadow_never_touches_child_effort(load_plugin, classifier):
    agent = MagicMock()
    agent.session_id = "child-x"
    agent.reasoning_config = {"enabled": True, "effort": "medium"}
    agent.model = "m"
    agent.provider = "openai"
    agent.api_mode = "chat_completions"
    h = load_plugin("subagent-effort")
    with bind_subagent_parent(agent):
        h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
               child_goal="complex-marker refactor the parser")
        h.hook("pre_llm_call", session_id="child-x", user_message="complex-marker ...")
    _settle_effort(h)
    # pure shadow: the actual reasoning_config was read, never written
    assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
    row = json.loads(h.command("subagent_effort"))["children"][0]
    assert row["verdict"] == "complex" and row["suggested_effort"] == "high"
    # unknown capability table -> explicitly unknown, never an invented level
    assert row["supported_efforts"] == "unknown"
    assert row["clamped_effort"] == "unknown"
    assert row["child_reasoning_config"] == {"enabled": True, "effort": "medium"}
    assert row["child_model"] == "m" and row["child_provider"] == "openai"


def test_recommendation_clamps_via_native_policy(load_plugin, classifier):
    agent = MagicMock()
    agent.session_id = "child-x"
    agent.reasoning_config = None
    agent.model = "m"
    agent.provider = "openrouter"
    agent.api_mode = "chat_completions"
    h = load_plugin("subagent-effort", settings={"high_effort": "xhigh"})
    # route supports only low/medium: native clamp_effort must pull the
    # suggestion down, never invent or escalate
    with bind_subagent_parent(agent):
        with patch(
            "hermes_cli.models_reasoning_caps.openrouter_model_reasoning_capabilities",
            return_value={"supports_reasoning": True, "supported_efforts": ["low", "medium"]},
        ):
            h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
                   child_goal="complex-marker hard")
            h.hook("pre_llm_call", session_id="child-x", user_message="hard")
    _settle_effort(h)
    row = json.loads(h.command("subagent_effort"))["children"][0]
    assert row["supported_efforts"] == ["low", "medium"]
    assert row["clamped_effort"] in row["supported_efforts"]


def test_invalid_effort_config_idles_plugin(load_plugin, classifier):
    h = load_plugin("subagent-effort", settings={"low_effort": "quantum"})
    # behavior, not registration: the idle plugin never issues a
    # classifier call and never invents a verdict; the child keeps an
    # honest unavailable row and the status route stays inspectable with
    # the config error (genuine operational feature, never silently dead)
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
           child_goal="complex-marker hard task")
    assert classifier == []
    summary = json.loads(h.command("subagent_effort"))
    # config-invalid: plugin stays idle (no classifier HTTP call at all),
    # but the child keeps an honest unavailable row — never a fabricated
    # verdict, and the config_error stays diagnosable on the summary.
    row = json.loads(h.command("subagent_effort"))["children"][0]
    assert row["classification_state"] == "unavailable"
    assert row["classification_reason"] == "not_configured"
    assert row["verdict"] is None and row["suggested_effort"] is None
    assert "quantum" in summary["config_error"]


def test_effort_levels_are_native_parsable(load_plugin, classifier):
    from hermes_constants import parse_reasoning_effort
    from agent.reasoning_effort import clamp_effort
    from hermes_cli.models import clamp_reasoning_effort_to_supported

    # real parser: every suggestion level produces a native config dict
    for level in ("none", "minimal", "low", "medium", "high", "xhigh"):
        assert parse_reasoning_effort(level) is not None
        h = load_plugin("subagent-effort", settings={"low_effort": level})
        assert json.loads(h.command("subagent_effort"))["mode"] == "shadow"
    # real clamp policy: nearest weaker, never escalate
    assert clamp_reasoning_effort_to_supported("xhigh", ["low", "medium"]) == "medium"
    assert clamp_effort("xhigh", None) == "xhigh"  # unknown table passes through
    assert parse_reasoning_effort("quantum") is None


def test_outcome_evidence_and_summary_distinguish_unknowns(load_plugin, classifier):
    h = load_plugin("subagent-effort")
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
           child_goal="complex-marker do work")
    # post_api_request payloads exactly as the native emitter builds them:
    # real ApiRequestHooksMixin._usage_summary_for_api_request_hook over
    # provider-shaped responses (OpenAI chat and Anthropic).
    openai_usage = _native_usage(
        SimpleNamespace(prompt_tokens=100, completion_tokens=40,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=30)),
        provider="openai", api_mode="chat_completions")
    anthropic_usage = _native_usage(
        SimpleNamespace(input_tokens=40, output_tokens=10,
                        cache_read_input_tokens=10),
        provider="anthropic", api_mode="anthropic_messages")
    for usage in (openai_usage, anthropic_usage):
        # native hook payloads carry canonical buckets, never completion_tokens
        assert "completion_tokens" not in usage
        # cache accounting invariant: prompt + output == total
        assert usage["prompt_tokens"] + usage["output_tokens"] == usage["total_tokens"]
    # OpenAI chat: cached tokens are contained in prompt_tokens
    assert openai_usage["prompt_tokens"] == 100
    assert openai_usage["output_tokens"] == 40
    assert openai_usage["input_tokens"] == 70
    assert openai_usage["cache_read_tokens"] == 30
    # Anthropic: cache buckets are additive on top of input_tokens
    assert anthropic_usage["prompt_tokens"] == 50
    assert anthropic_usage["output_tokens"] == 10
    assert anthropic_usage["input_tokens"] == 40
    assert anthropic_usage["cache_read_tokens"] == 10
    h.hook("post_api_request", session_id="child-x", usage=openai_usage)
    h.hook("post_api_request", session_id="child-x", usage=anthropic_usage)
    h.hook("subagent_stop", parent_session_id="p", child_session_id="child-x",
           child_status="completed", duration_ms=1234,
           tool_call_history=[
               {"tool_name": "write_file", "tool_input": {"targets": {"path": "/a"}}, "status": "ok"},
               {"tool_name": "write_file", "tool_input": {"targets": {"path": "/a"}}, "status": "ok"},
           ])
    _settle_effort(h)

    summary = json.loads(h.command("subagent_effort"))
    assert summary["mode"] == "shadow"
    row = summary["children"][0]
    assert row["verdict"] == "complex" and row["suggested_effort"] == "high"
    # lifecycle fact only; repeated targets is a PROXY metric
    assert row["outcome_status"] == "completed"
    assert row["latency_ms"] == 1234
    assert row["repeated_mutation_targets"] == ["/a"]
    # raw native usage fields + honest cost provenance
    assert row["prompt_tokens"] == 150
    assert row["completion_tokens"] == 50
    assert row["total_tokens"] == 200
    assert row["requests"] == 2
    assert row["cost_usd"] is None
    assert row["cost_status"] == "unknown"
    # calibration row: lifecycle outcome was observed for this child
    assert row["outcome_observed"] is True
    assert "proxy metric" in summary["note"] and "never applies" in summary["note"]


def test_native_child_cost_captured_on_post_llm_call(load_plugin, classifier):
    """Same native accessors delegate_tool_child_run.py reads, captured while
    the child agent is alive; native partial/estimated status preserved, cost
    kept independently of token usage (unknown, not fabricated 0), and
    unproven values (negative/non-numeric) stay None — never invented."""
    agent = SimpleNamespace(
        session_id="child-x", reasoning_config=None, model="m", provider="openai",
        session_estimated_cost_usd=0.123456, session_cost_status="estimated")
    h = load_plugin("subagent-effort")
    with bind_subagent_parent(agent):
        h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
               child_goal="complex-marker do work")
        h.hook("post_llm_call", session_id="child-x")
        # verification captured live: native ledger resolves child's context
        h.hook("subagent_stop", parent_session_id="p", child_session_id="child-x",
               child_status="completed", duration_ms=500, tool_call_history=[])
        row = json.loads(h.command("subagent_effort"))["children"][0]
        assert row["usage"]["cost_usd"] == 0.123456
        assert row["usage"]["cost_usd"] == round(agent.session_estimated_cost_usd, 6)
        assert row["usage"]["cost_status"] == "estimated"  # native, not "actual billing"
        # zero-API child: cost captured independently of usage; token fields
        # unknown (None) — no observed post_api_request, none fabricated
        assert row["usage"]["total_tokens"] is None
        assert row["usage"]["requests"] is None
        # unproven negative value stays None, unknown status
        agent.session_estimated_cost_usd = -1.0
        h.hook("subagent_start", parent_session_id="p", child_session_id="child-y",
               child_goal="complex-marker second")
        h.hook("post_llm_call", session_id="child-y")
        h.hook("subagent_stop", parent_session_id="p", child_session_id="child-y",
               child_status="completed", duration_ms=500, tool_call_history=[])
    rows = json.loads(h.command("subagent_effort"))["children"]
    row_y = next(r for r in rows if r["child_session_id"] == "child-y")
    assert row_y["usage"]["cost_usd"] is None
    assert row_y["usage"]["cost_status"] == "unknown"
    # non-numeric amount: unproven — None, unknown, never a native status
    agent.session_estimated_cost_usd = "banana"
    agent.session_cost_status = "estimated"
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-z",
           child_goal="complex-marker third")
    h.hook("post_llm_call", session_id="child-z")
    h.hook("subagent_stop", parent_session_id="p", child_session_id="child-z",
           child_status="completed", duration_ms=500, tool_call_history=[])
    row_z = next(r for r in json.loads(h.command("subagent_effort"))["children"]
                 if r["child_session_id"] == "child-z")
    assert row_z["usage"]["cost_usd"] is None
    assert row_z["usage"]["cost_status"] == "unknown"
    # parent-side stop never reads the ledger (would resolve the PARENT
    # workspace): child evidence is the live capture or explicit unknown,
    # never the parent's evidence, never fabricated
    assert (isinstance(row_z["verification"], dict)
            or row_z["verification"] == "unknown")


def test_real_producer_history_counts_only_mutation_paths(load_plugin, classifier):
    """Producer/consumer evidence: REAL _subagent_stop_tool_call_history
    output (tools/delegate_tool_results.py:134-156) on production-shaped
    trace rows. The sanitizer keeps list-valued targets (web_extract urls)
    — raw list keys would crash with unhashable TypeError and lose the
    whole outcome; reads and failed mutations must not count as rework."""
    from tools.delegate_tool_results import _subagent_stop_tool_call_history
    trace = [
        # genuine patch/write_file pair on the same path
        {"tool": "patch", "status": "ok", "args_bytes": 120, "result_bytes": 40,
         "input_summary": {"argument_keys": ["path", "changes"],
                           "targets": {"path": "/x"}}},
        {"tool": "write_file", "status": "ok", "args_bytes": 90, "result_bytes": 3,
         "input_summary": {"argument_keys": ["path", "content"],
                           "targets": {"path": "/x"}}},
        # web_extract carries the LIST target shape (urls)
        {"tool": "web_extract", "status": "ok", "args_bytes": 40, "result_bytes": 500,
         "input_summary": {"argument_keys": ["urls"],
                           "targets": {"urls": ["https://example.test/a"]}}},
        {"tool": "web_extract", "status": "ok", "args_bytes": 40, "result_bytes": 500,
         "input_summary": {"argument_keys": ["urls"],
                           "targets": {"urls": ["https://example.test/a"]}}},
        # repeated READ pair — reads are not rework
        {"tool": "read_file", "status": "ok", "args_bytes": 10, "result_bytes": 2000,
         "input_summary": {"argument_keys": ["path"], "targets": {"path": "/r"}}},
        {"tool": "read_file", "status": "ok", "args_bytes": 10, "result_bytes": 2000,
         "input_summary": {"argument_keys": ["path"], "targets": {"path": "/r"}}},
        # failed mutation pair — not confirmed success
        {"tool": "patch", "status": "error", "args_bytes": 60, "result_bytes": 0,
         "input_summary": {"argument_keys": ["path"], "targets": {"path": "/y"}}},
        {"tool": "patch", "status": "error", "args_bytes": 60, "result_bytes": 0,
         "input_summary": {"argument_keys": ["path"], "targets": {"path": "/y"}}},
    ]
    history = _subagent_stop_tool_call_history(trace)
    assert len(history) == 8  # native producer sanitized every row
    agent = SimpleNamespace(
        session_id="child-l", reasoning_config=None, model="m", provider="openai",
        session_estimated_cost_usd=0.25, session_cost_status="partial")
    h = load_plugin("subagent-effort")
    with bind_subagent_parent(agent):
        h.hook("subagent_start", parent_session_id="p", child_session_id="child-l",
               child_goal="complex-marker do work")
        h.hook("post_llm_call", session_id="child-l")
        h.hook("subagent_stop", parent_session_id="p", child_session_id="child-l",
               child_status="completed", duration_ms=999, tool_call_history=history)
    row = json.loads(h.command("subagent_effort"))["children"][0]
    # the list-valued web_extract targets cannot kill the outcome record
    assert row["outcome_observed"] is True
    assert row["outcome_status"] == "completed"
    assert row["usage"]["cost_usd"] == 0.25
    assert row["usage"]["cost_status"] == "partial"  # native, not actual billing
    # only actual confirmed mutation on a stable target counts
    assert row["repeated_mutation_targets"] == ["/x"]


def test_usage_captured_alongside_cost(load_plugin, classifier):
    """post_api_request usage feeds the native token buckets; cost rides the
    same usage dict — observed usage + captured cost, both real producers.
    The payload is built through the REAL hook normalizer from a
    provider-shaped response — native CanonicalUsage buckets, never a
    fabricated dict — and always carries explicit totals; assert stored
    verbatim."""
    usage_payload = _native_usage(
        SimpleNamespace(prompt_tokens=100, completion_tokens=50,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=20)),
        provider="openai", api_mode="chat_completions")
    assert "completion_tokens" not in usage_payload  # canonical buckets only
    assert usage_payload["prompt_tokens"] + usage_payload["output_tokens"] == usage_payload["total_tokens"]
    agent = SimpleNamespace(
        session_id="child-u", reasoning_config=None, model="m", provider="openai",
        session_estimated_cost_usd=0.5, session_cost_status="estimated")
    h = load_plugin("subagent-effort")
    with bind_subagent_parent(agent):
        h.hook("subagent_start", parent_session_id="p", child_session_id="child-u",
               child_goal="complex-marker do work")
        h.hook("post_api_request", session_id="child-u", usage=usage_payload)
        h.hook("post_llm_call", session_id="child-u")
        h.hook("subagent_stop", parent_session_id="p", child_session_id="child-u",
               child_status="completed", duration_ms=500, tool_call_history=[])
    row = json.loads(h.command("subagent_effort"))["children"][-1]
    assert row["usage"]["total_tokens"] == usage_payload["total_tokens"]
    assert row["usage"]["prompt_tokens"] == usage_payload["prompt_tokens"]
    assert row["usage"]["requests"] == 1
    assert row["usage"]["cost_usd"] == 0.5


def test_concurrent_lifecycle_retains_durable_rows(load_plugin, classifier):
    """Deterministic (Event-gated, no timing) interleaving of parent-side
    outcome finalization and child-side row registration: the durable row
    map must contain BOTH rows at the end. The old snapshot-outside-the-lock
    code let an older snapshot overwrite a newer row map here — child-y
    would vanish from ctx.state while staying in memory."""
    import threading
    h = load_plugin("subagent-effort")
    row_x_registered = threading.Event()
    row_y_registered = threading.Event()

    def finalize_x():
        h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
               child_goal="complex-marker x")
        row_x_registered.set()
        h.hook("subagent_stop", parent_session_id="p", child_session_id="child-x",
               child_status="completed", duration_ms=5, tool_call_history=[])
        row_y_registered.wait()  # hold x's finalizer until y's row is committed

    def register_y():
        h.hook("subagent_start", parent_session_id="p", child_session_id="child-y",
               child_goal="complex-marker y")
        row_y_registered.set()

    tx = threading.Thread(target=finalize_x)
    ty = threading.Thread(target=register_y)
    tx.start()
    row_x_registered.wait()  # strict ordering: y starts only after x's row exists
    ty.start()
    tx.join(timeout=10)
    ty.join(timeout=10)
    assert not tx.is_alive() and not ty.is_alive()
    # async classification: settle both verdicts before row assertions
    _settle_effort(h)

    by_id = {r["child_session_id"]: r
             for r in json.loads(h.command("subagent_effort"))["children"]}
    assert by_id["child-x"]["outcome_observed"] is True
    assert by_id["child-y"]["verdict"] == "complex"
    # durable rows: read through the plugin's own command channel — no
    # reimplementation of PluginState/ctx.state accessors in the test
    persisted = json.loads(h.command("subagent_effort"))
    assert persisted["mode"] == "shadow"
    by_persisted = {r["child_session_id"]: r for r in persisted["children"]}
    assert {"child-x", "child-y"} <= set(by_persisted)
    assert by_persisted["child-x"]["outcome_observed"] is True
    assert by_persisted["child-y"]["verdict"] == "complex"


def test_report_lock_is_held_during_summary_iteration(load_plugin, classifier):
    """Deterministic mid-iteration race discriminator for the report lock.
    A sys.settrace hook (module-scoped, installed only around the report
    call, cleared in finally) fires the instant the REAL registered summary
    handler reads its first row; the writer thread is launched at that
    instant and runs the REAL child-registration hook.
    - Fixed code: the handler holds the shared _lock over the iteration,
      the writer cannot add its row; the report stays a coherent snapshot.
    - Old unlocked code: the probe acquires the free lock, the writer
      mutates verdicts mid-iteration, and the next dict iteration raises
      RuntimeError — the test fails deterministically.
    The closure probe is scheduling/fault-injection only, never asserted
    against. Events, no sleeps."""
    import sys
    import threading
    h = load_plugin("subagent-effort")
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
           child_goal="x-goal complex-marker work")

    # Use the registration hook's shared lock so the scheduling seam also
    # exists when the report's locking is deliberately removed.
    from inspect import getclosurevars
    from hermes_cli.plugins import get_plugin_command_handler
    register_child_hook = next(
        callback for callback in h.manager._hooks["subagent_start"]
        if callback.__module__ == h.module.__name__)
    plugin_lock = getclosurevars(register_child_hook).nonlocals["_lock"]
    report_code = get_plugin_command_handler("subagent_effort").__code__

    writer_done = threading.Event()
    writer_fired = []  # [True] once the writer has been launched

    def register_child():
        h.hook("subagent_start", parent_session_id="p", child_session_id="child-y",
               child_goal="y-goal complex-marker work")
        writer_done.set()

    tw = threading.Thread(target=register_child, name="race-writer")

    def trace(frame, event, _arg):
        if frame.f_code is not report_code:
            return None
        if event == "line" and "sid" in frame.f_locals and not writer_fired:
            writer_fired.append(True)
            tw.start()
            if plugin_lock.acquire(blocking=False):
                # NOT held => unlocked (old) handler: let the writer mutate
                # verdicts mid-iteration; the next iteration raises RuntimeError
                plugin_lock.release()
                assert writer_done.wait(10)
            # held (fixed code): writer stays blocked until collection ends
        return trace

    try:
        sys.settrace(trace)
        mid = json.loads(h.command("subagent_effort"))
    finally:
        sys.settrace(None)
        if writer_fired:
            tw.join(timeout=10)
            assert not tw.is_alive()

    # behavioral assertions — no source-text checks:
    # (1) report is a coherent pre-race snapshot
    assert {r["child_session_id"] for r in mid["children"]} == {"child-x"}
    # (2) the new child lands in the next report
    _settle_effort(h)
    after = json.loads(h.command("subagent_effort"))
    assert {r["child_session_id"] for r in after["children"]} == {"child-x", "child-y"}


def _settle_effort(h, timeout=10):
    """Wait for every classifier attempt started so far to settle. The
    plugin's jobs map is drained only after the row update (under its
    lock), so waiting for every Event guarantees all row updates applied.
    Waits live in the TEST, outside any hook — the plugin itself never
    blocks the launch/stop path on classification."""
    seen = set()

    def walk(obj):
        if id(obj) in seen or not callable(obj):
            return {}
        seen.add(id(obj))
        merged = {}
        try:
            captured = dict(getclosurevars(obj).nonlocals)
        except (TypeError, ValueError):
            return {}
        for value in captured.values():
            merged.update(walk(value))
        merged.update(captured)
        return merged

    merged = {}
    for callbacks in h.manager._hooks.values():
        for callback in callbacks:
            if callback.__module__ == h.module.__name__:
                merged.update(walk(callback))
    for job in dict(merged["jobs"]).values():
        assert job.wait(timeout), "classifier worker did not settle"


def test_classification_stays_honest_until_landed(load_plugin, classifier):
    """Calibration row: verdict recorded, but child never reported a
    lifecycle outcome — observed must stay False, evidence unavailable,
    no inference. Classification is async: unlanded rows are honestly
    pending, never fabricated verdicts."""
    h = load_plugin("subagent-effort")
    h.hook("subagent_start", parent_session_id="p", child_session_id="child-x",
           child_goal="complex-marker do work")
    row = json.loads(h.command("subagent_effort"))["children"][0]
    assert row["outcome_observed"] is False
    assert row["outcome_status"] == "unknown"
    assert row["usage"] == "unknown" and row["verification"] == "unknown"
    # classification is async and honestly bounded: pending while the
    # classifier is in flight, classified once it lands — never a label
    # from nothing
    assert row["classification_state"] in ("pending", "classified")
    _settle_effort(h)
    row = json.loads(h.command("subagent_effort"))["children"][0]
    assert row["classification_state"] == "classified"
    assert row["verdict"] == "complex" and row["suggested_effort"] == "high"


# --- provider matrix: REAL request builders on the post-switch runtime ------


def test_prewalk_handoff_feeds_real_provider_builders(load_plugin, live_agent, workspace):
    """After the native switch_model handoff, the REAL provider kwargs
    builders emit the target model / reasoning — no plugin ever rewrites
    provider kwargs anywhere (neither plugin registers llm_request
    middleware); this asserts the built request matches the chosen runtime."""
    agent = live_agent
    from tools.todo_tool import TodoStore, todo_tool
    store = TodoStore()
    agent._todo_store = store
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    h.tool("prewalk_arm", {}, session_id="s")
    with bind_subagent_parent(agent):
        # native todo tool executes a real write against the real store,
        # and the validated handoff on the genuine plan drives the switch
        todos = [{"id": "1", "content": "plan step", "status": "pending"}]
        todo_result = todo_tool(todos=todos, store=store)
        assert store.has_items()
        handed = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
        assert handed["success"] is True
        assert agent.model == TARGET

        messages = [{"role": "user", "content": "go"}]

        # (no OpenAI-wire assertion here: the kwargs below would echo values
        # already asserted above — tautology. The full CLI smoke exercises
        # the native OpenAI builder with the switched target model.)

        # anthropic wire: REAL build_anthropic_kwargs on the new reasoning
        agent.reasoning_config = {"enabled": True, "effort": "medium"}
        from agent.anthropic_adapter import build_anthropic_kwargs
        built = build_anthropic_kwargs(
            model=TARGET, messages=messages, tools=None, max_tokens=1024,
            reasoning_config=agent.reasoning_config, base_url="https://api.anthropic.com",
        )
        assert built["model"] == TARGET
        assert built["max_tokens"] > 0
        # real _thinking_kwargs mapping (model-dependent adaptive/manual shape)
        thinking = built.get("thinking")
        assert thinking and thinking["type"] in {"enabled", "adaptive"}

        # (middleware-echo assertions removed: neither plugin registers
        # llm_request middleware, so a pass-through echo + empty trace says
        # nothing about provider compatibility — the REAL build_anthropic_
        # kwargs call above carries the compatibility evidence.)


# --- real dispatcher bridge regression (handle_function_call) ---------------


def test_tool_call_batch_bridge_arms_and_switches_with_exact_ids(
    load_plugin, live_agent, workspace
):
    """Native bridge regression: tool_call({calls:[...]}) batch dispatch through
    model_tools.handle_function_call with exact IDs, so future smoke/tests
    replicate the producer contract exactly (tool_call requires a 'calls'
    array of {name, arguments}; a bare {'tool_name': ...} payload is rejected)."""
    agent = live_agent
    from model_tools import handle_function_call
    from tools.todo_tool import TodoStore, todo_tool
    agent._todo_store = TodoStore()
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    with bind_subagent_parent(agent):
        # malformed legacy shape is rejected — the contract guard itself
        bad = json.loads(handle_function_call(
            "tool_call", {"tool_name": "prewalk_arm"}, session_id="s",
            tool_call_id="call_bad", turn_id="t:1", skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
        ))
        assert bad.get("error")

        # correct shape: calls array, exact IDs preserved through the unwrap
        result = json.loads(handle_function_call(
            "tool_call", {"calls": [{"name": "prewalk_arm", "arguments": {}}]},
            session_id="s", tool_call_id="call_arm", turn_id="t:1",
            skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
        ))
        assert result["armed"] is True
        assert result["target"] == TARGET

        todo_result = todo_tool(
            todos=[{"id": "1", "content": "plan step", "status": "pending"}],
            store=agent._todo_store,
        )
        # the validated handoff on the genuine plan drives the switch
        handed = json.loads(handle_function_call(
            "tool_call", {"calls": [{"name": "prewalk_ready",
                                     "arguments": _handoff()}]},
            session_id="s", tool_call_id="call_ready", turn_id="t:1",
            skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
        ))
        assert handed["success"] is True and handed["handed_off"] is True
        assert agent.model == TARGET


def test_unbound_worker_stays_armed_never_switches(load_plugin, live_agent, workspace):
    """Negative bridge case: a tool worker WITHOUT the bound turn context
    must refuse the switch and stay armed (retry semantics), never rewrite
    anything on another agent. Bare writes never switch at all."""
    agent = live_agent
    from tools.todo_tool import TodoStore, todo_tool
    agent._todo_store = TodoStore()
    h = load_plugin("prewalk", settings={"target_model": TARGET, "todo_gate": False})
    h.hook("pre_llm_call", session_id="s", user_message="plan")
    h.tool("prewalk_arm", {}, session_id="s")
    todo_tool(
        todos=[{"id": "1", "content": "plan step", "status": "pending"}],
        store=agent._todo_store,
    )
    # deliberately NO bind_subagent_parent here — worker without turn context
    h.hook("post_tool_call", tool_name="write_file", session_id="s",
           result='{"bytes_written": 12}', status="ok",
           error_type=None, error_message=None)
    refused = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
    assert refused["success"] is False  # native switch refused: no live bound agent
    assert agent.model == "strong-planner"
    assert json.loads(h.tool("prewalk_status", {}, session_id="s"))["state"] == "armed"




def test_ready_validates_effective_task_cwd(load_plugin, live_agent, tmp_path,
                                           monkeypatch):
    """prewalk_ready validates located evidence against the EFFECTIVE task
    workspace (terminal cwd registry), not the stale launch tree: the file
    exists only in the override tree B; an unrelated namesake check against
    the launch tree A must never decide the handoff."""
    agent = live_agent
    from tools.todo_tool import TodoStore, todo_tool
    b = tmp_path / "task-tree"
    b.mkdir()
    (b / "widget.py").write_text("VALUE = 1\n")
    monkeypatch.setattr("tools.terminal_tool.get_session_cwd",
                        lambda ident: str(b) if ident == "t-override" else None)
    monkeypatch.setattr("agent.runtime_cwd.resolve_context_cwd", lambda: tmp_path)
    assert not (tmp_path / "widget.py").exists()  # launch tree has NO such file
    agent._todo_store = TodoStore()
    todo_tool(todos=[{"id": "1", "content": "plan step",
                      "status": "pending"}], store=agent._todo_store)
    h = load_plugin("prewalk", settings={"target_model": TARGET})
    h.tool("prewalk_arm", {}, session_id="s")
    with bind_subagent_parent(agent):
        # without the task identity the launch tree rules: no widget.py there
        unconfined = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s"))
        assert unconfined["success"] is False and "widget.py" in str(unconfined["problems"])
        # with the effective task identity, B's file is real evidence
        handed = json.loads(h.tool("prewalk_ready", _handoff(), session_id="s",
                                   task_id="t-override"))
        assert handed["success"] is True and handed["handed_off"] is True
        assert agent.model == TARGET
