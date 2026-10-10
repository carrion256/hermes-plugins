"""Regression coverage for the rules plugin's rules_explain consumer tool.

The explain lane is read-only planner guidance: stable ids/sources, AND-scope
reasons, precedence/shadowing with both sources, declared-constraint conflicts,
budget omissions, and data-only check/verification refs.
"""

from __future__ import annotations

import json


def _meta(cwd, session_id="sess-1", profile="main"):
    return {"session_id": session_id, "model": "m", "provider": "p",
            "platform": "cli", "profile_name": profile, "cwd": str(cwd)}


def _write_rule(path, front_matter="", body="BODY"):
    text = (f"---\n{front_matter}\n---\n{body}\n") if front_matter else f"{body}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _harness(load_plugin, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return load_plugin("rules", settings={
        "rule_dirs": [str(tmp_path / "global")],
        "ttsr_enabled": True, "disabled_rules": [],
        "rules_explain_enabled": True})


def _seed(harness, cwd, session_id="sess-1"):
    harness.manager.render_system_prompt_sections(_meta(cwd, session_id=session_id))


def _explain(harness, args, session_id="sess-1", task_id="t1"):
    return json.loads(harness.tool("rules_explain", args,
                                   session_id=session_id, task_id=task_id))


# --- consumer behavior -------------------------------------------------

def test_explain_tool_registered(load_plugin, tmp_path, monkeypatch):
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)
    result = _explain(h, {"paths": ["src/a.py"], "tool_name": "patch"})
    assert result["success"] is True
    assert result["cwd"] == str(tmp_path)


def test_explain_matches_and_scopes_deterministically(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "notes.md",
                'tools: [terminal]\nglobs: ["*.txt"]', body="NOTES")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)
    hit = _explain(h, {"paths": ["notes.txt"], "tool_name": "terminal"})
    assert [m["name"] for m in hit["matched"]] == ["notes"]
    reasons = " ".join(hit["matched"][0]["reasons"])
    assert "terminal" in reasons and "*.txt" in reasons
    assert hit["matched"][0]["id"].startswith("rule:")
    assert hit["matched"][0]["source"] == str(tmp_path / "global" / "notes.md")
    # AND semantics: tool-only or path-only does not match.
    assert _explain(h, {"paths": ["notes.txt"], "tool_name": "read_file"})["matched"] == []
    assert _explain(h, {"paths": ["a.py"], "tool_name": "terminal"})["matched"] == []
    # Deterministic: identical payload on repeat.
    again = json.loads(h.tool("rules_explain",
                              {"paths": ["notes.txt"], "tool_name": "terminal"},
                              session_id="sess-1", task_id="t1"))
    assert again == hit


def test_agent_context_scopes_rules(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "sec.md",
                'description: Security\ntools: [patch]\nagents: ["audit*"]', body="SEC")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)
    # Session profile 'main' does not match; explicit agent context does.
    assert _explain(h, {"paths": ["a.py"], "tool_name": "patch",
                        "agent": "main"})["matched"] == []
    on = _explain(h, {"paths": ["a.py"], "tool_name": "patch", "agent": "auditor"})
    assert [m["name"] for m in on["matched"]] == ["sec"]
    assert any("agent scope" in r for r in on["matched"][0]["reasons"])


def test_precedence_shadowing_reports_both_sources(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "style.md", "description: g", body="GLOBAL-STYLE")
    _write_rule(tmp_path / "proj" / ".cursor" / "rules" / "style.md",
                "description: p", body="PROJECT-STYLE")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path / "proj")
    result = _explain(h, {"paths": ["a.py"], "tool_name": "read_file"})
    assert len(result["shadowed"]) == 1
    entry = result["shadowed"][0]
    assert entry["rule"] == "style"
    assert entry["source"] == str(tmp_path / "global" / "style.md")
    assert entry["overridden_by"] == str(tmp_path / "proj" / ".cursor" / "rules" / "style.md")
    assert entry["identical_content"] is False  # same-key content conflict, both sources


def test_declared_constraint_conflict_with_both_sources(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "gk.md",
                "description: g\nconstraints: {style: tabs}", body="G")
    _write_rule(tmp_path / "proj" / ".cursor" / "rules" / "pk.md",
                "description: p\nconstraints: {style: spaces}", body="P")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path / "proj")
    result = _explain(h, {"paths": ["a.py"], "tool_name": "read_file"})
    conflicts = result["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["type"] == "declared_constraint" and c["constraint"] == "style"
    assert c["winner"]["source"] == str(tmp_path / "proj" / ".cursor" / "rules" / "pk.md")
    assert c["winner"]["value"] == "spaces"
    assert c["loser"]["source"] == str(tmp_path / "global" / "gk.md")
    assert c["loser"]["value"] == "tabs"


def test_agreeing_constraints_are_not_conflicts(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "gk.md",
                "description: g\nconstraints: {style: tabs}", body="G")
    _write_rule(tmp_path / "proj" / ".cursor" / "rules" / "pk.md",
                "description: p\nconstraints: {style: tabs}", body="P")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path / "proj")
    result = _explain(h, {"paths": ["a.py"], "tool_name": "read_file"})
    assert result["conflicts"] == []


