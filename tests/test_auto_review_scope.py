import json
import os
from pathlib import Path

import pytest
from agent.subagent_lifecycle import bind_subagent_parent


def envelope(risky_probability, choice=None):
    """The raw answer envelope the service returns for the plugin's `review` question."""
    probabilities = {"risky": risky_probability, "safe": 1.0 - risky_probability}
    answer = {"type": "choice", "probabilities": probabilities,
              "choice": choice or ("risky" if risky_probability >= 0.5 else "safe")}
    answer["confidence"] = probabilities[answer["choice"]]
    return {"model": "synthetic", "usage": {}, "answers": {"review": answer}}


def gate_setup(load_plugin, monkeypatch, classifier_stub, verdict="risky", policy="ask",
               answers=None):
    """The plugin with its classifier transport replaced by the shared stub.

    The stub is installed BEFORE the plugin loads, because the plugin resolves its
    client once, at registration. ``h.captured`` holds every state that left the
    plugin, ``h.raws`` the state plus the questions asked, and ``h.transport.calls``
    the caller and timeout the plugin passed. ``answers`` scripts raw envelopes in
    order, for a test that needs a specific (or nonsense) reply.
    """
    captured, raws = [], []
    script = list(answers) if answers is not None else []

    def reply(state, questions, **_kwargs):
        captured.append(state)
        raws.append({"state": state, "questions": questions})
        if script:
            return script.pop(0)
        if verdict is None:
            return {"answers": {}}  # no answer for our question: unusable
        return envelope(0.97 if verdict == "risky" else 0.03)

    transport = classifier_stub.install(reply)
    h = load_plugin("auto-review-classifier", {"policy": policy})
    h.captured = captured
    h.raws = raws
    h.transport = transport
    return h


def local_agent():
    from run_agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    agent._interrupt_requested = False
    return agent


def workspace_for(task_id, cwd):
    from tools.terminal_tool import register_task_env_overrides
    register_task_env_overrides(task_id, {"cwd": str(cwd)})


def screen(h, agent, task_id, tool, args, tool_call_id=None):
    with bind_subagent_parent(agent):
        return h.hook("pre_tool_call", tool_name=tool, task_id=task_id,
                      tool_call_id=tool_call_id or "", args=args)


def test_secret_values_never_reach_classifier(load_plugin, monkeypatch, classifier_stub):
    h = gate_setup(load_plugin, monkeypatch, classifier_stub)
    agent = local_agent()
    secret = "super-secret-token"
    quoted = "correct horse battery staple"
    cases = ({"command": f"run --api-key {secret}"},
             {"command": f'pg_dump --password "{quoted}" db'},
             {"command": f"pg_dump --password='{quoted}' db"},
             {"command": "pg_dump --password correct\\ horse db"},
             {"command": f"run --token={secret}"},
             {"args": {"api_key": secret, "note": "safe value"}})
    for args in cases:
        before = len(h.captured)
        screen(h, agent, "task-secret", "terminal", args)
        assert len(h.captured) == before + 1, "this operation was screened"
        # Neither the whole credential nor any fragment reaches this request.
        for fragment in (secret, "super-secret", "secret-token", quoted,
                         "correct horse", "horse battery staple"):
            assert fragment not in h.captured[-1], (args, fragment)
        request = h.raws[-1]
        assert isinstance(request["state"], str)
        question = request["questions"]["review"]
        assert question["type"] == "choice"
        assert sorted(question["criteria"]) == ["risky", "safe"]
        summary = json.loads(h.captured[-1])
        assert "secret_values_redacted" in summary["unknown"]
        if "command" in args:
            command = summary["arguments"]["command"]
            assert "[redacted]" in command
            assert any(flag in command for flag in ("--api-key", "--password", "--token"))
        else:
            assert summary["arguments"]["args"]["api_key"] == "[redacted]"
            assert summary["arguments"]["args"]["note"] == "safe value"


