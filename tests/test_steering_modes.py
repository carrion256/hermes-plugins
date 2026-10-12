"""Native-seam regression coverage for keyword-steering's explicit modes.

All assertions run through the real Hermes loader/dispatcher (conftest
fixtures); no test reimplements the plugin SDK.
"""

import json

import pytest

REF_A = "skill://review-mode"
REF_B = "/home/common/.omp/rules/pnav-boundary-parse.md"


@pytest.fixture()
def steering(load_plugin):
    return load_plugin("keyword-steering", settings={
        "modes": {"review": {"refs": [REF_A]}},
    })


def _turn(harness, message, session_id="sess-1", turn_id="turn-1"):
    return harness.hook(
        "pre_llm_call", session_id=session_id, user_message=message,
        conversation_history=[], is_first_turn=False, model="m", platform="cli",
        turn_id=turn_id,
    )


def _status(harness, session_id="sess-1"):
    return json.loads(harness.tool("steering_status", {}, session_id=session_id))


# --- activation and reference pointers -------------------------------------

def test_mode_activates_with_reference_pointer(steering):
    results = _turn(steering, "please review this diff")
    assert len(results) == 1 and "context" in results[0]
    assert "Steering mode `review`" in results[0]["context"]
    assert REF_A in results[0]["context"]


def test_custom_triggers_are_configurable(load_plugin):
    harness = load_plugin("keyword-steering", settings={"modes": {
        "strict": {"refs": [REF_A], "triggers": ["extra strict"]},
    }})
    assert _turn(harness, "use extra strict checks") != []
    # The mode name itself is no longer an activation trigger.
    assert _turn(harness, "be strict please", turn_id="turn-2") == []


# --- prose exclusions (preserved from the keyword behavior) -----------------

def test_fenced_code_is_ignored(steering):
    assert _turn(steering, "```\nplease review the fix\n```") == []


def test_inline_code_is_ignored(steering):
    assert _turn(steering, "run `review --strict` carefully") == []


def test_quoted_text_is_ignored(steering):
    assert _turn(steering, 'the log said "review approved" only') == []


def test_path_and_identifier_contexts_ignored(steering):
    assert _turn(steering, "check src/review/hooks.py and review_timeout") == []


# --- lifetime and scope -----------------------------------------------------

def test_guidance_once_per_turn_and_replaced_next_turn(steering):
    assert _turn(steering, "please review this") != []
    assert _turn(steering, "please review this") == []  # same turn deduped
    # Turn-local lifetime: the next turn re-evaluates from its own message.
    assert _turn(steering, "carry on", turn_id="turn-2") == []
    assert _status(steering)["active"] == []


def test_sessions_are_scoped_independently(steering):
    assert _turn(steering, "please review this", session_id="sess-a") != []
    assert _status(steering, session_id="sess-b")["state"] == "inactive"
    assert _turn(steering, "please review this", session_id="sess-b") != []
    assert _status(steering, session_id="sess-a")["active"] == ["review"]


def test_session_reset_drops_records(steering):
    assert _turn(steering, "please review this") != []
    steering.hook("on_session_reset", session_id="sess-1")
    assert _status(steering)["state"] == "inactive"
    assert _turn(steering, "please review this") != []


# --- deterministic conflict resolution ---------------------------------------

CONFLICT_MODES = {
    "review": {"refs": [REF_A], "conflicts_with": ["ship"]},
    "ship": {"refs": [REF_B]},  # one-sided declaration
}


def _conflict_setup(load_plugin):
    return load_plugin("keyword-steering", settings={"modes": CONFLICT_MODES})


def test_conflicting_modes_withhold_both_and_surface_conflict(load_plugin):
    harness = _conflict_setup(load_plugin)
    results = _turn(harness, "review then ship it")
    assert len(results) == 1
    guidance = results[0]["context"]
    assert "conflict" in guidance
    assert REF_A not in guidance and REF_B not in guidance  # both withheld
    status = _status(harness)
    assert status["requested"] == ["review", "ship"]
    assert status["active"] == []
    assert status["withheld"] == ["review", "ship"]
    assert status["conflicts"] == [["review", "ship"]]


def test_conflict_resolution_is_order_independent(load_plugin):
    reversed_modes = {k: CONFLICT_MODES[k] for k in ("ship", "review")}
    harness = load_plugin("keyword-steering", settings={"modes": reversed_modes})
    _turn(harness, "ship it")
    _turn(harness, "ship and review it", turn_id="turn-2")
    status = _status(harness)
    assert status["active"] == []
    assert status["conflicts"] == [["review", "ship"]]  # canonical pair order


def test_compatible_modes_steer_together(load_plugin):
    harness = load_plugin("keyword-steering", settings={"modes": {
        "review": {"refs": [REF_A]},
        "audit": {"refs": [REF_B]},
    }})
    guidance = _turn(harness, "review and audit this")[0]["context"]
    assert REF_A in guidance and REF_B in guidance


# --- steering_status tool ----------------------------------------------------

