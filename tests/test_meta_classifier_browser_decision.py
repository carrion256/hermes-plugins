"""browser_decision: jev-ultrafast-style one-request operation+target heads."""
import json

ELEMENTS = [
    {"index": 1, "role": "button", "name": "Change ticket type · Round trip"},
    {"index": 2, "role": "combobox", "name": "Where from?", "value": "San Francisco"},
    {"index": 3, "role": "combobox", "name": "Where to?", "value": "empty"},
    {"index": 4, "role": "textbox", "name": "Departure", "value": "empty"},
    {"index": 5, "role": "combobox", "name": "Cabin", "options": ["Economy", "Premium", "Business"]},
]


def build(goal="Find one-way flights from Zurich to London", elements=None):
    return build_request(goal, ELEMENTS if elements is None else elements)


def _import_module():
    import importlib.util
    from pathlib import Path
    path = (Path(__file__).resolve().parents[1] / "plugins" / "meta-classifier"
            / "browser_decision.py")
    spec = importlib.util.spec_from_file_location("meta_classifier_browser", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_request = _import_module().build_request
validate = _import_module().validate


def test_one_request_operation_plus_speculative_targets():
    state, schema, meta = build()
    assert list(schema) == ["operation", "click_target", "type_target",
                            "select_target", "select_option"]
    assert "click" in schema["operation"]["labels"] and "select" in schema["operation"]["labels"]
    # each target head contains only compatible elements
    assert set(schema["click_target"]["labels"]) == {"1", "2", "3"}  # combobox w/o options = activation click
    assert set(schema["type_target"]["labels"]) == {"4", "none"}  # single element -> 'none' appended
    assert set(schema["select_target"]["labels"]) == {"5", "none"}  # combobox WITH options only
    assert "2" not in schema["select_target"]["labels"]        # no observed options -> not selectable
    assert set(schema["select_option"]["labels"]) == {"5:0", "5:1", "5:2"}
    assert state["goal"].startswith("Find one-way")  # returned as a dict; plugin canonicalizes+redacts


def test_validate_strict_on_menu():
    _state, _schema, meta = build()
    ok, problems = validate({"operation": "click", "click_target": "1"}, meta)
    assert ok == {"operation": "click", "target_index": 1} and problems == []
    ok, problems = validate({"operation": "select", "select_target": "5",
                            "select_option": "5:2"}, meta)
    assert ok == {"operation": "select", "target_index": 5, "option_index": 2}
    # hallucinated target: not in the offered head -> unusable, never coerced
    ok, problems = validate({"operation": "click", "click_target": "9"}, meta)
    assert ok is None and problems[0].startswith("target_not_offered")
    # select option mismatching the target -> unusable
    ok, problems = validate({"operation": "select", "select_target": "5",
                            "select_option": "3:0"}, meta)
    assert ok is None and problems[0].startswith("option_not_offered")
    # operation that was never offered (no such element type on the page)
    _s, _sc, meta_t = build(elements=[ELEMENTS[0]])
    ok, problems = validate({"operation": "type", "type_target": "4"}, meta_t)
    assert ok is None and problems[0].startswith("operation_not_offered")
    # 'none' is treated as a declined target, not an index
    ok, problems = validate({"operation": "type", "type_target": "none"}, meta)
    assert ok is None and problems[0] == "missing_type_target"


def test_no_options_no_select_head():
    elements = [{"index": 1, "role": "combobox", "name": "Sort"}, {"index": 2, "role": "link", "name": "x"}]
    _state, schema, meta = build_request("sort results", elements)
    assert "select_target" not in schema and "select" not in meta["operations"]
    assert "select_option" not in schema
    ok, _p = validate({"operation": "click", "click_target": "2"}, meta)
    assert ok["target_index"] == 2


def test_single_choice_dropdown_never_builds_illegal_head():
    # the service 400s any single-label head: a 1-option dropdown must degrade to
    # an activation click, and one option across all dropdowns must drop the head
    elements = [{"index": 1, "role": "combobox", "name": "Language", "options": ["English"]},
                {"index": 2, "role": "link", "name": "Apply"}]
    _state, schema, meta = build_request("switch language to english", elements)
    assert "select_target" not in schema and "select_option" not in schema
    assert "select" not in meta["operations"]
    assert "1" in schema["click_target"]["labels"]  # activation click instead
    ok, _p = validate({"operation": "click", "click_target": "1"}, meta)
    assert ok == {"operation": "click", "target_index": 1}
    # caller-declared select caps on a 1-option dropdown fall back to activation click
    elements[0]["caps"] = ["select"]
    _state, schema, meta = build_request("switch language to english", elements)
    assert "select_target" not in schema and "select_option" not in schema
    assert "1" in schema["click_target"]["labels"]
    # two options across two single-option dropdowns: no legal per-target option
    # head either (each would be 1-label), so select stays unoffered
    two = [{"index": 1, "role": "combobox", "name": "Lang", "options": ["en"]},
           {"index": 2, "role": "combobox", "name": "Region", "options": ["IM"]}]
    _state, schema, meta = build_request("set lang and region", two)
    assert "select_option" not in schema and "select" not in meta["operations"]


def test_input_validation():
    for bad in ((None, ELEMENTS), ("", ELEMENTS), ("goal", []), ("goal", "no")):
        try:
            build_request(*bad)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
    try:
        build_request("g", [{"role": "button"}])  # no index
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    huge = [{"index": i, "role": "button", "name": str(i)} for i in range(100)]
    try:
        build_request("g", huge)
        raise AssertionError("expected too_many_elements")
    except ValueError as exc:
        assert "too_many_elements" in str(exc)


# ---------------------------------------------------- plugin level: one round trip, egress-safe

class Reply:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self, size=-1):
        return json.dumps(self.body).encode()


def test_tool_one_round_trip_per_observation(load_plugin, monkeypatch):
    requests = []

    def fake_urlopen(request, *_a, **_kw):
        raw = json.loads(request.data)
        requests.append(raw)
        if len(requests) == 1:
            assert "click_target" in raw["schema"] and "type_target" in raw["schema"], \
                "operation+targets must share ONE request"
        assert "sk-secret" not in raw["text"]  # egress redaction ran on the table
        return Reply({"operation": "click", "click_target": "1"})

    monkeypatch.setenv("META_TEST_KEY", "synthetic-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    h = load_plugin("meta-classifier", {"api_key_env": "META_TEST_KEY"})
    elements = [dict(e) for e in ELEMENTS]
    elements[0]["name"] += " sk-ant-ABCDEF0123456789"
    out = json.loads(h.tool("browser_decision", {"goal": "switch to one-way", "elements": elements}))
    assert out["success"] is True
    assert out["decision"] == {"operation": "click", "target_index": 1}
    assert "click" in out["offered"]

    off_menu = json.loads(h.tool("browser_decision", {"goal": "switch to one-way",
                                                      "elements": [ELEMENTS[0]]}))
    assert off_menu["success"] is True  # click+1 was valid for THAT table; second request made
    assert len(requests) == 2


def test_tool_unavailable_is_honest(load_plugin, monkeypatch):
    from urllib.error import URLError
    monkeypatch.setenv("META_TEST_KEY", "synthetic-key")
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(URLError("down")))
    h = load_plugin("meta-classifier", {"api_key_env": "META_TEST_KEY"})
    out = json.loads(h.tool("browser_decision", {"goal": "g", "elements": ELEMENTS}))
    assert out["success"] is False and out["error"] == "classifier_unavailable"
