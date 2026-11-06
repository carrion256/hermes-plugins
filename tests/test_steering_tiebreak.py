"""Classifier tie-break for ambiguous keyword-steering hits (common word, negated).

The transport is classifier-core's shared client, so these tests stand in for that module
(the ``classifier_stub`` fixture) and never for HTTP: what this plugin owns is which
question it asks, what it refuses to send, what it accepts as an answer, and what it
reports when the shared transport is not there.
"""
import pytest

from test_meta_classifier import Snapshot
from test_steering_modes import REF_A, _status, _turn

SETTINGS = {"classifier": {"enabled": True}}
# Bare `workflow` is a common word: without the classifier it needs /workflow or #workflow.
COMMON_WORD = "the GitHub workflow is failing again"
LABELS = ("directive", "mention", "negated")
CALLER = "keyword-steering"


def judge(classifier_stub, label):
    """Answer the tie-break's one question with ``label``; return the recorded requests.

    ``label`` is the choice the shared transport back-maps for the ``steering`` head. Pass
    ``None`` for a transport that declines the request: that is how an unconfigured,
    breaker-open or unreachable service reaches this plugin now, and the decision it must
    produce is the same one an unusable answer produces -- decline the hit.
    """
    def answer(state, questions, **_kw):
        if label is None:
            return None
        return {"model": "fake", "usage": {},
                "answers": {"steering": {"type": "choice", "choice": str(label)}}}

    return classifier_stub.install(answer)


def test_mention_label_declines_common_word(load_plugin, classifier_stub):
    stub = judge(classifier_stub, "mention")
    h = load_plugin("keyword-steering", settings=SETTINGS)

    assert _turn(h, COMMON_WORD) == []

    (hit,) = _status(h)["hits"]
    assert hit["ambiguity"] == "common_word"
    assert hit["verdict"] == "declined"
    assert hit["confirmed_by"] == "classifier"
    assert hit["reason"] == "classifier:mention"
    assert len(stub.calls) == 1


def test_directive_label_activates_common_word(load_plugin, classifier_stub):
    stub = judge(classifier_stub, "directive")
    h = load_plugin("keyword-steering", settings=SETTINGS)

    (result,) = _turn(h, COMMON_WORD)

    assert "Magic keyword 'workflow' is active" in result["context"]
    (hit,) = _status(h)["hits"]
    assert hit["ambiguity"] == "common_word"
    assert hit["verdict"] == "active"
    assert hit["confirmed_by"] == "classifier"
    assert hit["reason"] is None
    assert len(stub.calls) == 1


def test_negated_hit_is_judged_too(load_plugin, classifier_stub):
    stub = judge(classifier_stub, "directive")
    h = load_plugin("keyword-steering", settings={
        **SETTINGS, "modes": {"review": {"refs": [REF_A]}},
    })

    (result,) = _turn(h, "don't review this one")

    assert REF_A in result["context"]  # the classifier overrode the deterministic negation
    (hit,) = _status(h)["hits"]
    assert hit["mode"] == "review"
    assert hit["ambiguity"] == "negated"
    assert hit["verdict"] == "active"
    assert hit["confirmed_by"] == "classifier"
    (request,) = stub.calls
    assert request["state"].startswith("Keyword: review\nMessage: ")


def test_unavailable_classifier_declines(load_plugin, classifier_stub):
    judge(classifier_stub, None)
    h = load_plugin("keyword-steering", settings=SETTINGS)

    assert _turn(h, COMMON_WORD) == []

    status = _status(h)
    (hit,) = status["hits"]
    assert hit["verdict"] == "declined"
    assert hit["confirmed_by"] == "regex"
    assert hit["reason"] == "classifier_unavailable"
    assert status["classifier"]["enabled"] is True
    assert isinstance(status["classifier"]["last_error"], str)
    assert "unusable" in status["classifier"]["last_error"]