def test_status_is_readonly_and_bounded(steering):
    _turn(steering, "please review this")
    status = _status(steering)
    assert status["lifetime"] == "turn" and status["scope"] == "session"
    assert status["references"] == {"review": [REF_A]}
    # Querying status never mutates resolution: the same turn stays deduped,
    # and the next turn still steers normally.
    assert _turn(steering, "please review this") == []
    assert _turn(steering, "review again", turn_id="turn-2") != []
    assert _status(steering)["active"] == ["review"]


# --- guidance-only: no semantic/model-effort side effects --------------------

def test_no_model_or_effort_mutation(steering):
    # The plugin registers no pre_api_request hook: model/effort parameters
    # on the API seam are never touched.
    assert steering.hook("pre_api_request", model="m", request={}) == []


def test_tool_args_untouched(steering):
    calls = []
    steering.middleware(
        "tool_execution", tool_name="terminal", args={"command": "ls"},
        next_call=lambda values: calls.append(values) or "ok",
        session_id="sess-1", task_id="t1", tool_call_id="c1",
        turn_id="turn-1", api_request_id="api-1",
    )
    assert calls == [{"command": "ls"}]


# --- config boundary: migration and invalid tables fail loudly ---------------

def test_legacy_keywords_config_rejected(load_plugin):
    with pytest.raises(AssertionError, match="keywords"):
        load_plugin("keyword-steering", settings={
            "keywords": {"deploy": "Prefer the reviewed deploy pipeline."},
        })


def test_empty_config_stays_idle(load_plugin):
    harness = load_plugin("keyword-steering", settings={})
    assert _turn(harness, "please review this") == []
    assert _status(harness)["state"] == "inactive"


@pytest.mark.parametrize("bad_modes,reason", [
    ({"review": {"refs": []}}, "refs"),
    ({"review": {"refs": [""], "wat": 1}}, "unknown field"),
    ({"review": {"refs": [REF_A], "triggers": []}}, "triggers"),
    ({"review": {"refs": [REF_A], "conflicts_with": ["ghost"]}}, "unknown mode"),
    ({"review": {"refs": [REF_A], "conflicts_with": ["review"]}}, "itself"),
])
def test_invalid_mode_table_refused(load_plugin, bad_modes, reason):
    with pytest.raises(AssertionError, match=reason):
        load_plugin("keyword-steering", settings={"modes": bad_modes})


def test_session_cleanup_preserves_other_live_turns(steering):
    _turn(steering, "please review", session_id="owner")
    _turn(steering, "please review", session_id="peer")
    steering.hook("on_session_finalize", session_id="owner")
    assert _status(steering, "owner")["state"] == "inactive"
    assert _status(steering, "peer")["active"] == ["review"]
    assert _turn(steering, "please review", session_id="peer") == []


# --- built-in magic keywords (OMP-compatible) -------------------------------

MAGIC_MESSAGES = {
    "ultrathink": "please ultrathink this design",
    "orchestrate": "orchestrate the migration please",
    "workflow": "run this as a workflow please",
}
MAGIC_MARKERS = {
    "ultrathink": "maximum reasoning effort requested",
    "orchestrate": "delegation and parallel subagents",
    "workflow": "explicit plan first, then verification, then a closing review",
}


def test_magic_keywords_active_by_default_from_prose(load_plugin):
    harness = load_plugin("keyword-steering", settings={})
    for keyword, message in MAGIC_MESSAGES.items():
        results = _turn(harness, message, turn_id=f"turn-{keyword}")
        assert len(results) == 1
        guidance = results[0]["context"]
        assert f"Magic keyword '{keyword}' is active" in guidance
        assert MAGIC_MARKERS[keyword] in guidance
        status = _status(harness)
        assert status["active"] == [keyword]
        assert status["references"][keyword] == [
            "built-in magic keyword (OMP-compatible)"
        ]


def test_magic_keywords_ignored_outside_prose(steering):
    for i, message in enumerate((
        "```\nultrathink it\n```",
        "run `orchestrate --all` now",
        'the doc said "workflow mode" twice',
        "check src/ultrathink/hooks.py and workflow_timeout",
    )):
        assert _turn(steering, message, turn_id=f"t{i}") == []


def test_orchestrate_guidance_documents_model_role_routing(load_plugin):
    harness = load_plugin("keyword-steering", settings={})
    results = _turn(harness, "orchestrate the migration please")
    assert len(results) == 1
    guidance = results[0]["context"]
    # Deterministic routing guidance: delegate tasks carry model_role,
    # route_status is the role registry.
    assert "delegate tasks may pass `model_role`" in guidance
    assert "`route_status` tool lists the configured roles" in guidance


