"""Classifier tie-break for ambiguous keyword-steering hits (common word, negated)."""
import json

import pytest

from test_meta_classifier import Reply
from test_steering_modes import REF_A, _status, _turn

SETTINGS = {"classifier": {"enabled": True, "api_key_env": "KS_TEST_KEY"}}
# Bare `workflow` is a common word: without the classifier it needs /workflow or #workflow.
COMMON_WORD = "the GitHub workflow is failing again"


def stub(monkeypatch, reply):
    """Fake the classifier endpoint; return the list of requests it receives."""
    monkeypatch.setenv("KS_TEST_KEY", "synthetic-key")
    requests = []

    def fake_urlopen(request, *a, **kw):
        requests.append(request)
        if isinstance(reply, Exception):
            raise reply
        return Reply(reply)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return requests


def test_mention_label_declines_common_word(load_plugin, monkeypatch):
    requests = stub(monkeypatch, {"steering": "mention"})
    h = load_plugin("keyword-steering", settings=SETTINGS)

    assert _turn(h, COMMON_WORD) == []

    (hit,) = _status(h)["hits"]
    assert hit["ambiguity"] == "common_word"
    assert hit["verdict"] == "declined"
    assert hit["confirmed_by"] == "classifier"
    assert hit["reason"] == "classifier:mention"
    assert len(requests) == 1


def test_directive_label_activates_common_word(load_plugin, monkeypatch):
    requests = stub(monkeypatch, {"steering": "directive"})
    h = load_plugin("keyword-steering", settings=SETTINGS)

    (result,) = _turn(h, COMMON_WORD)

    assert "Magic keyword 'workflow' is active" in result["context"]
    (hit,) = _status(h)["hits"]
    assert hit["ambiguity"] == "common_word"
    assert hit["verdict"] == "active"
    assert hit["confirmed_by"] == "classifier"
    assert hit["reason"] is None
    assert len(requests) == 1


def test_negated_hit_is_judged_too(load_plugin, monkeypatch):
    requests = stub(monkeypatch, {"steering": "directive"})
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
    (request,) = requests
    assert json.loads(request.data)["text"].startswith("Keyword: review\nMessage: ")


def test_unavailable_classifier_declines(load_plugin, monkeypatch):
    stub(monkeypatch, OSError("down"))
    h = load_plugin("keyword-steering", settings=SETTINGS)

    assert _turn(h, COMMON_WORD) == []

    status = _status(h)
    (hit,) = status["hits"]
    assert hit["verdict"] == "declined"
    assert hit["confirmed_by"] == "regex"
    assert hit["reason"] == "classifier_unavailable"
    assert status["classifier"]["enabled"] is True
    assert isinstance(status["classifier"]["last_error"], str)
    assert "OSError" in status["classifier"]["last_error"]


def test_request_shape_and_redaction(load_plugin, monkeypatch):
    requests = stub(monkeypatch, {"steering": "mention"})
    h = load_plugin("keyword-steering", settings=SETTINGS)
    secret = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"

    _turn(h, f"the workflow token is sk-ant-api03-{secret}")

    (request,) = requests
    body = json.loads(request.data)
    assert body["text"].startswith("Keyword: workflow\nMessage: the workflow token is ")
    assert "steering" in body["schema"]
    assert secret not in request.data.decode()
    assert request.get_header("Authorization") == "Bearer synthetic-key"


def test_unambiguous_hit_never_calls_classifier(load_plugin, monkeypatch):
    requests = stub(monkeypatch, {"steering": "mention"})
    h = load_plugin("keyword-steering", settings=SETTINGS)

    (result,) = _turn(h, "please ultrathink this design")

    assert "Magic keyword 'ultrathink' is active" in result["context"]
    (hit,) = _status(h)["hits"]
    assert hit["ambiguity"] is None
    assert hit["verdict"] == "active"
    assert hit["confirmed_by"] == "regex"
    assert requests == []


def test_garbage_label_declines_as_unavailable(load_plugin, monkeypatch):
    requests = stub(monkeypatch, {"steering": "maybe"})
    h = load_plugin("keyword-steering", settings=SETTINGS)

    assert _turn(h, COMMON_WORD) == []

    status = _status(h)
    (hit,) = status["hits"]
    assert hit["verdict"] == "declined"
    assert hit["confirmed_by"] == "regex"
    assert hit["reason"] == "classifier_unavailable"
    assert len(requests) == 1  # the endpoint answered; the label itself was rejected
    assert "ValueError" in status["classifier"]["last_error"]


def test_classifier_enabled_must_be_boolean(load_plugin):
    with pytest.raises(AssertionError, match="classifier.enabled must be a boolean"):
        load_plugin("keyword-steering", settings={"classifier": {"enabled": "yes"}})
