"""#9 — /cheap, /deep, /local ask model-routing to pin one role for the turn.

keyword-steering only ASKS; model-routing owns the pin and the resolver, so the
assertions go through the host seams a consumer uses (``resolve_role_model``,
``route_status``) rather than through either plugin's internals. The keyword is
worth nothing if it pins a model that the next turn still uses, or if a pin in
one session changes another session's model, so those are asserted too.
"""
import json

import pytest
from hermes_cli import plugins as plugins_mod
from test_steering_modes import _status, _turn

ROLES = {"cheap": {"model": "tiny-1"},
         "deep": {"model": "big-1"},
         "local": {"model": "laptop-1"}}
PARENT_MODEL = "parent-model"
PARENT_PROVIDER = "parent-provider"


def _load(load_plugin, *, roles=ROLES, settings=None):
    """Both plugins through the real loader: model-routing with ``roles``, then
    keyword-steering (whose pin request is what these tests observe)."""
    route = load_plugin("model-routing", settings={"roles": roles})
    steer = load_plugin("keyword-steering", settings=settings or {})
    return route, steer


def _resolve(role, session_id):
    """The host's own resolution entry point (delegate/review code calls this)."""
    return plugins_mod.resolve_role_model(
        role, session_id=session_id, parent_model=PARENT_MODEL,
        parent_provider=PARENT_PROVIDER)


def _effects(h, session="kw-a"):
    return _status(h, session_id=session)["magic_effects"]


def _assert_pinned(receipt, model):
    """The resolution IS the pinned model, and the host can actually read it.

    A receipt whose ``source`` the host does not know is discarded as malformed,
    which would make the keyword silently do nothing — so honouring the host's
    vocabulary is part of the feature, not a detail of it.
    """
    assert receipt["resolved"] is True and receipt["model"] == model, receipt
    assert receipt["source"] in plugins_mod._ROLE_RECEIPT_SOURCES, receipt
    return receipt


def test_cheap_activates_and_the_resolver_answers_the_pinned_role(load_plugin):
    _, h = _load(load_plugin)

    out = _turn(h, "/cheap skim this diff", session_id="kw-a", turn_id="t-1")

    assert any("Magic keyword 'cheap' is active" in item["context"] for item in out)
    receipt = _assert_pinned(_resolve("anything", "kw-a"), "tiny-1")
    # The requested role is echoed, not rewritten: the caller still sees what it asked.
    assert receipt["role"] == "anything"
    # The reason is self-describing regardless of the source label: which role is
    # pinned, and who asked for it.
    assert "turn pin 'cheap'" in receipt["reason"] and "keyword-steering" in receipt["reason"]
    assert _effects(h)["cheap"] == {
        "keyword": "cheap", "role": "cheap", "applied": True, "degraded": False,
        "reason": "applied", "detail": "turn pin recorded", "model": "tiny-1", "provider": None,
        "line": _effects(h)["cheap"]["line"]}
    assert "route_status" in _effects(h)["cheap"]["line"]


def test_the_pin_reports_a_source_the_host_honours(load_plugin, monkeypatch):
    # This host's receipt law knows no "turn_pin"; emitting it anyway would make
    # every consumer drop the receipt, so the plugin asks the host first.
    _, h = _load(load_plugin)
    assert "turn_pin" not in plugins_mod._ROLE_RECEIPT_SOURCES
    _turn(h, "/deep think hard", session_id="kw-label-1", turn_id="t-1")
    assert _assert_pinned(_resolve("anything", "kw-label-1"), "big-1")["source"] == "explicit"

    # A host that does recognise the source gets the precise provenance instead.
    monkeypatch.setattr(plugins_mod, "_ROLE_RECEIPT_SOURCES",
                        frozenset(plugins_mod._ROLE_RECEIPT_SOURCES | {"turn_pin"}))
    _, h = _load(load_plugin)  # re-register: the label is chosen when the resolver is built
    _turn(h, "/deep think hard", session_id="kw-label-2", turn_id="t-1")
    assert _resolve("anything", "kw-label-2")["source"] == "turn_pin"


def test_the_pin_reaches_any_requested_role_in_that_session_only(load_plugin):
    _, h = _load(load_plugin)
    _turn(h, "/deep think hard", session_id="kw-pin", turn_id="t-1")

    for role in ("executor", "reviewer", "anything-not-configured"):
        _assert_pinned(_resolve(role, "kw-pin"), "big-1")

    # The explicit keyword belongs to one session: an unrelated session keeps the
    # configured behaviour for an unknown role (inherit the parent model).
    other = _resolve("anything-not-configured", "kw-other")
    assert other["source"] == "inherit" and other["model"] == PARENT_MODEL


def test_the_pin_never_survives_into_the_next_turn(load_plugin):
    _, h = _load(load_plugin)
    _turn(h, "/cheap skim", session_id="kw-a", turn_id="t-1")
    _assert_pinned(_resolve("anything", "kw-a"), "tiny-1")

    out = _turn(h, "now summarise it", session_id="kw-a", turn_id="t-2")

    assert out == []  # no keyword, so no guidance invented for the new turn
    assert _resolve("anything", "kw-a")["source"] == "inherit"


