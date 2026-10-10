import hashlib
import json
import time

import pytest

from agent.subagent_lifecycle import bind_subagent_parent


def native_agent(session_id="owner", turn_id="turn"):
    from run_agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = session_id
    agent._current_turn_id = turn_id
    agent._persist_disabled = False
    agent._interrupt_requested = False
    agent._ask_calls = []

    def callback(question, provided_choices):
        agent._ask_calls.append((question, provided_choices))
        return next(iter(agent._answers), "")

    agent.clarify_callback = callback
    agent._answers = []
    return agent


def bind_agent(agent, task_id, cwd):
    """Native workspace anchoring: override registration records the session cwd."""
    from tools.terminal_tool import register_task_env_overrides
    register_task_env_overrides(task_id, {"cwd": str(cwd)})
    return bind_subagent_parent(agent)


def setup(load_plugin, monkeypatch, agent=None):
    h = load_plugin("auto-answer-classifier", {"api_key_env": "ANSWER_TEST_KEY"})
    if agent is not None:
        monkeypatch.setattr(h.module, "_owner", lambda *a: (agent, agent.session_id, agent._current_turn_id))
    monkeypatch.delenv("ANSWER_TEST_KEY", raising=False)
    return h


def observed_proof(h, *, session_id="owner", turn_id="turn", task_id="task",
                   workspace="/work/alpha", tool_call_id="call", qid="q0",
                   question="Which indentation?", choice="tabs", created=None):
    proof = {"session_id": session_id, "turn_id": turn_id, "task_id": task_id,
             "workspace": str(workspace), "tool_call_id": tool_call_id, "qid": qid,
             "question": question, "choice": choice,
             "created_at": time.time() if created is None else created}
    key = h.module._digest([session_id, turn_id, tool_call_id, qid])
    h.context.state.set("observations_v2", {key: proof})
    return proof


def test_observed_answer_records_native_proof(load_plugin, tmp_path):
    h = load_plugin("auto-answer-classifier")
    agent = native_agent()
    from tools.clarify_tool import clarify_tool
    args = {"question": "Which indentation?", "choices": ["tabs", "spaces"]}
    raw = clarify_tool(**args, callback=lambda *_a: "tabs")
    with bind_agent(agent, "task-observed", tmp_path):
        h.hook("post_tool_call", tool_name="clarify", args=args, result=raw,
               session_id="owner", turn_id="turn", task_id="task-observed",
               tool_call_id="observed-call")
    stored = h.context.state.get("observations_v2")
    proof = stored[h.module._digest(["owner", "turn", "observed-call", "q0"])]
    assert proof["choice"] == "tabs"
    assert proof["workspace"] == str(tmp_path)
    assert proof["tool_call_id"] == "observed-call"


def test_approved_convention_resolves_without_native_clarify(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    observed_proof(h, workspace=str(tmp_path), tool_call_id="call", qid="q0")
    agent._answers = ["Remember this convention"]
    with bind_agent(agent, "task", tmp_path):
        recorded = json.loads(h.tool("clarification_defaults", {
            "action": "record", "tool_call_id": "call", "qid": "q0",
            "question": "Which indentation?", "choice": "tabs"}, task_id="task"))
        assert recorded["recorded"] is True
        result = json.loads(h.middleware(
            "tool_execution", tool_name="clarify",
            args={"question": "Which indentation?", "choices": ["tabs", "spaces"]},
            session_id="owner", turn_id="turn", task_id="task",
            next_call=lambda _args: (_ for _ in ()).throw(AssertionError("native clarify called"))))
    decision = result["automatic_answers"][0]
    assert decision["selected"] == "tabs" and decision["applied"] is True
    assert result["responses"][0]["response_status"] == "automatic_default"
    # The upstream default pass asked nobody; only the consent question was asked.
    assert len(agent._ask_calls) == 1


def test_consent_denial_keeps_convention_unrecorded(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    observed_proof(h, workspace=str(tmp_path))
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "record", "tool_call_id": "call", "qid": "q0",
            "question": "Which indentation?", "choice": "tabs"}, task_id="task"))
    assert result["recorded"] is False
    assert agent._ask_calls  # the human WAS asked, and answered Do not remember


@pytest.mark.parametrize("field,value", [
    ("tool_call_id", "replacement"),
    ("qid", "q1"),
    ("workspace", "/work/foreign"),
])
def test_mismatched_or_foreign_observation_is_refused(load_plugin, tmp_path, monkeypatch, field, value):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    observed_proof(h, **{"workspace": str(tmp_path), field: value})
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "record", "tool_call_id": "call", "qid": "q0",
            "question": "Which indentation?", "choice": "tabs"}, task_id="task"))
    assert result["recorded"] is False