def test_redaction_unavailable_blocks_egress(load_plugin, monkeypatch, classifier_stub):
    h = gate_setup(load_plugin, monkeypatch, classifier_stub)
    import agent.redact as native_redact
    monkeypatch.setattr(native_redact, "redact_for_egress",
                        lambda text: native_redact.REDACTION_UNAVAILABLE)
    agent = local_agent()
    result = screen(h, agent, "task-redact", "terminal", {"command": "ls"})
    assert h.captured == []  # fail closed: nothing sent
    assert result == [{"risk_review": {"status": "unknown",
                                        "reason": "context_or_redaction_unavailable",
                                        "screened": False}}]


def test_relative_symlink_retarget_uses_fresh_target_identity(load_plugin, tmp_path, monkeypatch, classifier_stub):
    h = gate_setup(load_plugin, monkeypatch, classifier_stub)
    workspace = tmp_path / "ws"
    a, b = workspace / "a.txt", workspace / "b.txt"
    workspace.mkdir()
    a.write_text("a")
    b.write_text("b")
    link = workspace / "current.txt"
    agent = local_agent()
    workspace_for("task-sym", workspace)
    os.symlink(a, link)
    screen(h, agent, "task-sym", "write_file", {"path": "current.txt", "content": "x"})
    os.remove(link)
    os.symlink(b, link)
    screen(h, agent, "task-sym", "write_file", {"path": "current.txt", "content": "x"})
    summary = json.loads(h.captured[-1])
    assert summary["targets"][0]["resolved"].endswith("b.txt")
    assert summary["targets"][0]["identity"]  # identity bound to the file the symlink now points at


def test_absolute_symlink_retarget_changes_approval_scope(load_plugin, monkeypatch, classifier_stub, tmp_path):
    h = gate_setup(load_plugin, monkeypatch, classifier_stub)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    symlink = workspace / "target.txt"
    (workspace.parent / "inside.txt").write_text("x")
    os.symlink(workspace.parent / "inside.txt", symlink)
    agent = local_agent()
    workspace_for("task-abs", workspace)
    args = {"mode": "replace", "path": str(symlink), "old_string": "x", "new_string": "y"}
    first, = screen(h, agent, "task-abs", "patch", args, tool_call_id="c1")
    os.remove(symlink)
    (workspace.parent / "elsewhere.txt").write_text("elsewhere")
    os.symlink(workspace.parent / "elsewhere.txt", symlink)
    second, = screen(h, agent, "task-abs", "patch", args, tool_call_id="c1")
    # Identical call identity: native resolves the symlink before identity
    # binding, so retargeting must still change the approval scope key.
    assert first["rule_key"] != second["rule_key"]
    assert first["rule_key"].startswith("auto-review:patch:")
    resolved = json.loads(h.captured[0])["targets"][0]
    assert resolved["resolved"].endswith("inside.txt")
    assert resolved["identity"]  # device/inode/mode/ctime bound from the resolved file
    assert json.loads(h.captured[-1])["targets"][0]["resolved"].endswith("elsewhere.txt")


def test_patch_header_targets_are_scoped(load_plugin, monkeypatch, classifier_stub, tmp_path):
    h = gate_setup(load_plugin, monkeypatch, classifier_stub)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "one.txt").write_text("1")
    agent = local_agent()
    workspace_for("task-v4a", workspace)
    patch = "*** Begin Patch\n*** Update File: one.txt\n@@\n-1\n+2\n*** End Patch"
    directive, = screen(h, agent, "task-v4a", "patch",
                        {"mode": "patch", "patch": patch}, tool_call_id="c1")
    summary = json.loads(h.captured[0])
    assert summary["targets"][0]["resolved"].endswith("one.txt")
    assert directive["rule_key"].startswith("auto-review:patch:")


def test_incomplete_safe_verdict_stays_unknown(load_plugin, monkeypatch, classifier_stub):
    h = gate_setup(load_plugin, monkeypatch, classifier_stub, verdict="safe")
    agent = local_agent()
    result = screen(h, agent, "task-safe", "terminal", {"command": "echo hi"})
    # Safe labels over incomplete context are refused; native permissions untouched.
    assert result == [{"risk_review": {"status": "unknown",
                                        "reason": "incomplete_context",
                                        "screened": False}}]


