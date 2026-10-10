import json
import os
from pathlib import Path

import pytest

from agent.subagent_lifecycle import bind_subagent_parent


class Reply:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self, size=-1):
        return json.dumps(self.body).encode()


def gate_setup(load_plugin, monkeypatch, verdict="risky", policy="ask"):
    h = load_plugin("auto-review-classifier",
                    {"policy": policy, "api_key_env": "REVIEW_TEST_KEY"})
    captured = []
    raws = []

    def fake_urlopen(request, *_a, **_kw):
        raw = json.loads(request.data)
        raws.append(raw)
        captured.append(raw["text"])
        return Reply({"risk": verdict})

    monkeypatch.setenv("REVIEW_TEST_KEY", "synthetic-key")
    monkeypatch.setattr(h.module.urllib.request, "urlopen", fake_urlopen)
    h.captured = captured
    h.raws = raws
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


def test_secret_values_never_reach_classifier(load_plugin, monkeypatch):
    h = gate_setup(load_plugin, monkeypatch)
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
        assert isinstance(request["text"], str)
        assert request["schema"]["risk"] == ["safe", "risky"]
        summary = json.loads(h.captured[-1])
        assert "secret_values_redacted" in summary["unknown"]
        if "command" in args:
            command = summary["arguments"]["command"]
            assert "[redacted]" in command
            assert any(flag in command for flag in ("--api-key", "--password", "--token"))
        else:
            assert summary["arguments"]["args"]["api_key"] == "[redacted]"
            assert summary["arguments"]["args"]["note"] == "safe value"


def test_redaction_unavailable_blocks_egress(load_plugin, monkeypatch):
    h = gate_setup(load_plugin, monkeypatch)
    import agent.redact as native_redact
    monkeypatch.setattr(native_redact, "redact_for_egress",
                        lambda text: native_redact.REDACTION_UNAVAILABLE)
    agent = local_agent()
    result = screen(h, agent, "task-redact", "terminal", {"command": "ls"})
    assert h.captured == []  # fail closed: nothing sent
    assert result == [{"risk_review": {"status": "unknown",
                                        "reason": "context_or_redaction_unavailable",
                                        "screened": False}}]


def test_relative_symlink_retarget_uses_fresh_target_identity(load_plugin, tmp_path, monkeypatch):
    h = gate_setup(load_plugin, monkeypatch)
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


def test_absolute_symlink_retarget_changes_approval_scope(load_plugin, monkeypatch, tmp_path):
    h = gate_setup(load_plugin, monkeypatch)
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


def test_patch_header_targets_are_scoped(load_plugin, monkeypatch, tmp_path):
    h = gate_setup(load_plugin, monkeypatch)
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


def test_incomplete_safe_verdict_stays_unknown(load_plugin, monkeypatch):
    h = gate_setup(load_plugin, monkeypatch, verdict="safe")
    agent = local_agent()
    result = screen(h, agent, "task-safe", "terminal", {"command": "echo hi"})
    # Safe labels over incomplete context are refused; native permissions untouched.
    assert result == [{"risk_review": {"status": "unknown",
                                        "reason": "incomplete_context",
                                        "screened": False}}]


def test_risky_directive_shape_preserved_with_per_call_rule_keys(load_plugin, monkeypatch, tmp_path):
    h = gate_setup(load_plugin, monkeypatch, verdict="risky")
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


def test_approved_operation_revalidates_identity(load_plugin, monkeypatch, tmp_path):
    h = gate_setup(load_plugin, monkeypatch)
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
    denied = gate_setup(load_plugin, monkeypatch, policy="deny")
    directive, = screen(denied, agent, "task-reval", "patch", args, tool_call_id="c1")
    assert directive["action"] == "block" and "revalidate" not in directive


def test_policy_deny_blocks_and_polling_skipped(load_plugin, monkeypatch, tmp_path):
    h = gate_setup(load_plugin, monkeypatch, verdict="risky", policy="deny")
    workspace_for("task-deny", tmp_path)
    agent = local_agent()
    directive, = screen(h, agent, "task-deny", "terminal",
                        {"command": "rm -rf /tmp/x"}, tool_call_id="c1")
    assert directive["action"] == "block"
    assert screen(h, agent, "task-deny", "process_manage", {"action": "poll"}) == []
    assert screen(h, agent, "task-deny", "read_file", {"path": "x"}) == []


@pytest.mark.parametrize("root", [{"risk": "weird"}, b"null"])
def test_unknown_classifier_responses_stay_fail_open(load_plugin, monkeypatch, root):
    h = gate_setup(load_plugin, monkeypatch, verdict=None)

    class RawReply(Reply):
        def __init__(self):
            pass

        def read(self, size=-1):
            return root if isinstance(root, bytes) else json.dumps(root).encode()

    monkeypatch.setattr(h.module.urllib.request, "urlopen",
                        lambda *_a, **_kw: RawReply())
    agent = local_agent()
    result = screen(h, agent, "task-unknown", "terminal", {"command": "echo hi"})
    assert result == [{"risk_review": {"status": "unknown",
                                        "reason": "classifier_unavailable_or_invalid",
                                        "screened": False}}]


def test_quoted_environment_secret_is_masked_before_http(load_plugin, monkeypatch):
    h = gate_setup(load_plugin, monkeypatch)
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
def test_nested_credential_syntax_abstains_before_http(load_plugin, monkeypatch, command):
    h = gate_setup(load_plugin, monkeypatch)
    result = screen(h, local_agent(), "nested-secret", "terminal", {"command": command})
    assert h.captured == []
    assert result[0]["risk_review"]["status"] == "unknown"
    assert result[0]["risk_review"]["screened"] is False
