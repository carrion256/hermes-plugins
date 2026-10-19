import pytest

from test_steering_modes import _turn, _status, CONFLICT_MODES, REF_A


def test_refs_resolve_against_ref_roots(load_plugin, tmp_path, caplog):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "SKILL.md").write_text("skill x")
    (tmp_path / "pnav.md").write_text("rule pnav")
    with caplog.at_level("WARNING"):
        h = load_plugin("keyword-steering", settings={
            "ref_roots": [str(tmp_path)],
            "modes": {
                "good": {"refs": ["skill://x", "rule://pnav"]},
                "review": {"refs": ["skill://missing"]},
            },
        })
    assert any("unresolved" in r.getMessage() for r in caplog.records
               if r.levelname == "WARNING")
    assert _status(h)["unresolved_refs"] == {"review": ["skill://missing"]}

    review_ctx = _turn(h, "please review this")[0]["context"]
    assert "Unresolved at registration" in review_ctx
    assert "skill://missing" in review_ctx

    good_ctx = _turn(h, "this is good", turn_id="t2")[0]["context"]
    assert "skill://x" in good_ctx
    assert "Unresolved" not in good_ctx


def test_refs_module_resolution_rules(load_plugin, tmp_path):
    refs = load_plugin("keyword-steering").module.refs
    (tmp_path / "cat" / "deep").mkdir(parents=True)
    (tmp_path / "cat" / "deep" / "SKILL.md").write_text("deep")
    (tmp_path / "r.mdc").write_text("rule")

    assert refs.resolve_ref("skill://deep", [str(tmp_path)]).endswith("cat/deep/SKILL.md")
    assert refs.resolve_ref("r", [str(tmp_path)]).endswith("r.mdc")
    assert refs.resolve_ref("/definitely/missing.md", [str(tmp_path)]) is None
    assert refs.resolve_ref(str(tmp_path / "r.mdc"), []) is not None


def test_empty_turn_id_dedups_per_message(load_plugin, monkeypatch):
    from hermes_cli.plugins import PluginContext
    calls = []

    def fake_set_turn_reasoning(self, effort, reason=""):
        calls.append((effort, reason))
        return {"applied": True, "effort": effort, "source": "plugin"}

    monkeypatch.setattr(PluginContext, "set_turn_reasoning",
                        fake_set_turn_reasoning, raising=False)
    h = load_plugin("keyword-steering", settings={})

    assert _turn(h, "please ultrathink this design", turn_id="")
    assert _turn(h, "please ultrathink this design", turn_id="") == []
    assert len(calls) == 1
    assert _turn(h, "please ultrathink that design", turn_id="")
    assert len(calls) == 2


def test_conflict_guidance_asks_user_and_logs(load_plugin, caplog):
    h = load_plugin("keyword-steering", settings={"modes": CONFLICT_MODES})
    with caplog.at_level("WARNING"):
        ctx = _turn(h, "review then ship it")[0]["context"]
    assert "Tell the user" in ctx
    assert "withheld" in ctx
    assert any("conflict" in r.getMessage() for r in caplog.records
               if r.levelname == "WARNING")
    assert _status(h)["state"] == "conflict"


def test_status_without_record_has_full_shape(load_plugin):
    h = load_plugin("keyword-steering", settings={})
    s = _status(h, session_id="never-seen")
    assert s["hits"] == []
    assert s["unresolved_refs"] == {}
    assert s["classifier"]["enabled"] is False
    assert s["classifier"]["endpoint"] is None
    assert set(s) >= {
        "success", "session_id", "turn_id", "scope", "lifetime", "state",
        "requested", "active", "withheld", "conflicts", "hits", "references",
        "unresolved_refs", "magic_keywords", "magic_effects", "classifier",
    }


@pytest.mark.parametrize("bad, reason", [
    ({"workflow": {"enabled": True, "bare": "x"}}, "boolean"),
    ({"workflow": {"foo": 1}}, "unknown"),
])
def test_magic_dict_config_validation(load_plugin, bad, reason):
    with pytest.raises(AssertionError, match=reason):
        load_plugin("keyword-steering", settings={"magic_keywords": bad})