def test_session_reset_drops_the_pin(load_plugin):
    _, h = _load(load_plugin)
    _turn(h, "/local work offline", session_id="kw-a", turn_id="t-1")
    assert _resolve("anything", "kw-a")["model"] == "laptop-1"

    h.hook("on_session_reset", session_id="kw-a", old_session_id="kw-a",
           new_session_id="kw-a", session_key="kw-a")

    assert _resolve("anything", "kw-a")["source"] == "inherit"
    assert _status(h, session_id="kw-a")["active"] == []


def test_route_status_lists_the_active_pin(load_plugin):
    route, h = _load(load_plugin)
    _turn(h, "/cheap skim", session_id="kw-a", turn_id="t-1")

    pins = [row for row in json.loads(route.tool("route_status", {}))["turn_pins"]
            if row["session_id"] == "kw-a"]

    assert pins == [{"session_id": "kw-a", "turn_id": "t-1",
                     "role": "cheap", "model": "tiny-1",
                     "provider": None, "requested_by": "keyword-steering"}]
    # The configured table is still reported unchanged: a pin is runtime state,
    # not configuration, and must not masquerade as the route.
    assert sorted(json.loads(route.tool("route_status", {}))["effective"]) == ["cheap", "deep", "local"]


def test_two_routing_keywords_are_both_withheld_and_pin_nothing(load_plugin):
    _, h = _load(load_plugin)

    _turn(h, "/cheap /deep which model?", session_id="kw-a", turn_id="t-1")

    status = _status(h, session_id="kw-a")
    assert status["state"] == "conflict"
    assert sorted(status["withheld"]) == ["cheap", "deep"]
    assert sorted(tuple(sorted(pair)) for pair in status["conflicts"]) == [("cheap", "deep")]
    assert status["active"] == []
    # Contradiction means NO decision: neither keyword pins, and the status says so
    # rather than quietly keeping whichever keyword was parsed first.
    assert status["magic_effects"] == {}
    assert _resolve("anything", "kw-a")["source"] == "inherit"


def test_bare_common_words_do_not_pin_a_model(load_plugin):
    _, h = _load(load_plugin)

    out = _turn(h, "the cheap local deep-fat-fried option", session_id="kw-a", turn_id="t-1")

    assert out == []
    assert _resolve("anything", "kw-a")["source"] == "inherit"
    status = _status(h, session_id="kw-a")
    assert status["active"] == []
    assert {hit["mode"] for hit in status["hits"]} == {"cheap", "deep", "local"}
    for hit in status["hits"]:
        assert (hit["form"], hit["verdict"], hit["reason"]) == ("bare", "declined", "sigil_required")


def test_a_role_model_routing_does_not_have_says_so(load_plugin):
    _, h = _load(load_plugin, roles={"deep": {"model": "big-1"}})

    out = _turn(h, "/cheap skim", session_id="kw-a", turn_id="t-1")

    assert any("reason: role_not_configured" in item["context"] for item in out)
    effect = _effects(h)["cheap"]
    assert (effect["applied"], effect["reason"], effect["model"]) == (False, "role_not_configured", None)
    assert effect["degraded"] is True
    # The user keeps their turn: the keyword is withheld from routing, the message
    # is not refused, and nothing is pinned.
    assert _resolve("anything", "kw-a")["source"] == "inherit"


def test_without_model_routing_the_keyword_is_guidance_only(load_plugin, monkeypatch):
    h = load_plugin("keyword-steering", settings={})
    # The sibling really is absent: the path-load the plugin uses to reach it finds
    # nothing, which is the installed-without-model-routing case.
    monkeypatch.setattr(h.module, "_load_sibling", lambda key, path: None)

    out = _turn(h, "/deep think hard", session_id="kw-none", turn_id="t-1")

    assert any("reason: model_routing_unavailable" in item["context"] for item in out)
    effect = _effects(h, session="kw-none")["deep"]
    assert (effect["applied"], effect["reason"], effect["degraded"]) == (
        False, "model_routing_unavailable", True)
    # Resolution itself is untouched — the host has no resolver, so inheritance stands.
    assert _resolve("anything", "kw-none")["source"] == "unavailable"


def test_routing_keywords_setting_renames_the_role(load_plugin):
    _, h = _load(load_plugin,
                 roles={"tiny": {"model": "tiny-9"}, "deep": {"model": "big-1"},
                        "local": {"model": "laptop-1"}},
                 settings={"routing_keywords": {"cheap": "tiny"}})

    _turn(h, "/cheap skim", session_id="kw-a", turn_id="t-1")

    assert _effects(h)["cheap"]["role"] == "tiny"
    assert _resolve("anything", "kw-a")["model"] == "tiny-9"


@pytest.mark.parametrize("bad,message", [
    # The wrong TYPE and a wrong VALUE are different complaints, and each one
    # refuses registration instead of silently ignoring half the table.
    ({"routing_keywords": "cheap=cheap"}, "must be a mapping"),
    ({"routing_keywords": {"cheapskate": "cheap"}}, "not a routing keyword"),
    ({"routing_keywords": {"cheap": "Cheap-Role"}}, "must be a role name"),
    ({"routing_keywords": {"cheap": ""}}, "must be a role name"),
])
def test_bad_routing_keyword_config_refuses_registration(load_plugin, bad, message):
    with pytest.raises(AssertionError, match=message):
        load_plugin("keyword-steering", settings=bad)