def test_expired_observation_is_refused(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    observed_proof(h, workspace=str(tmp_path), created=0)
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "record", "tool_call_id": "call", "qid": "q0",
            "question": "Which indentation?", "choice": "tabs"}, task_id="task"))
    assert result["recorded"] is False


def test_ranking_consults_all_choices_once_and_labels_identity(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    captured = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self, size=-1):
            return b'{"pick": "1", "sound_default": "yes"}'

    def fake_urlopen(request, *_a, **_kw):
        captured.append(json.loads(request.data))
        return Reply()

    monkeypatch.setenv("ANSWER_TEST_KEY", "synthetic-key")
    monkeypatch.setattr(h.module.urllib.request, "urlopen", fake_urlopen)
    raw = json.dumps({"clarification_defaults": [
        {"question": "Which indentation?", "choice": "tabs"},
        {"question": "Which indentation?", "choice": "spaces"},
    ]}).encode()
    (tmp_path / "evidence.json").write_bytes(raw)
    reference = {"path": "evidence.json", "sha256": hashlib.sha256(raw).hexdigest()}
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Which indentation?",
            "choices": ["tabs", "spaces"], "evidence": [reference]}, task_id="task"))
    assert result["status"] == "recommended" and result["applied"] is False
    assert len(captured) == 1  # exactly one ranking call
    schema = captured[0]["schema"]
    # ALL options ranked, identity by label; pick "1" maps to spaces, not index 0.
    assert schema["pick"]["labels"] == {"0": "tabs", "1": "spaces"}
    assert result["selected"] == "spaces"


def test_existing_repo_config_file_supports_default_without_hash(load_plugin, tmp_path, monkeypatch):
    """Ordinary .editorconfig content (no sidecar hashes, no new DB)."""
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / ".editorconfig").write_text(
        "root = true\n\n[*]\nindent_style = tab\n")
    monkeypatch.setattr(h.module, "rank",
                        lambda q, choices, candidates, proofs, *a: candidates[0])
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Which indentation style?",
            "choices": ["tabs", "spaces"],
            "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
    assert result["status"] == "recommended"
    assert result["selected"] == "tabs"
    assert len(result["evidence"]) == 1 and len(result["evidence"][0]["sha256"]) == 64