def test_ultrathink_calls_set_turn_reasoning_and_reports_applied(load_plugin, monkeypatch):
    from hermes_cli.plugins import PluginContext
    calls = []

    def fake_set_turn_reasoning(self, effort, reason=""):
        calls.append((effort, reason))
        return {"applied": True, "effort": effort, "source": "plugin"}

    monkeypatch.setattr(PluginContext, "set_turn_reasoning",
                        fake_set_turn_reasoning, raising=False)
    harness = load_plugin("keyword-steering", settings={})
    guidance = _turn(harness, "please ultrathink this design")[0]["context"]
    assert calls == [("ultra", "magic keyword: ultrathink")]
    assert "raised to 'ultra'" in guidance
    assert "applied=true" in guidance
    effect = _status(harness)["magic_effects"]["ultrathink"]
    assert effect["applied"] is True and effect["degraded"] is False
    assert effect["receipt"] == {"applied": True, "effort": "ultra", "source": "plugin"}
    # Turn dedup: the seam is requested once per turn, never again.
    assert _turn(harness, "please ultrathink this design") == []
    assert calls == [("ultra", "magic keyword: ultrathink")]


def test_ultrathink_degrades_without_host_seam(load_plugin, monkeypatch):
    from hermes_cli.plugins import PluginContext
    # Deterministic regardless of whether the host has grown the seam.
    monkeypatch.delattr(PluginContext, "set_turn_reasoning", raising=False)
    harness = load_plugin("keyword-steering", settings={})
    guidance = _turn(harness, "please ultrathink this design")[0]["context"]
    assert "degraded to guidance-only" in guidance
    assert "does not provide PluginContext.set_turn_reasoning" in guidance
    effect = _status(harness)["magic_effects"]["ultrathink"]
    assert effect["applied"] is False and effect["degraded"] is True
    assert effect["reason"] == "magic keyword: ultrathink"
    assert "unavailable" in effect["detail"]


def test_ultrathink_reports_host_declined_receipt(load_plugin, monkeypatch):
    from hermes_cli.plugins import PluginContext
    monkeypatch.setattr(PluginContext, "set_turn_reasoning",
                        lambda self, effort, reason="": {"applied": False}, raising=False)
    harness = load_plugin("keyword-steering", settings={})
    guidance = _turn(harness, "please ultrathink this design")[0]["context"]
    assert "degraded to guidance-only" in guidance
    assert "applied=false" in guidance
    effect = _status(harness)["magic_effects"]["ultrathink"]
    assert effect["applied"] is False and effect["degraded"] is True


def test_magic_keyword_disabled_via_config(load_plugin):
    harness = load_plugin("keyword-steering", settings={
        "magic_keywords": {"ultrathink": False},
    })
    assert _turn(harness, "please ultrathink this design") == []
    assert _turn(harness, "orchestrate the migration please", turn_id="turn-2") != []
    summary = _status(harness)["magic_keywords"]
    assert summary["ultrathink"]["enabled"] is False
    assert summary["orchestrate"]["enabled"] is True
    assert summary["workflow"]["source"] == "built-in (OMP-compatible)"


def test_all_magic_keywords_disabled_via_enabled_flag(load_plugin):
    harness = load_plugin("keyword-steering", settings={
        "magic_keywords": {"enabled": False},
    })
    for i, keyword in enumerate(("ultrathink", "orchestrate", "workflow")):
        assert _turn(harness, f"please {keyword} this", turn_id=f"t{i}") == []
    assert _status(harness)["state"] == "inactive"


def test_user_mode_cannot_shadow_magic_keyword(load_plugin):
    with pytest.raises(AssertionError, match="reserved"):
        load_plugin("keyword-steering", settings={"modes": {
            "ultrathink": {"refs": [REF_A]},
        }})


def test_user_mode_conflict_with_magic_keyword_withholds_both(load_plugin):
    harness = load_plugin("keyword-steering", settings={"modes": {
        "quick": {"refs": [REF_A], "conflicts_with": ["ultrathink"]},
    }})
    guidance = _turn(harness, "do a quick ultrathink pass please")[0]["context"]
    assert "conflict" in guidance
    assert REF_A not in guidance
    assert "maximum reasoning effort requested" not in guidance
    status = _status(harness)
    assert status["withheld"] == ["quick", "ultrathink"]
    assert status["conflicts"] == [["quick", "ultrathink"]]
    # The effort seam is never requested for a withheld keyword.
    assert status["magic_effects"] == {}


def test_status_lists_magic_keyword_provenance(load_plugin):
    harness = load_plugin("keyword-steering", settings={})
    _turn(harness, "orchestrate the migration please")
    status = _status(harness)
    assert set(status["magic_keywords"]) == {"ultrathink", "orchestrate", "workflow"}
    assert all(entry["enabled"] is True
               and entry["source"] == "built-in (OMP-compatible)"
               for entry in status["magic_keywords"].values())
    assert status["references"]["orchestrate"] == [
        "built-in magic keyword (OMP-compatible)"
    ]


@pytest.mark.parametrize("bad_magic,reason", [
    (["ultrathink"], "mapping"),
    ({"enabled": "sure"}, "boolean"),
    ({"ultrathink": "yes"}, "boolean"),
    ({"hyperthink": True}, "unknown magic"),
])
def test_invalid_magic_config_refused(load_plugin, bad_magic, reason):
    with pytest.raises(AssertionError, match=reason):
        load_plugin("keyword-steering", settings={"magic_keywords": bad_magic})