def test_risky_directive_shape_preserved_with_per_call_rule_keys(load_plugin, monkeypatch, classifier_stub, tmp_path):
    h = gate_setup(load_plugin, monkeypatch, classifier_stub, verdict="risky")
    workspace_for("task-risky", tmp_path)
    agent = local_agent()
    directive, = screen(h, agent, "task-risky", "terminal",
                        {"command": "rm -rf ./build"}, tool_call_id="call-1")
    assert directive["action"] == "approve"
    assert directive["message"]
    assert directive["rule_key"].startswith("auto-review:terminal:")
    # Incomplete (command) context: no reusable approval key across calls.
    second, = screen(h, agent, "task-risky", "terminal",
                     {"command": "rm -rf ./build"}, tool_call_id="call-2")
    assert second["rule_key"] != directive["rule_key"]


def test_approved_operation_revalidates_identity(load_plugin, monkeypatch, classifier_stub, tmp_path):
    h = gate_setup(load_plugin, monkeypatch, classifier_stub)
    workspace_for("task-reval", tmp_path)
    (tmp_path / "f.txt").write_text("a")
    agent = local_agent()
    args = {"mode": "replace", "path": str(tmp_path / "f.txt"),
            "old_string": "a", "new_string": "b"}
    directive, = screen(h, agent, "task-reval", "patch", args, tool_call_id="c1")
    assert directive["action"] == "approve"
    assert callable(directive["revalidate"])
    # Read-only closure: unchanged native identity is confirmed.
    assert directive["revalidate"](args) is True
    # Same name, new file identity (retarget): the closure vetoes.
    os.remove(tmp_path / "f.txt")
    (tmp_path / "f.txt").write_text("b")
    assert directive["revalidate"](args) is False
    # Deny directives carry no closure; closure presence is approval-scoped.
    denied = gate_setup(load_plugin, monkeypatch, classifier_stub, policy="deny")
    directive, = screen(denied, agent, "task-reval", "patch", args, tool_call_id="c1")
    assert directive["action"] == "block" and "revalidate" not in directive


def test_policy_deny_blocks_and_polling_skipped(load_plugin, monkeypatch, classifier_stub, tmp_path):
    h = gate_setup(load_plugin, monkeypatch, classifier_stub, verdict="risky", policy="deny")
    workspace_for("task-deny", tmp_path)
    agent = local_agent()
    directive, = screen(h, agent, "task-deny", "terminal",
                        {"command": "rm -rf /tmp/x"}, tool_call_id="c1")
    assert directive["action"] == "block"
    assert screen(h, agent, "task-deny", "process_manage", {"action": "poll"}) == []
    assert screen(h, agent, "task-deny", "read_file", {"path": "x"}) == []


@pytest.mark.parametrize("root", [{"risk": "weird"}, b"null"])
def test_unknown_classifier_responses_stay_fail_open(load_plugin, monkeypatch, classifier_stub, root):
    """A reply that is not an answer to our question is refused, never guessed at."""
    h = gate_setup(load_plugin, monkeypatch, classifier_stub, answers=[root])
    result = screen(h, local_agent(), "task-unknown", "terminal", {"command": "echo hi"})
    assert result == [{"risk_review": {"status": "unknown",
                                        "reason": "classifier_unavailable_or_invalid",
                                        "screened": False}}]


def test_quoted_environment_secret_is_masked_before_http(load_plugin, monkeypatch, classifier_stub):
    h = gate_setup(load_plugin, monkeypatch, classifier_stub)
    screen(h, local_agent(), "env-secret", "terminal",
           {"command": 'TOKEN="correct horse battery staple" run'})
    assert len(h.captured) == 1
    for fragment in ("correct", "horse", "battery", "staple"):
        assert fragment not in h.captured[0]
    assert "run" in json.loads(h.captured[0])["arguments"]["command"]


@pytest.mark.parametrize("command", [
    """sh -c 'run --password "correct horse battery staple"'""",
    """python -c 'request(token="correct horse battery staple")'""",
])
def test_nested_credential_syntax_abstains_before_http(load_plugin, monkeypatch, classifier_stub, command):
    h = gate_setup(load_plugin, monkeypatch, classifier_stub)
    result = screen(h, local_agent(), "nested-secret", "terminal", {"command": command})
    assert h.captured == []
    assert result[0]["risk_review"]["status"] == "unknown"
    assert result[0]["risk_review"]["screened"] is False