def test_disjoint_scopes_do_not_conflict(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "python.md",
                'globs: ["*.py"]\nconstraints: {language: python}')
    _write_rule(tmp_path / "global" / "rust.md",
                'globs: ["*.rs"]\nconstraints: {language: rust}')
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)
    result = _explain(h, {"paths": ["a.py"], "tool_name": "patch"})
    assert [r["name"] for r in result["matched"]] == ["python"]
    assert result["conflicts"] == []
    both = _explain(h, {"paths": ["a.py", "b.rs"], "tool_name": "patch"})
    assert {r["name"] for r in both["matched"]} == {"python", "rust"}
    assert both["conflicts"][0]["constraint"] == "language"


def test_budget_omissions_visible(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "aaa.md", "alwaysApply: true", body="A" * 3500)
    _write_rule(tmp_path / "global" / "zbb.md", "alwaysApply: true", body="B" * 3500)
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)
    result = _explain(h, {"paths": ["a.py"], "tool_name": "read_file"})
    assert result["budget_omissions"]["always_apply"] == ["zbb"]
    # Applicable regardless of budget: omissions are transport, not applicability.
    assert [m["name"] for m in result["matched"]] == ["aaa", "zbb"]


def test_check_refs_are_data_only(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md",
                'tools: [patch]\ncheck_refs: ["run: make lint"]\n'
                'verification_refs: ["tests/test_guard.py"]', body="GUARD")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)
    result = _explain(h, {"paths": ["src/a.py"], "tool_name": "patch"})
    assert result["matched"][0]["check_refs"] == ["run: make lint"]
    assert result["matched"][0]["verification_refs"] == ["tests/test_guard.py"]
    ref = result["rule_refs"][0]
    assert set(ref) == {"id", "source", "description"}
    assert ref["id"] == result["matched"][0]["id"]


def test_malformed_new_metadata_never_applies(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "bad1.md", 'id: 123\ntools: [patch]', body="B1")
    _write_rule(tmp_path / "global" / "bad2.md", 'check_refs: "not-a-list"', body="B2")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)
    assert _explain(h, {"paths": ["src/a.py"], "tool_name": "patch"})["matched"] == []


def test_invalid_paths_rejected(load_plugin, tmp_path, monkeypatch):
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)
    assert _explain(h, {"paths": "src/a.py"})["success"] is False
    assert _explain(h, {"paths": [""]})["success"] is False


def test_rule_refs_in_pre_edit_guidance_envelope(load_plugin, tmp_path, monkeypatch):
    _write_rule(tmp_path / "global" / "guard.md", "tools: [terminal]", body="GUARD")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)

    def next_call(next_args=None):
        return "EXECUTED"

    result = h.middleware("tool_execution", tool_name="terminal",
                          args={"command": "ls"}, next_call=next_call,
                          session_id="sess-1", task_id="t1", tool_call_id="c1",
                          turn_id="turn-1", api_request_id="api-1")
    payload = json.loads(result)
    assert "GUARD" in payload["rule_guidance"]
    refs = payload["rule_refs"]
    assert [r["source"] for r in refs] == [str(tmp_path / "global" / "guard.md")]
    assert [set(r) for r in refs] == [{"id", "source", "description"}]
    assert refs[0]["id"].startswith("rule:")


def test_tool_scoped_rulebook_rule_matches_only_in_scope(load_plugin, tmp_path, monkeypatch):
    """A rulebook rule scoped tools=[patch] is out of scope for read_file:
    it must not appear in matched or rule_refs (discoverability stays
    separate from applicability) while the same query for patch matches."""
    _write_rule(tmp_path / "global" / "sec.md",
                "description: Security\ntools: [patch]", body="SEC")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path)
    out = _explain(h, {"paths": ["a.py"], "tool_name": "read_file"})
    assert out["matched"] == [] and out["rule_refs"] == []
    hit = _explain(h, {"paths": ["a.py"], "tool_name": "patch"})
    assert [m["name"] for m in hit["matched"]] == ["sec"]
    assert hit["rule_refs"] and hit["rule_refs"][0]["id"]


def test_same_name_conflict_survives_precedence_winner(load_plugin, tmp_path, monkeypatch):
    """Identical bodies with conflicting declared constraints are NOT
    identical guidance: the overridden source must still surface in
    conflicts even though identical_content is true."""
    _write_rule(tmp_path / "global" / "style.md",
                "description: g\nconstraints: {style: tabs}", body="SAME")
    _write_rule(tmp_path / "proj" / ".cursor" / "rules" / "style.md",
                "description: p\nconstraints: {style: spaces}", body="SAME")
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _seed(h, tmp_path / "proj")
    result = _explain(h, {"paths": ["a.py"], "tool_name": "read_file"})
    assert result["shadowed"] and result["shadowed"][0]["identical_content"] is True
    conflicts = [c for c in result["conflicts"]
                 if c["type"] == "declared_constraint" and c["constraint"] == "style"]
    assert len(conflicts) == 1
    assert conflicts[0]["winner"]["value"] == "spaces"
    assert conflicts[0]["loser"]["value"] == "tabs"