def test_conditional_conflicting_and_unset_values_abstain(load_plugin, tmp_path, monkeypatch):
    for body in ("root = true\n\n[*.py]\nindent_style = tab\n",
                 "[*]\nindent_style = tab\n[*.js]\nindent_style = space\n",
                 "[*]\nindent_style =\n"):
        agent = native_agent()
        h = setup(load_plugin, monkeypatch, agent)
        (tmp_path / ".editorconfig").write_text(body)
        monkeypatch.setattr(h.module, "rank", lambda *a: "tabs")
        with bind_agent(agent, "task", tmp_path):
            result = json.loads(h.tool("clarification_defaults", {
                "action": "recommend", "question": "Which indentation style?",
                "choices": ["tabs", "spaces"],
                "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
        assert result["status"] == "abstained", body


def test_rank_receives_actual_key_value_scope_evidence(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / ".editorconfig").write_text("[*]\nindent_style = tab\n")
    seen = []
    def fake_rank(q, choices, candidates, proofs, *a):
        seen.append((q, list(choices), list(candidates), proofs))
        return candidates[0]
    monkeypatch.setattr(h.module, "rank", fake_rank)
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Which indentation style?",
            "choices": ["tabs", "spaces"],
            "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
    assert result["selected"] == "tabs"
    q, choices, candidates, proofs = seen[0]
    assert choices == ["tabs", "spaces"] and candidates == ["tabs"]
    assert proofs[0]["source"] == [{"path": ".editorconfig", "key": "indent_style",
                                    "value": "tab", "scope": "repo-wide"}]


def test_crlf_never_evidence_for_lf(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / ".editorconfig").write_text("[*]\nend_of_line = crlf\n")
    monkeypatch.setattr(h.module, "rank",
                        lambda q, c, cand, p, *a: cand[0])
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Which line endings?",
            "choices": ["LF", "CRLF"],  # display case; identity is crlf vs lf
            "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
    # Exact normalized identity: CRLF content can only ever support CRLF.
    assert result["selected"] == "CRLF"


def test_invalid_editorconfig_values_are_not_evidence(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    # 'tabs'/'spaces' are display labels, not valid editorconfig values.
    (tmp_path / ".editorconfig").write_text("[*]\nindent_style = tabs\n")
    monkeypatch.setattr(h.module, "rank", lambda *a: "tabs")
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Which indentation style?",
            "choices": ["tabs", "spaces"],
            "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
    assert result["status"] == "abstained"


def test_width_values_match_only_width_domains(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / ".editorconfig").write_text("[*]\nindent_size = 4\n")
    monkeypatch.setattr(h.module, "rank",
                        lambda q, c, cand, p, *a: cand[0])
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Indentation width value?",
            "choices": ["2", "4"],
            "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
    assert result["selected"] == "4"
    # Garbage numeric field values never qualify.
    (tmp_path / ".editorconfig").write_text("[*]\nindent_size = tab\n")
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Indentation width value?",
            "choices": ["2", "4"],
            "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
    assert result["status"] == "abstained"


def test_style_and_size_domains_never_cross(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / ".editorconfig").write_text("[*]\nindent_style = tab\n")
    monkeypatch.setattr(h.module, "rank", lambda *a: "tabs")
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Indentation width value?",
            "choices": ["2", "4"],
            "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
    # style=tab says nothing about a numeric width; abstain, never invent.
    assert result["status"] == "abstained"


def test_nested_irrelevant_keys_are_not_evidence(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / "config.json").write_text(
        '{"conventions": {"indent_style": "tabs"}, "indent_style": "nested-not-top-level"}')
    monkeypatch.setattr(h.module, "rank", lambda *a: "tabs")
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Which indentation style?",
            "choices": ["tabs", "spaces"],
            "evidence": [{"path": "config.json"}]}, task_id="task"))
    # The top-level scalar says nested-not-top-level... which matches no
    # choice; only nested tables say tabs, and nested data is never evidence.
    assert result["status"] == "abstained"


def test_config_without_supporting_value_abstains(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / ".editorconfig").write_text(
        "root = true\n\n[*]\nindent_style = space\n")
    monkeypatch.setattr(h.module, "rank", lambda *a: "tabs")
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Which indentation style?",
            "choices": ["tabs", "spaces"],
            "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
    assert result["status"] == "abstained"  # content, not metadata, must support the choice


def test_stale_evidence_hash_is_rejected(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    raw = json.dumps({"clarification_defaults": [
        {"question": "Which indentation?", "choice": "tabs"}]}).encode()
    (tmp_path / "evidence.json").write_bytes(raw)
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Which indentation?",
            "choices": ["tabs", "spaces"],
            "evidence": [{"path": "evidence.json", "sha256": "0" * 64}]}, task_id="task"))
    assert result["status"] == "abstained"


def test_secret_bearing_evidence_never_serves_default(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / "evidence.json").write_bytes(b'{"token":"super-secret-token"}')
    import agent.redact as native_redact
    monkeypatch.setattr(native_redact, "redact_for_egress", lambda text: "[redacted]")
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Which indentation?",
            "choices": ["tabs", "spaces"],
            "evidence": [{"path": "evidence.json",
                          "sha256": hashlib.sha256((tmp_path / "evidence.json").read_bytes()).hexdigest()}]},
            task_id="task"))
    assert result["status"] == "abstained"


def test_genuine_timeout_applies_recommendation_legacy_batch_advisory(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    monkeypatch.setattr(h.module, "rank", lambda *a: "tabs")
    raw = json.dumps({"clarification_defaults": [
        {"question": "Which indentation?", "choice": "tabs"}]}).encode()
    (tmp_path / "evidence.json").write_bytes(raw)
    reference = {"path": "evidence.json", "sha256": hashlib.sha256(raw).hexdigest()}
    with bind_agent(agent, "task", tmp_path):
        recommended = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Which indentation?",
            "choices": ["tabs", "spaces"], "evidence": [reference]}, task_id="task"))
        assert recommended["status"] == "recommended"
        result = json.loads(h.middleware(
            "tool_execution", tool_name="clarify",
            args={"questions": [{"question": "Which indentation?", "choices": ["tabs", "spaces"]}]},
            session_id="owner", turn_id="turn", task_id="task",
            next_call=lambda _a: json.dumps({
                "responses": [{"question": "Which indentation?", "choices_offered": ["tabs", "spaces"],
                               "user_response": "", "response_status": "timed_out"}], "timed_out": True})))
    assert result["automatic_answers"][0]["applied"] is True
    with bind_agent(agent, "task", tmp_path):
        legacy = json.loads(h.middleware(
            "tool_execution", tool_name="clarify",
            args={"questions": [{"question": "Which indentation?", "choices": ["tabs", "spaces"]}]},
            session_id="owner", turn_id="turn", task_id="task",
            next_call=lambda _a: json.dumps({
                "responses": [{"question": "Which indentation?", "choices_offered": ["tabs", "spaces"],
                               "user_response": ""}], "timed_out": True})))
    assert legacy["automatic_answers"][0]["applied"] is False  # legacy ambiguity stays advisory