def test_dead_classifier_fails_open_on_every_call(load_plugin, monkeypatch, classifier_stub):
    """No answer twice means reported twice: a fail-open verdict is never cached.

    Retry policy belongs to the shared transport now. What this plugin owes the agent
    is that every screened call asks, and every call the transport declines to answer
    says so instead of quietly passing.
    """
    h = gate_setup(load_plugin, monkeypatch, classifier_stub, verdict=None)
    agent = local_agent()
    for task in ("task-dead", "task-dead-2"):
        result = screen(h, agent, task, "terminal", {"command": "echo hi"})
        assert result[0]["risk_review"]["reason"] == "classifier_unavailable_or_invalid"
    assert len(h.raws) == 2  # the second call asks again rather than replaying a verdict


def test_unusable_verdict_is_diagnosed_and_the_next_call_recovers(
        load_plugin, monkeypatch, classifier_stub, tmp_path, caplog):
    """A bad first reply is logged, not remembered: the next call is judged on its own answer.

    The plugin used to black the endpoint out on a cooldown of its own; the transport
    owns retry policy now, so the guarantee left here is diagnosability plus a fresh
    ask, never a fail-open verdict served from a stale cache.
    """
    h = gate_setup(load_plugin, monkeypatch, classifier_stub,
                   answers=[{"answers": {}}, envelope(0.03)])
    agent = local_agent()
    workspace_for("task-recover", tmp_path)
    workspace_for("task-recovered", tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("before")
    assert screen(h, agent, "task-recover", "write_file",
                  {"path": str(target), "content": "after"}) is not None
    # An unusable verdict must be diagnosable, never a silent pass.
    assert any("unusable verdict" in r.getMessage() for r in caplog.records)
    assert screen(h, agent, "task-recovered", "write_file",
                  {"path": str(target), "content": "after"}) == []
    assert len(h.raws) == 2  # both calls asked; only the second answer was believed


def test_screening_names_its_caller_and_lets_the_transport_size_the_wait(
        load_plugin, monkeypatch, classifier_stub):
    """Each request carries this plugin's identity; the wait is the transport's to compute.

    A many-field call still leaves as one large state, which is what the shared client
    sizes its timeout from: measured ~0.17 ms/char on the live engine, so >15k chars
    needs well over the 2.5 s floor this plugin used to compute for itself.
    """
    h = gate_setup(load_plugin, monkeypatch, classifier_stub)
    screen(h, local_agent(), "task-timeout-bound", "terminal", {"command": "echo hi"})
    screen(h, local_agent(), "task-timeout-big", "terminal",
           {chr(97 + i): "x" * 2000 for i in range(10)})

    calls = h.transport.calls
    assert [call["caller"] for call in calls] == ["auto-review-classifier"] * 2
    assert [call["timeout"] for call in calls] == [None, None]  # never a private budget
    small, big = (len(call["state"]) for call in calls)
    assert small < big and big > 10000  # the payload leaves whole, untruncated by us


@pytest.mark.parametrize("probability,choice,needs_review", [
    # The label alone would pass this: the head says "safe" while P(risky) is at the
    # configured boundary. Only the probability may decide.
    (0.5, "safe", True),
    # And the label alone would block this: the head says "risky" while P(risky) sits
    # below the boundary. Only the probability may decide.
    (0.4999, "risky", False),
])
def test_decision_thresholds_the_probability_not_the_label(load_plugin, monkeypatch, classifier_stub, tmp_path,
                                                           probability, choice, needs_review):
    h = gate_setup(load_plugin, monkeypatch, classifier_stub, verdict="safe",
                   answers=[envelope(probability, choice)])
    workspace_for("task-threshold", tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("a")
    result = screen(h, local_agent(), "task-threshold", "write_file",
                    {"path": str(target), "content": "b"}, tool_call_id="c1")
    if not needs_review:
        assert result == []  # below the threshold: a clean pass, never a block
        return
    directive, = result
    assert directive["action"] == "approve"
    # The decision row carries what the threshold was compared against, so the
    # threshold can be recalibrated against observed probabilities later.
    row = directive["risk_review"]
    assert row["probability"] == probability
    assert row["threshold"] == 0.5 == h.module._RISKY_THRESHOLD
