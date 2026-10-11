"""Native-seam coverage for the rules and keyword-steering plugins.

All assertions run through real Hermes machinery: the fixture's native
PluginManager (``harness.hook`` = ``invoke_hook``,
``harness.middleware`` = native middleware runner,
``harness.manager.render_system_prompt_sections``). No fake plugin contexts;
tool args and executor execution are asserted explicitly.
"""

from __future__ import annotations

import json

from pathlib import Path
import pytest


def _meta(cwd, session_id="sess-1", profile="main"):
    """session_info in the core's real shape (agent/system_prompt._plugin_session_info)."""
    return {"session_id": session_id, "model": "m", "provider": "p",
            "platform": "cli", "profile_name": profile, "cwd": str(cwd)}


def _write_rule(path, front_matter="", body="BODY"):
    text = (f"---\n{front_matter}\n---\n{body}\n") if front_matter else f"{body}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sections(harness, session_info):
    return {item.id: item.content
            for item in harness.manager.render_system_prompt_sections(session_info)}


def _mw(harness, tool_name="terminal", args=None, session_id="sess-1", task_id="t1"):
    """Run the native tool_execution middleware; returns (result, executor_calls)."""
    calls = []

    def next_call(next_args=None):
        calls.append(dict(next_args) if isinstance(next_args, dict) else next_args)
        return "EXECUTED"

    result = harness.middleware(
        "tool_execution", tool_name=tool_name,
        args=args if args is not None else {}, next_call=next_call,
        session_id=session_id, task_id=task_id, tool_call_id="c1",
        turn_id="turn-1", api_request_id="api-1",
    )
    return result, calls


def _guidance(result):
    # Guidance rides the executed tool result: guidance is checked as text.
    assert isinstance(result, str) and result.strip()
    return result, ""


def _referenced_files(book):
    """The actual rule files the rendered rulebook points the agent at."""
    return {line.split(" — ")[0].strip("-* "): Path(line.rsplit(" [", 1)[1].rstrip("]"))
            for line in book.splitlines() if " [" in line}


def _referenced_bodies(book):
    return {name: path.read_text(encoding="utf-8")
            for name, path in _referenced_files(book).items()}


def _seeded(harness, tmp_path, session_id="sess-1"):
    harness.manager.render_system_prompt_sections(_meta(tmp_path, session_id=session_id))


def _rules_harness(load_plugin, tmp_path, monkeypatch, extra_settings=None):
    """Process cwd = tmp_path; sessions get their own cwd via _meta()."""
    monkeypatch.chdir(tmp_path)
    settings = {"rule_dirs": [str(tmp_path / "global")], "ttsr_enabled": True,
                "disabled_rules": []}
    settings.update(extra_settings or {})
    return load_plugin("rules", settings=settings)


# --- rules: discovery, precedence, lanes --------------------------------------

