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
