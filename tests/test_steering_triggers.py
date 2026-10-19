from test_steering_modes import REF_A, _status, _turn


def _assert_one_hit(harness, **expected):
    """The latest turn recorded exactly one hit, and it carries the ``expected`` fields."""
    hits = _status(harness)["hits"]
    assert len(hits) == 1, hits
    assert {key: hits[0][key] for key in expected} == expected


def test_negation_declines_hit(load_plugin):
    h = load_plugin("keyword-steering", settings={"modes": {"review": {"refs": [REF_A]}}})

    assert _turn(h, "don't review this one") == []

    _assert_one_hit(h, mode="review", trigger="review", form="bare", ambiguity="negated",
                    verdict="declined", confirmed_by="regex", reason="negated")
    assert _status(h)["requested"] == []


def test_negation_far_away_does_not_decline(load_plugin):
    h = load_plugin("keyword-steering", settings={"modes": {"review": {"refs": [REF_A]}}})

    # "never" is six tokens before "review": outside the three-token negation window.
    results = _turn(h, "never mind that, after lunch please review this")

    assert len(results) == 1
    assert "Steering mode `review` active" in results[0]["context"]
    _assert_one_hit(h, mode="review", form="bare", ambiguity=None,
                    verdict="active", reason=None)


def test_default_common_word_requires_sigil(load_plugin):
    h = load_plugin("keyword-steering", settings={})

    # A bare "workflow" is a common word: declined, and the reason says why.
    assert _turn(h, "the GitHub workflow is failing again", turn_id="turn-1") == []
    _assert_one_hit(h, mode="workflow", trigger="workflow", form="bare",
                    ambiguity="common_word", verdict="declined", confirmed_by="regex",
                    reason="sigil_required")

    # Either sigil spelling is an explicit request.
    for turn_id, message in (("turn-2", "/workflow the migration"),
                             ("turn-3", "#workflow please")):
        results = _turn(h, message, turn_id=turn_id)
        assert len(results) == 1, message
        assert "Magic keyword 'workflow' is active" in results[0]["context"], message
        _assert_one_hit(h, mode="workflow", form="sigil", ambiguity=None,
                        verdict="active", reason=None)

    # Paths and filenames are neither a bare mention nor a sigil: no hit at all.
    for turn_id, message in (("turn-4", "see src/workflow for details"),
                             ("turn-5", "see /workflow.py for details")):
        assert _turn(h, message, turn_id=turn_id) == [], message
        assert _status(h)["hits"] == [], message

    # ultrathink is bare by default: no sigil needed.
    results = _turn(h, "please ultrathink this design", turn_id="turn-6")
    assert len(results) == 1
    assert "Magic keyword 'ultrathink' is active" in results[0]["context"]
    _assert_one_hit(h, mode="ultrathink", form="bare", ambiguity=None,
                    verdict="active", reason=None)


def test_bare_true_restores_omp_matching(load_plugin):
    h = load_plugin("keyword-steering", settings={
        "magic_keywords": {"workflow": {"enabled": True, "bare": True}},
    })

    results = _turn(h, "run this as a workflow please")

    assert len(results) == 1
    assert "Magic keyword 'workflow' is active" in results[0]["context"]
    _assert_one_hit(h, mode="workflow", form="bare", ambiguity=None,
                    verdict="active", reason=None)
    magic = _status(h)["magic_keywords"]
    assert magic["workflow"]["bare"] is True
    assert magic["orchestrate"]["bare"] is False


def test_user_mode_bare_false_requires_sigil(load_plugin):
    h = load_plugin("keyword-steering", settings={
        "modes": {"strict": {"refs": ["rule://x"], "bare": False}},
    })

    assert _turn(h, "be strict please", turn_id="turn-1") == []
    _assert_one_hit(h, mode="strict", form="bare", ambiguity="common_word",
                    verdict="declined", reason="sigil_required")

    results = _turn(h, "/strict please", turn_id="turn-2")
    assert len(results) == 1
    assert "Steering mode `strict` active" in results[0]["context"]
    _assert_one_hit(h, mode="strict", form="sigil", verdict="active")


def test_bare_match_inside_sigil_is_one_hit(load_plugin):
    h = load_plugin("keyword-steering", settings={})

    # "#" is not a path character, so the bare pattern also matches inside "#workflow";
    # that match is the sigil itself and must not become a second, declined hit.
    assert len(_turn(h, "#workflow")) == 1

    _assert_one_hit(h, mode="workflow", form="sigil", verdict="active")