def test_session_cwd_drives_discovery_and_precedence(load_plugin, tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    _write_rule(global_dir / "shared.md", "description: g", body="GLOBAL-SHARED")
    _write_rule(global_dir / "legacy.md", "description: keep", body="LEGACY")
    project_dir = tmp_path / "proj" / ".cursor" / "rules"
    _write_rule(project_dir / "shared.md", "description: p", body="PROJECT-SHARED")
    _write_rule(project_dir / "always.md", "alwaysApply: true", body="ALWAYS-TEXT")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    # Session cwd differs from the process cwd: discovery follows the session.
    sections = _sections(harness, _meta(tmp_path / "proj"))
    assert sections["rules.always-apply"] == "ALWAYS-TEXT"
    book = sections["rules.rulebook"]
    bodies = _referenced_bodies(book)
    # The actual file the agent is pointed to for 'shared' carries the project body.
    assert _referenced_files(book)["shared"] == project_dir / "shared.md"
    assert "PROJECT-SHARED" in bodies["shared"]
    assert str(global_dir / "shared.md") not in book  # global override was dropped
    # Global rules survive under their own name.
    assert "LEGACY" in bodies["legacy"]


def test_ancestor_dirs_lose_to_project_dirs(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / ".cursor" / "rules" / "dup.md", "description: anc", body="ANCESTOR")
    inner = tmp_path / "proj" / "deep"
    _write_rule(inner / ".cursor" / "rules" / "dup.md", "description: deep", body="NEAREST")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    book = _sections(harness, _meta(inner))["rules.rulebook"]
    # The nearest directory's file is the actual chosen file.
    assert _referenced_files(book)["dup"] == inner / ".cursor" / "rules" / "dup.md"
    assert "NEAREST" in _referenced_bodies(book)["dup"]


def test_yaml_lists_and_comments_parse(load_plugin, tmp_path, monkeypatch):
    fm = 'description: typed\n# comment\nglobs:\n  - "*.rs"\n  - "*.toml"\n'
    _write_rule(tmp_path / "global" / "typed.md", fm, body="TYPED-BODY")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    sections = _sections(harness, _meta(tmp_path))
    assert "(globs: *.rs, *.toml)" in sections["rules.rulebook"]


def test_malformed_metadata_never_applies(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "bad.md", "alwaysApply: maybe", body="BAD-BODY")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    assert harness.manager.render_system_prompt_sections(_meta(tmp_path)) == []
    result, calls = _mw(harness, args={"command": "x"})
    assert result == "EXECUTED" and len(calls) == 1  # nothing consumed, executor ran


def test_always_apply_with_globs_is_never_unconditional(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "scoped.md",
                'alwaysApply: true\nglobs: ["*.rs"]', body="SCOPED-BODY")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    sections = _sections(harness, _meta(tmp_path))
    assert "SCOPED-BODY" not in sections.get("rules.always-apply", "")
    # Demoted to the scoped reminder lane: matches on a globbed path arg.
    result, calls = _mw(harness, tool_name="read_file", args={"path": "a.rs"})
    guidance, _ = _guidance(result)
    assert "SCOPED-BODY" in guidance
    assert calls == [{"path": "a.rs"}]  # executed with guidance appended


# --- rules: tool_execution guidance lane --------------------------------------

def test_reminder_delivers_once_with_unchanged_args(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", "tools: [terminal]", body="GUARD-BODY")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    _seeded(harness, tmp_path)
    args = {"command": "git push --force"}
    result, calls = _mw(harness, args=args)
    guidance, error = _guidance(result)
    assert "GUARD-BODY" in guidance
    assert str(tmp_path / "global" / "guard.md") in guidance  # read-full path
    # Delivered once, on the executed call; args are never modified.
    assert calls == [{"command": "git push --force"}]
    assert args == {"command": "git push --force"}
    # Retry: guidance consumed, executor runs with the SAME args.
    result2, calls2 = _mw(harness, args=args)
    assert result2 == "EXECUTED" and calls2 == [args]


def test_scopes_combine_with_and(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "notes.md",
                "tools: [terminal]\nglobs: [\"*.txt\"]", body="NOTES-BODY")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    # Both scopes satisfied: guidance delivered on the executed result.
    _seeded(harness, tmp_path)
    result, calls = _mw(harness, args={"path": "notes.txt"})
    assert "NOTES-BODY" in _guidance(result)[0] and calls == [{"path": "notes.txt"}]
    # Tool scope alone is insufficient.
    _seeded(harness, tmp_path, session_id="sess-2")
    result, calls = _mw(harness, tool_name="terminal",
                        args={"command": "ls"}, session_id="sess-2")
    assert result == "EXECUTED" and len(calls) == 1
    # Glob scope alone is insufficient.
    _seeded(harness, tmp_path, session_id="sess-3")
    result, calls = _mw(harness, tool_name="read_file",
                        args={"path": "notes.txt"}, session_id="sess-3")
    assert result == "EXECUTED" and len(calls) == 1


def test_glob_rule_matches_relative_project_path(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "src.md", 'globs: ["src/*.py"]', body="SRC-RULE")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    # Path argument resolves through the core task-root anchoring; the
    # session-relative candidate matches the project path glob.
    _seeded(harness, tmp_path)
    result, calls = _mw(harness, tool_name="read_file", args={"path": "src/a.py"})
    assert "SRC-RULE" in _guidance(result)[0] and len(calls) == 1
    # Already delivered once: the retry executes with the same args.
    result, calls = _mw(harness, tool_name="read_file", args={"path": "src/a.py"})
    assert result == "EXECUTED" and calls == [{"path": "src/a.py"}]
    # Foreign workspace with the same relative layout: multi-segment globs
    # are scoped to the session project root, never absolute suffix-matched.
    foreign = tmp_path / "other" / "src" / "b.py"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("x")
    _seeded(harness, tmp_path / "other", session_id="sess-2")
    result, calls = _mw(harness, tool_name="read_file",
                        args={"path": str(foreign)}, session_id="sess-2")
    # sess-2's cwd is .../other, so the path is src/b.py inside its own
    # project: per-project scope applies there too (not a foreign suffix match).
    assert "SRC-RULE" in _guidance(result)[0] and len(calls) == 1
    # True foreign read: session project has no src/ prefix for this path.
    _seeded(harness, tmp_path / "proj_c", session_id="sess-3")
    (tmp_path / "proj_c").mkdir()
    result, calls = _mw(harness, tool_name="read_file",
                        args={"path": str(foreign)}, session_id="sess-3")
    assert result == "EXECUTED" and len(calls) == 1


def test_unknown_workspace_does_not_match_foreign_path(load_plugin, tmp_path, monkeypatch):
    from agent.runtime_cwd import _SESSION_CWD, set_session_cwd
    from tools.terminal_scope import TerminalPolicyRefusal, set_terminal_scope, reset_terminal_scope

    _write_rule(tmp_path / "global" / "src.md", 'globs: ["src/*.py"]', body="SRC-RULE")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    cwd_token = set_session_cwd("")
    policy_token = set_terminal_scope(TerminalPolicyRefusal("profile policy unavailable"))
    try:
        result, calls = _mw(harness, tool_name="read_file",
                            args={"path": "/foreign/project/src/a.py"}, session_id="unseeded")
        assert result == "EXECUTED" and calls == [{"path": "/foreign/project/src/a.py"}]
    finally:
        reset_terminal_scope(policy_token)
        _SESSION_CWD.reset(cwd_token)


def test_native_multifile_patch_gets_guidance_with_write(load_plugin, tmp_path, monkeypatch):
    from tools.file_tools import patch_tool

    _write_rule(tmp_path / "global" / "src.md",
                'tools: [patch]\nglobs: ["src/*.py"]', body="Review Python changes before applying.")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    _seeded(harness, tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    args = {"mode": "patch", "patch": (
        "*** Begin Patch\n*** Add File: docs/note.txt\n+hello\n"
        "*** Add File: src/widget.py\n+ANSWER = 42\n*** End Patch"
    )}
    original = json.dumps(args)

    def apply():
        return harness.middleware(
            "tool_execution", tool_name="patch", args=args,
            next_call=lambda values: patch_tool(**values, task_id="t1"),
            session_id="sess-1", task_id="t1",
        )

    result = apply()
    assert "Review Python changes" in _guidance(result)[0]  # delivered once
    assert (tmp_path / "src" / "widget.py").exists()
    assert (tmp_path / "docs" / "note.txt").exists()
    import runpy
    assert runpy.run_path(str(tmp_path / "src" / "widget.py"))["ANSWER"] == 42
    assert (tmp_path / "docs" / "note.txt").read_text().splitlines() == ["hello"]
    assert json.dumps(args) == original


def test_glob_extension_basename_match(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "rust.md", 'globs: ["*.rs"]', body="RUST-RULE")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    _seeded(harness, tmp_path)
    result, calls = _mw(harness, tool_name="read_file", args={"path": "src/a.rs"})
    guidance, _ = _guidance(result)
    assert "RUST-RULE" in guidance
    assert str(tmp_path / "global" / "rust.md") in guidance  # full text + file
    assert len(calls) == 1  # guidance rides the executed result
    # Extension mismatch: executor runs.
    _seeded(harness, tmp_path, session_id="sess-2")
    result, calls = _mw(harness, tool_name="read_file",
                        args={"path": "src/a.py"}, session_id="sess-2")
    assert result == "EXECUTED" and len(calls) == 1


def test_two_matching_rules_deliver_deterministically(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "bravo.md", "tools: [terminal]", body="BRAVO")
    _write_rule(tmp_path / "global" / "alpha.md", "tools: [terminal]", body="ALPHA")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    _seeded(harness, tmp_path)
    result, calls = _mw(harness, args={"command": "ls"})
    guidance, _ = _guidance(result)
    assert guidance.index("ALPHA") < guidance.index("BRAVO")  # sorted-by-name
    assert len(calls) == 1  # delivered with the executed result
    result, calls = _mw(harness, args={"command": "ls"})
    assert result == "EXECUTED" and len(calls) == 1


def test_agents_scoped_rule_isolated_per_agent(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "sec.md",
                "description: Security\nagents: [auditor]", body="SEC-BODY")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    # Agent-named rules stay inactive for non-matching agents...
    assert harness.manager.render_system_prompt_sections(_meta(tmp_path)) == []
    # ...and are discoverable for a matching agent.
    sections = _sections(harness, _meta(tmp_path, session_id="sess-a", profile="auditor"))
    assert "Security" in sections["rules.rulebook"]


def test_activation_cleanup_on_session_reset(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", "tools: [terminal]", body="GUARD-BODY")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    _seeded(harness, tmp_path)
    result, calls = _mw(harness, args={"command": "ls"})
    assert "GUARD-BODY" in _guidance(result)[0]
    _mw(harness, args={"command": "ls"})  # consumed
    harness.hook("on_session_reset", session_id="sess-1",
                 reason="new_session", platform="cli")
    _seeded(harness, tmp_path)  # fresh activation
    result, calls = _mw(harness, args={"command": "ls"})
    assert "GUARD-BODY" in _guidance(result)[0] and len(calls) == 1


def test_lazy_seed_delivers_without_render(load_plugin, tmp_path, monkeypatch):
    # Frozen-prompt/resumed sessions skip section render: the middleware seeds
    # identity itself from the real bound runtime (no reminder loss).
    _write_rule(tmp_path / "global" / "guard.md", "tools: [terminal]", body="GUARD-BODY")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    monkeypatch.setattr(harness.module, "_runtime_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(harness.module, "_profile_name", lambda: "main")
    result, calls = _mw(harness, args={"command": "ls"}, session_id="resumed-1")
    assert "GUARD-BODY" in _guidance(result)[0] and len(calls) == 1


def test_empty_session_identity_executes_without_guidance(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", "tools: [terminal]", body="GUARD-BODY")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    _seeded(harness, tmp_path)
    result, calls = _mw(harness, args={"command": "ls"}, session_id="")
    assert result == "EXECUTED" and len(calls) == 1  # unknown identity: no guidance


def test_guidance_delivered_with_executed_call_pretool_untouched(load_plugin, tmp_path, monkeypatch):
    # Verified pipeline order (agent/tool_executor.py: tool_execution
    # middleware wraps the hermes pipeline; _pre_tool_block — the plugin
    # pre-tool veto — runs strictly BELOW it, inside _dispatch_authorized_once).
    # Guidance short-circuits in the middleware: pre-tool policy is never
    # reached, so no veto can consume pending guidance, and the retry meets
    # pre-tool policy on its way to the executor.
    _write_rule(tmp_path / "global" / "guard.md", "tools: [terminal]", body="GUARD-BODY")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    _seeded(harness, tmp_path)
    # This plugin contributes no directive in the pre-tool phase at all.
    veto = harness.hook("pre_tool_call", tool_name="terminal", args={"command": "ls"},
                        session_id="sess-1", task_id="t1", tool_call_id="c1",
                        turn_id="turn-1", api_request_id="api-1")
    assert veto == []
    result, calls = _mw(harness, args={"command": "ls"})
    assert "GUARD-BODY" in _guidance(result)[0] and len(calls) == 1
    # Same call: guidance consumed, the executor (and the pre-tool policy
    # below it in the real pipeline order) ran with unchanged args.
    result, calls = _mw(harness, args={"command": "ls"})
    assert result == "EXECUTED" and len(calls) == 1


def test_rulebook_budget_omits_complete_rules_and_lists_remainder(
        load_plugin, tmp_path, monkeypatch):
    for n in "1234":
        _write_rule(tmp_path / "global" / f"d{n}.md", f"description: {'d' * 900}_{n}")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch)
    rulebook = _sections(harness, _meta(tmp_path))["rules.rulebook"]
    assert "- **d3**" in rulebook  # included rules are complete
    assert "- d4 — over the rulebook budget" in rulebook  # remainder discoverable


def test_reminders_disabled_via_config(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", "tools: [terminal]", body="GUARD-BODY")
    harness = _rules_harness(load_plugin, tmp_path, monkeypatch,
                             extra_settings={"ttsr_enabled": False})
    _seeded(harness, tmp_path)
    result, calls = _mw(harness, args={"command": "ls"})
    assert result == "EXECUTED" and len(calls) == 1


# --- keyword-steering: prose detection + per-turn delivery --------------------

@pytest.fixture()
def steering(load_plugin):
    return load_plugin("keyword-steering", settings={
        "modes": {"deploy": {"refs": ["rule:reviewed-deploy-pipeline"]}},
    })


def _turn(harness, message, session_id="sess-1", turn_id="turn-1"):
    return harness.hook(
        "pre_llm_call", session_id=session_id, user_message=message,
        conversation_history=[], is_first_turn=False, model="m", platform="cli",
        turn_id=turn_id,
    )


def test_fenced_code_is_ignored(steering):
    assert _turn(steering, "```\nplease deploy the fix\n```") == []


def test_inline_code_is_ignored(steering):
    assert _turn(steering, "run `deploy --prod` carefully") == []


def test_quoted_text_is_ignored(steering):
    assert _turn(steering, 'the log said "deploy approved" only') == []


def test_path_and_identifier_contexts_ignored(steering):
    assert _turn(steering, "check src/deploy/hooks.py and deploy_timeout") == []


def test_steering_once_per_user_turn(steering):
    first = _turn(steering, "please deploy this")
    assert first != []
    assert _turn(steering, "please deploy this") == []  # same turn deduped


def test_steering_isolated_per_session(steering):
    assert _turn(steering, "please deploy this",
                 session_id="sess-a", turn_id="turn-1") != []
    assert _turn(steering, "please deploy this",
                 session_id="sess-b", turn_id="turn-1") != []