def test_human_answer_wins_and_skip_never_applied(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    monkeypatch.setattr(h.module, "rank", lambda *a: "tabs")
    raw = json.dumps({"clarification_defaults": [
        {"question": "Which indentation?", "choice": "tabs"}]}).encode()
    (tmp_path / "evidence.json").write_bytes(raw)
    with bind_agent(agent, "task", tmp_path):
        h.tool("clarification_defaults", {"action": "recommend", "question": "Which indentation?",
                                          "choices": ["tabs", "spaces"],
                                          "evidence": [{"path": "evidence.json",
                                                        "sha256": hashlib.sha256(raw).hexdigest()}]},
               task_id="task")
        result = json.loads(h.middleware(
            "tool_execution", tool_name="clarify",
            args={"question": "Which indentation?", "choices": ["tabs", "spaces"]},
            session_id="owner", turn_id="turn", task_id="task",
            next_call=lambda _a: json.dumps({"question": "Which indentation?",
                                            "choices_offered": ["tabs", "spaces"],
                                            "user_response": "spaces", "response_status": "answered"})))
    assert result.get("automatic_answers", []) == []
    assert result["user_response"] == "spaces"
    # explicit blank stays skipped
    with bind_agent(agent, "task", tmp_path):
        skipped = json.loads(h.middleware(
            "tool_execution", tool_name="clarify",
            args={"questions": [{"question": "Which indentation?", "choices": ["tabs", "spaces"]}]},
            session_id="owner", turn_id="turn", task_id="task",
            next_call=lambda _a: json.dumps({
                "responses": [{"question": "Which indentation?", "choices_offered": ["tabs", "spaces"],
                               "user_response": "", "response_status": "skipped"}], "timed_out": False})))
    assert skipped.get("automatic_answers", []) == []


@pytest.mark.parametrize("name,content,question,choices,selection", [
    (".editorconfig", "[*]\nindent_size = -4\n", "Which indentation size?", ["2", "4"], "4"),
    (".editorconfig", "[*]\nindent_style = sp-ace\n", "Which indentation style?", ["tabs", "spaces"], "spaces"),
    (".editorconfig", '[*]\nindent_style = "tab"\n', "Which indentation style?", ["tabs", "spaces"], "tabs"),
    (".editorconfig", "[*]\nindent_size = 0\n", "Which indentation size?", ["2", "4"], "4"),
    ("config.json", '{"indent_size": true}', "Which indentation size?", ["1", "4"], "1"),
    ("config.json", '{"indent_size": -4}', "Which indentation size?", ["2", "4"], "4"),
    ("config.json", '{"indent_size": 4.0}', "Which indentation size?", ["2", "4"], "4"),
    ("config.toml", 'indent_style = "sp-ace"\n', "Which indentation style?", ["tabs", "spaces"], "spaces"),
    ("config.toml", "indent_size = -4\n", "Which indentation size?", ["2", "4"], "4"),
    ("config.json", '{"end_of_line": "c-r-l-f"}', "Which line ending?", ["LF", "CRLF"], "CRLF"),
])
def test_invalid_raw_config_cannot_recommend_or_apply(
        load_plugin, tmp_path, monkeypatch, name, content, question, choices, selection):
    from tools.clarify_tool import clarify_tool, TIMEOUT_RESPONSE
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / name).write_text(content)
    monkeypatch.setattr(h.module, "rank", lambda *a: selection)
    request = {"question": question, "choices": choices}
    raw = clarify_tool(**request, callback=lambda *_: TIMEOUT_RESPONSE)
    with bind_agent(agent, "task", tmp_path):
        recommendation = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", **request, "evidence": [{"path": name}]},
            task_id="task"))
        result = h.middleware("tool_execution", tool_name="clarify", args=request,
                              session_id="owner", turn_id="turn", task_id="task",
                              next_call=lambda _: raw)
    assert recommendation["status"] == "abstained"
    assert result == raw


