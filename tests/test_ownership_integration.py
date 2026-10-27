import json
from types import SimpleNamespace

import pytest


def agent_for(session_id, platform="cli"):
    from run_agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = session_id
    agent._current_turn_id = "turn"
    agent.platform = platform
    agent._persist_disabled = False
    agent._interrupt_requested = False
    return agent


def classifier(monkeypatch, module, verdict):
    class Reply:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, size=-1):
            return json.dumps(verdict).encode()
    monkeypatch.setenv("AUDIT_CLASSIFIER_KEY", "synthetic-key")
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_kw: Reply())


def recommend_indentation(h, agent, tmp_path, monkeypatch, choice="spaces"):
    from agent.subagent_lifecycle import bind_subagent_parent
    from tools.terminal_tool import record_session_cwd
    record_session_cwd("task", str(tmp_path))
    style = {"spaces": "space", "tabs": "tab"}[choice]
    (tmp_path / ".editorconfig").write_text(f"root = true\n[*]\nindent_style = {style}\n")
    index = ["spaces", "tabs"].index(choice)
    classifier(monkeypatch, h.module, {"answers": {
        "pick": {"type": "choice", "choice": str(index), "confidence": 0.98,
                 "probabilities": {str(index): 0.98, str(1 - index): 0.02}},
        "sound_default": {"type": "noul", "noul": 0.99}}})
    with bind_subagent_parent(agent):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Which indentation?",
            "choices": ["spaces", "tabs"],
            "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
    assert result["status"] == "recommended" and result["selected"] == choice


@pytest.mark.parametrize("platform", ["cli", "telegram", "subagent"])
def test_partial_clarification_preserves_human_and_question_identity(load_plugin, monkeypatch, tmp_path, platform):
    from agent.subagent_lifecycle import bind_subagent_parent
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext, tool_hook_ids
    from hermes_cli.middleware import run_tool_execution_middleware
    from tools.clarify_tool import TIMEOUT_RESPONSE

    h = load_plugin("auto-answer-classifier", {"api_key_env": "AUDIT_CLASSIFIER_KEY"})
    agent = agent_for("child-or-parent", platform)
    recommend_indentation(h, agent, tmp_path, monkeypatch)
    question = {"question": "Which indentation?", "choices": ["spaces", "tabs"]}
    # Evidence would select spaces on either row, but a human chose tabs on q0.
    questions = [question, dict(question)]
    answers = iter(["tabs", TIMEOUT_RESPONSE])
    agent.clarify_callback = lambda *_a: next(answers)
    context = InlineToolContext(effective_task_id="task", tool_call_id="call-1")
    with bind_subagent_parent(agent):
        result = run_tool_execution_middleware(
            "clarify", {"questions": questions},
            lambda args: INLINE_TOOL_EXECUTORS["clarify"](agent, args, context),
            **tool_hook_ids(agent, "task", "call-1"),
        )
    result = json.loads(result)
    assert result["responses"][0]["user_response"] == "tabs"
    assert result["responses"][0]["response_status"] == "answered"
    assert result["responses"][1]["user_response"] == ""
    assert result["responses"][1]["response_status"] == "timed_out"
    assert [item["qid"] for item in result["automatic_answers"]] == ["q1"]
    decision = result["automatic_answers"][0]
    assert decision["selected"] == "spaces" and decision["applied"] is True
    assert decision["provenance"]["source"] == "repository_evidence_single_ranking"
    # The result stays in call-1; no parent CLI or asynchronous gateway queue exists.
    assert h.manager._cli_ref is None and h.manager._gateway_message_injector is None


@pytest.mark.parametrize("case", ["foreign", "detached", "interrupted", "approval", "human"])
def test_clarification_cannot_override_ownership_or_approval(load_plugin, monkeypatch, tmp_path, case):
    from agent.subagent_lifecycle import bind_subagent_parent
    from tools.clarify_tool import clarify_tool, TIMEOUT_RESPONSE

    h = load_plugin("auto-answer-classifier", {"api_key_env": "AUDIT_CLASSIFIER_KEY"})
    agent = agent_for("owner")
    recommend_indentation(h, agent, tmp_path, monkeypatch)
    agent._persist_disabled = case == "detached"
    agent._interrupt_requested = case == "interrupted"
    question = "Approve production deployment?" if case == "approval" else "Which indentation?"
    args = {"question": question, "choices": ["spaces", "tabs"]}
    raw = clarify_tool(**args, callback=lambda *_a: "tabs" if case == "human" else TIMEOUT_RESPONSE)
    with bind_subagent_parent(agent):
        output = h.middleware("tool_execution", tool_name="clarify", args=args,
                              session_id="foreign" if case == "foreign" else "owner",
                              task_id="task", next_call=lambda _args: raw)
    assert output == raw