def test_request_shape_and_redaction(load_plugin, classifier_stub):
    stub = judge(classifier_stub, "mention")
    h = load_plugin("keyword-steering", settings={**SETTINGS, "classifier": {
        "enabled": True, "timeout": 4.0}})
    secret = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"

    _turn(h, f"the workflow token is sk-ant-api03-{secret}")

    (request,) = stub.calls
    assert request["state"].startswith("Keyword: workflow\nMessage: the workflow token is ")
    assert secret not in request["state"]  # redaction ran before the text left the plugin
    question = request["questions"]["steering"]
    assert question["type"] == "choice"  # one head, asked as the labels describe it
    assert set(question["criteria"]) == set(LABELS)
    assert request["caller"] == CALLER  # declines are attributed to this plugin, not to nobody
    assert request["timeout"] == 4.0  # this seam's own wait budget, stated per call


def test_unambiguous_hit_never_calls_classifier(load_plugin, classifier_stub):
    stub = judge(classifier_stub, "mention")
    h = load_plugin("keyword-steering", settings=SETTINGS)

    (result,) = _turn(h, "please ultrathink this design")

    assert "Magic keyword 'ultrathink' is active" in result["context"]
    (hit,) = _status(h)["hits"]
    assert hit["ambiguity"] is None
    assert hit["verdict"] == "active"
    assert hit["confirmed_by"] == "regex"
    assert stub.calls == []


def test_garbage_label_declines_as_unavailable(load_plugin, classifier_stub):
    stub = judge(classifier_stub, "maybe")
    h = load_plugin("keyword-steering", settings=SETTINGS)

    assert _turn(h, COMMON_WORD) == []

    status = _status(h)
    (hit,) = status["hits"]
    assert hit["verdict"] == "declined"
    assert hit["confirmed_by"] == "regex"
    assert hit["reason"] == "classifier_unavailable"
    assert len(stub.calls) == 1  # the transport answered; the label itself was rejected
    assert "unusable" in status["classifier"]["last_error"]


def test_status_reports_the_shared_breaker(load_plugin, classifier_stub):
    """The outage belongs to the service, so the status reports the shared client's breaker.

    A private timer here would have disagreed with every other consumer of the same endpoint;
    these are the fields that let an operator see why hits are being declined.
    """
    stub = judge(classifier_stub, "mention")
    stub.status = lambda: Snapshot({
        "endpoint": "http://service.invalid:8102", "breaker": "open",
        "open_until": "2026-11-06T12:00:45+00:00", "consecutive_failures": 3,
        "last_error": "ConnectionError: refused",
        "callers": {CALLER: {"asks": 4, "declines": {"transport": 3, "unusable_body": 1}}}})
    h = load_plugin("keyword-steering", settings=SETTINGS)

    _turn(h, COMMON_WORD)

    reported = _status(h)["classifier"]
    assert reported["enabled"] is True and reported["transport"] == "classifier-core"
    assert reported["breaker"] == "open"
    assert reported["open_until"] == "2026-11-06T12:00:45+00:00"
    assert reported["consecutive_failures"] == 3
    assert reported["service_error"] == "ConnectionError: refused"
    assert reported["counters"] == {"asks": 4, "declines": {"transport": 3, "unusable_body": 1}}


def test_status_names_an_absent_transport(load_plugin, classifier_stub):
    """A missing classifier-core is a documented degraded path, said out loud: every
    ambiguous hit declines, which is what the disabled switch does, and the reason is named."""
    classifier_stub.install(absent=True)
    h = load_plugin("keyword-steering", settings=SETTINGS)

    assert _turn(h, COMMON_WORD) == []

    reported = _status(h)["classifier"]
    assert reported["enabled"] is True
    assert reported["transport"] == "classifier_core_unavailable"


def test_disabled_tiebreak_reports_itself_and_asks_nothing(load_plugin, classifier_stub):
    stub = judge(classifier_stub, "directive")
    h = load_plugin("keyword-steering", settings={"classifier": {"enabled": False}})

    assert _turn(h, COMMON_WORD) == []

    assert _status(h)["classifier"] == {"enabled": False, "transport": "tiebreak_disabled"}
    assert stub.calls == []


def test_classifier_enabled_must_be_boolean(load_plugin):
    with pytest.raises(AssertionError, match="classifier.enabled must be a boolean"):
        load_plugin("keyword-steering", settings={"classifier": {"enabled": "yes"}})