def test_leading_whitespace_valid_json_remains_evidence(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / "config.json").write_text(
        '\n{"indent_style": "tab"}')
    monkeypatch.setattr(h.module, "rank",
                        lambda q, c, cand, p, *a: cand[0])
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Which indentation style?",
            "choices": ["tabs", "spaces"],
            "evidence": [{"path": "config.json"}]}, task_id="task"))
    assert result["status"] == "recommended"
    assert result["selected"] == "tabs"


def test_negative_and_punctuation_choices_never_match_positive_width(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / ".editorconfig").write_text("[*]\nindent_size = 4\n")
    for invalid in ("-4", "-04", "4_000"):
        monkeypatch.setattr(h.module, "rank", lambda *a: invalid)
        with bind_agent(agent, "task", tmp_path):
            result = json.loads(h.tool("clarification_defaults", {
                "action": "recommend", "question": "Which indentation size?",
                "choices": ["-4", "-04", "4_000", "2"],
                "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
        assert result["status"] == "abstained", invalid


def test_ordinary_numeric_and_display_aliases_still_work(load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / ".editorconfig").write_text(
        "[*]\nindent_size = 4\ntab_width = 2\nindent_style = tab\nend_of_line = crlf\n")
    cases = (
        ("Indentation width value?", ["2", "4"], "4"),
        ("Which indentation tab width?", ["2", "4"], "2"),
        ("Which indentation style?", ["Tabs", "Spaces"], "Tabs"),
        ("Which line endings?", ["CRLF", "LF"], "CRLF"),
    )
    for question, choices, selected in cases:
        monkeypatch.setattr(h.module, "rank",
                            lambda q, c, cand, p, *a: cand[0])
        with bind_agent(agent, "task", tmp_path):
            result = json.loads(h.tool("clarification_defaults", {
                "action": "recommend", "question": question, "choices": choices,
                "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
        assert result["status"] == "recommended", question
        assert result["selected"] == selected, question


@pytest.mark.parametrize("name,content", [
    (".editorconfig", "[*]\nindent_size = 04\n"),
    ("config.json", '{"indent_size": "04"}'),
    ("config.json", '{"indent_size": 04}'),
])
def test_leading_zero_width_is_explicitly_invalid(name, content, load_plugin, tmp_path, monkeypatch):
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    (tmp_path / name).write_text(content)
    monkeypatch.setattr(h.module, "rank", lambda *a: "4")
    with bind_agent(agent, "task", tmp_path):
        result = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", "question": "Indentation width value?",
            "choices": ["04", "4"], "evidence": [{"path": name}]}, task_id="task"))
    assert result["status"] == "abstained"


@pytest.mark.parametrize("mutation_phase", ["during_ranking", "during_clarification"])
def test_changed_evidence_cannot_supply_a_timeout_default(
        load_plugin, tmp_path, monkeypatch, mutation_phase):
    from tools.clarify_tool import clarify_tool, TIMEOUT_RESPONSE
    agent = native_agent()
    h = setup(load_plugin, monkeypatch, agent)
    config = tmp_path / ".editorconfig"
    config.write_text("[*]\nindent_size = 4\n")

    def rank_supported_choice(*args):
        if mutation_phase == "during_ranking":
            config.write_text("[*]\nindent_size = 8\n")
        return "4"

    def timeout_after_change(*args):
        config.write_text("[*]\nindent_size = 8\n")
        return TIMEOUT_RESPONSE

    monkeypatch.setattr(h.module, "rank", rank_supported_choice)
    request = {"question": "Which indentation size?", "choices": ["4", "2"]}
    native_results = []

    def clarify(args):
        raw = clarify_tool(**args, callback=timeout_after_change)
        native_results.append(raw)
        return raw

    with bind_agent(agent, "task", tmp_path):
        recommendation = json.loads(h.tool("clarification_defaults", {
            "action": "recommend", **request,
            "evidence": [{"path": ".editorconfig"}]}, task_id="task"))
        if mutation_phase == "during_ranking":
            assert recommendation["status"] == "abstained"
        else:
            assert recommendation["status"] == "recommended"
            assert recommendation["selected"] == "4"
        result = h.middleware("tool_execution", tool_name="clarify", args=request,
                              session_id="owner", turn_id="turn", task_id="task",
                              next_call=clarify)
    assert result == native_results[0]