@pytest.mark.parametrize("field,value", [
    ("_interrupt_requested", True), ("_persist_disabled", True),
    ("session_id", "new-session"), ("_current_turn_id", "new-turn"),
])
@pytest.mark.parametrize("stage", ["clarification", "classification"])
def test_clarification_rechecks_owner_after_blocking_calls(load_plugin, monkeypatch, tmp_path, field, value, stage):
    from agent.subagent_lifecycle import bind_subagent_parent
    from tools.clarify_tool import clarify_tool, TIMEOUT_RESPONSE

    h = load_plugin("auto-answer-classifier", {"api_key_env": "AUDIT_CLASSIFIER_KEY"})
    owner = agent_for("owner")
    owner._current_turn_id = "original-turn"
    recommend_indentation(h, owner, tmp_path, monkeypatch)
    args = {"question": "Which indentation?", "choices": ["spaces", "tabs"]}
    raw = clarify_tool(**args, callback=lambda *_a: TIMEOUT_RESPONSE)
    urlopen = h.module.urllib.request.urlopen

    def classify_after_change(*a, **kw):
        setattr(owner, field, value)
        return urlopen(*a, **kw)

    def clarify_after_change(_args):
        if stage == "clarification":
            setattr(owner, field, value)
        return raw

    if stage == "classification":
        monkeypatch.setattr(h.module.urllib.request, "urlopen", classify_after_change)
        with bind_subagent_parent(owner):
            rejected = json.loads(h.tool("clarification_defaults", {
                "action": "recommend", **args,
                "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
        assert rejected["status"] == "abstained"
    with bind_subagent_parent(owner):
        assert h.middleware("tool_execution", tool_name="clarify", args=args,
                            session_id="owner", turn_id="original-turn", task_id="task",
                            next_call=clarify_after_change) == raw
    assert getattr(owner, field) == value  # blocking boundary really ran


def test_ranked_dry_run_defaults_never_replace_human_response(load_plugin, monkeypatch, tmp_path):
    from agent.subagent_lifecycle import bind_subagent_parent
    from tools.clarify_tool import clarify_tool, TIMEOUT_RESPONSE

    h = load_plugin("auto-answer-classifier", {"api_key_env": "AUDIT_CLASSIFIER_KEY", "dry_run": True})
    args = {"question": "Which indentation?", "choices": ["spaces", "tabs"]}
    raw = clarify_tool(**args, callback=lambda *_a: TIMEOUT_RESPONSE)
    agent = agent_for("owner")
    for choice in ("spaces", "tabs"):
        recommend_indentation(h, agent, tmp_path, monkeypatch, choice)
        with bind_subagent_parent(agent):
            output = json.loads(h.middleware("tool_execution", tool_name="clarify", args=args,
                                            session_id="owner", task_id="task",
                                            next_call=lambda _a: raw))
        assert output["user_response"] == TIMEOUT_RESPONSE
        assert output["automatic_answers"][0]["selected"] == choice
        assert output["automatic_answers"][0]["applied"] is False


def test_blank_human_skip_is_not_auto_selected(load_plugin, monkeypatch, tmp_path):
    from agent.subagent_lifecycle import bind_subagent_parent
    from tools.clarify_tool import clarify_tool, TIMEOUT_RESPONSE

    h = load_plugin("auto-answer-classifier", {"api_key_env": "AUDIT_CLASSIFIER_KEY"})
    owner = agent_for("owner")
    recommend_indentation(h, owner, tmp_path, monkeypatch)
    # Both rows have qualifying evidence: only native timeout provenance,
    # not lack of evidence for the skipped row, may allow the second default.
    question = {"question": "Which indentation?", "choices": ["spaces", "tabs"]}
    questions = [question, dict(question)]
    answers = iter(["", TIMEOUT_RESPONSE])
    original = clarify_tool(question="", questions=questions, callback=lambda *_a: next(answers))
    with bind_subagent_parent(owner):
        output = json.loads(h.middleware("tool_execution", tool_name="clarify",
                                        args={"questions": questions}, task_id="task",
                                        session_id="owner", next_call=lambda _a: original))
    assert output["responses"] == json.loads(original)["responses"]
    assert output["responses"][0]["response_status"] == "skipped"
    assert output["responses"][1]["response_status"] == "timed_out"
    assert [item["qid"] for item in output["automatic_answers"]] == ["q1"]
    assert output["automatic_answers"][0]["applied"] is True
    raw = clarify_tool(question="", questions=[question], callback=lambda *_a: TIMEOUT_RESPONSE)
    with bind_subagent_parent(owner):
        output = json.loads(h.middleware("tool_execution", tool_name="clarify",
                                        args={"questions": [question]}, task_id="task",
                                        session_id="owner", next_call=lambda _a: raw))
    assert output["automatic_answers"][0]["selected"] == "spaces"
    assert output["automatic_answers"][0]["applied"] is True


def test_loop_blocks_own_call_and_never_interrupts_parent(load_plugin):
    import queue
    from agent.subagent_lifecycle import bind_subagent_parent
    from agent.inline_tool_executors import emit_terminal_post_tool_call
    from hermes_cli.plugins import get_pre_tool_call_directive

    h = load_plugin("loop-guard", {"tool_call_streak": 2})
    parent_queue = queue.Queue()
    h.manager._cli_ref = SimpleNamespace(_agent_running=True, _interrupt_queue=parent_queue)
    child = agent_for("child", "subagent")
    args = {"path": "status.txt"}
    with bind_subagent_parent(child):
        for _ in range(2):
            emit_terminal_post_tool_call(
                child, function_name="read_file", function_args=args,
                result='{"content":"unchanged"}', effective_task_id="task", tool_call_id="call",
            )
        action, message = get_pre_tool_call_directive("read_file", args, session_id="child")
    assert action == "block"
    assert parent_queue.empty()
    parent = agent_for("parent")
    with bind_subagent_parent(parent):
        assert get_pre_tool_call_directive("read_file", args, session_id="parent") == (None, None)


def test_progress_polling_final_answer_and_zero_cap_break_loop(load_plugin):
    from agent.subagent_lifecycle import bind_subagent_parent
    from hermes_cli.plugins import get_pre_tool_call_directive

    h = load_plugin("loop-guard", {"tool_call_streak": 2})
    agent = agent_for("owner")
    args = {"path": "status.txt"}
    with bind_subagent_parent(agent):
        for content in ["one", "two"]:
            h.hook("post_tool_call", tool_name="read_file", args=args, result=content, session_id="owner")
        assert get_pre_tool_call_directive("read_file", args, session_id="owner") == (None, None)
        h.hook("post_llm_call", assistant_response="Final answer", session_id="owner")
        h.hook("post_tool_call", tool_name="read_file", args=args, result="two", session_id="owner")
        assert get_pre_tool_call_directive("read_file", args, session_id="owner") == (None, None)
        for _ in range(4):
            h.hook("post_tool_call", tool_name="process_manage", args={"action":"poll"}, result="waiting", session_id="owner")
        assert get_pre_tool_call_directive("process_manage", {"action":"poll"}, session_id="owner") == (None, None)
    h = load_plugin("loop-guard", {"tool_call_streak": 2, "redirect_cap": 0})
    with bind_subagent_parent(agent):
        for _ in range(4):
            h.hook("post_tool_call", tool_name="read_file", args=args, result="two", session_id="owner")
        assert get_pre_tool_call_directive("read_file", args, session_id="owner") == (None, None)


@pytest.mark.parametrize("policy,expected", [("ask", "approve"), ("deny", "block")])
def test_process_mutations_use_native_risk_directives(load_plugin, monkeypatch, policy, expected):
    from hermes_cli.plugins import get_pre_tool_call_directive

    h = load_plugin("auto-review-classifier", {"policy": policy, "api_key_env": "AUDIT_CLASSIFIER_KEY"})
    classifier(monkeypatch, h.module, {"answers": {"review": {
        "type": "choice", "choice": "risky", "confidence": 0.97,
        "probabilities": {"risky": 0.97, "safe": 0.03}}}})
    for action in ["submit", "write", "kill", "close"]:
        directive, message = get_pre_tool_call_directive("process_manage", {"action":action}, session_id="owner")
        assert directive == expected and "human review" in message
    assert get_pre_tool_call_directive("process_manage", {"action":"poll"}, session_id="owner") == (None, None)
