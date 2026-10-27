import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.subagent_lifecycle import bind_subagent_parent

CAPTURES = Path("/var/lib/hermes/projects/droid/notes/captured/classifiers")


class Reply:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self, size=-1):
        return json.dumps(self.body).encode()


def choice_body(got):
    """A multi-label reply (`{"verdict": ["B"]}`) meant the service had applied the
    head's own `cls_threshold`, so it stands for "above threshold" here: P(B)=1.0,
    and an absent label is P(B)=0.0. An envelope from `choice` is served untouched."""
    verdict = got.get("verdict") if isinstance(got, dict) else None
    if not isinstance(verdict, list):
        return got  # an explicit `choice` envelope, or a body meant to be unusable
    yes = "B" in verdict
    return {"model": "fake", "usage": {},
            "answers": {"verdict": {"type": "choice", "choice": "B" if yes else "S",
                                    "probabilities": {"B": 1.0 if yes else 0.0,
                                                      "S": 0.0 if yes else 1.0},
                                    "confidence": 1.0}}}


def setup(load_plugin, monkeypatch, answers=None, **settings):
    """Load the plugin with a head-dispatching fake classifier.

    `answers` maps a head name present in a `/classify` schema or a `/v1/systemone`
    question to the reply body. A reply of Exception means the service is
    unreachable for that call.
    """
    h = load_plugin("meta-classifier", {"api_key_env": "META_TEST_KEY", **settings})
    answers = answers or {}
    requests = []

    def fake_urlopen(request, *_a, **_kw):
        raw = json.loads(request.data)
        requests.append(raw)
        heads = raw["schema"] if "schema" in raw else raw["questions"]
        for head, body in answers.items():
            if head in heads:
                if isinstance(body, Exception):
                    raise body
                body = body(raw) if callable(body) else body
                return Reply(body if "schema" in raw else choice_body(body))
        raise AssertionError(f"unexpected classifier heads {sorted(heads)}")

    monkeypatch.setenv("META_TEST_KEY", "synthetic-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    h.requests = requests
    return h


def local_agent():
    from run_agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    agent._interrupt_requested = False
    return agent


def workspace_for(task_id, cwd):
    from tools.terminal_tool import register_task_env_overrides
    register_task_env_overrides(task_id, {"cwd": str(cwd)})


def screen(h, task_id, tool, args, tool_call_id="c1", session_id="s1"):
    with bind_subagent_parent(local_agent()):
        return h.hook("pre_tool_call", tool_name=tool, task_id=task_id, tool_call_id=tool_call_id,
                      session_id=session_id, args=args)


# ---------------------------------------------------------------- execute risk
def test_high_tier_requires_approval_with_droid_criteria(load_plugin, monkeypatch, tmp_path):
    h = setup(load_plugin, monkeypatch, {"riskLevel": {"riskLevel": "high"}})
    workspace_for("t-risk", tmp_path)
    directive, = screen(h, "t-risk", "terminal", {"command": "git push origin HEAD --token abc123secret"})
    assert directive["action"] == "approve"
    assert directive["rule_key"].startswith("meta-classifier:terminal:")
    assert "riskLevel=high" in directive["message"]
    request = h.requests[-1]
    labels = request["schema"]["riskLevel"]["labels"]
    assert list(labels) == ["low", "medium", "high"]
    assert labels["high"].startswith("Commands with a credible possibility of severe")
    state = json.loads(request["text"])
    assert state["pendingCommand"]["tool"] == "terminal"
    assert "abc123secret" not in request["text"]
    assert "[redacted]" in state["pendingCommand"]["arguments"]["command"]


def test_low_tier_on_incomplete_context_stays_unknown(load_plugin, monkeypatch, tmp_path):
    h = setup(load_plugin, monkeypatch, {"riskLevel": {"riskLevel": "low"}})
    workspace_for("t-low", tmp_path)
    result, = screen(h, "t-low", "terminal", {"command": "ls"})
    assert result["risk_review"]["status"] == "unknown"
    assert result["risk_review"]["reason"] == "incomplete_context"
    assert result["risk_review"]["riskLevel"] == "low"


def test_luna_scale_and_deny_policy(load_plugin, monkeypatch, tmp_path):
    h = setup(load_plugin, monkeypatch, {"riskLevel": {"riskLevel": "medium"}},
              risk_scale="luna", approve_from="medium", policy="deny")
    workspace_for("t-luna", tmp_path)
    directive, = screen(h, "t-luna", "terminal", {"command": "mkdir out"})
    assert directive["action"] == "block" and "revalidate" not in directive
    assert list(h.requests[-1]["schema"]["riskLevel"]["labels"]) == ["off", "low", "medium", "high"]


def test_transcript_tail_rides_as_redacted_evidence(load_plugin, monkeypatch, tmp_path):
    h = setup(load_plugin, monkeypatch, {"riskLevel": {"riskLevel": "medium"}})
    workspace_for("t-tr", tmp_path)
    h.hook("pre_llm_call", session_id="s-ev", conversation_history=[
        {"role": "user", "content": "token is sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "terminal", "arguments": "{\"command\": \"git status\"}"}}]},
        {"role": "tool", "content": "clean"}])
    screen(h, "t-tr", "terminal", {"command": "git status"}, session_id="s-ev")
    state = json.loads(h.requests[-1]["text"])
    rendered = state["sessionTranscript"]["rendered"]
    assert "USER [id=0]" in rendered and "TOOL_CALL terminal" in rendered and "TOOL_RESULT" in rendered
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef" not in rendered


def test_unavailable_classifier_is_unscreened(load_plugin, monkeypatch, tmp_path):
    h = setup(load_plugin, monkeypatch, {"riskLevel": OSError("down")})
    workspace_for("t-down", tmp_path)
    result, = screen(h, "t-down", "terminal", {"command": "ls"})
    assert result["risk_review"]["reason"] == "classifier_unavailable_or_invalid"


# ---------------------------------------------------------------- droid shield
git = pytest.mark.skipif(shutil.which("git") is None, reason="git required")
SECRET_LINE = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY99"


def stage(repo, name, text):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "HOME": str(repo)}
    if not (repo / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / name).write_text(text)
    subprocess.run(["git", "add", name], cwd=repo, check=True, env=env)


def choice(probability, argmax=None):
    """A `/v1/systemone` choice envelope. `argmax` may disagree with the gate on
    purpose: the plugin compares P(B) with the threshold and never the label."""
    return {"model": "fake", "usage": {},
            "answers": {"verdict": {"type": "choice",
                                    "choice": argmax or ("B" if probability >= 0.5 else "S"),
                                    "probabilities": {"B": probability, "S": 1.0 - probability},
                                    "confidence": max(probability, 1.0 - probability)}}}


def shield_lines(h):
    """The rows the shield recorded: each line's P(B) beside the threshold used."""
    path = h.home / "logs" / "meta-classifier-signals.jsonl"
    return [row for line in path.read_text().splitlines() for row in json.loads(line).get("lines", [])]


@git
def test_shield_blocks_likely_real_secret_and_never_sends_it(load_plugin, monkeypatch, tmp_path):
    h = setup(load_plugin, monkeypatch, {"verdict": {"verdict": ["B"]}, "riskLevel": {"riskLevel": "medium"}})
    stage(tmp_path, "deploy.env", f"# deploy\n{SECRET_LINE}\n")
    workspace_for("t-sh", tmp_path)
    directive, = screen(h, "t-sh", "terminal", {"command": "git commit -m 'add env'"})
    assert directive["action"] == "block"
    assert "deploy.env:2" in directive["message"]
    shield_request, = [r for r in h.requests if "questions" in r]
    question = shield_request["questions"]["verdict"]
    assert question["type"] == "choice" and set(question["criteria"]) == {"S", "B"}
    payload = json.loads(shield_request["state"])
    assert payload["extension"] == "env" and payload["focus_line"] == 1
    assert "wJalrXUtnFEMI" not in shield_request["state"]
    row, = shield_lines(h)
    assert row == {"location": "deploy.env:2", "kind": "downgrade", "probability": 1.0,
                   "threshold_used": h.module.schemas.SHIELD["downgrade"]["threshold"],
                   "decision": "block"}
    assert not [r for r in h.requests if "riskLevel" in r.get("schema", {})], "blocked before the risk head"


@git
def test_shield_false_positive_warns_via_approval(load_plugin, monkeypatch, tmp_path):
    h = setup(load_plugin, monkeypatch, {"verdict": {"verdict": []}, "riskLevel": {"riskLevel": "medium"}})
    stage(tmp_path, "deploy.env", f"{SECRET_LINE}\n")
    workspace_for("t-warn", tmp_path)
    directive, = screen(h, "t-warn", "terminal", {"command": "git commit -am x"})
    assert directive["action"] == "approve"
    assert directive["rule_key"].startswith("meta-classifier:shield:")
    assert "deploy.env:1" in directive["message"]


@git
def test_shield_fails_closed_when_classifier_down(load_plugin, monkeypatch, tmp_path):
    h = setup(load_plugin, monkeypatch, {"verdict": OSError("down")})
    stage(tmp_path, "deploy.env", f"{SECRET_LINE}\n")
    workspace_for("t-fc", tmp_path)
    directive, = screen(h, "t-fc", "terminal", {"command": "git commit -m x"})
    assert directive["action"] == "block"


@git
def test_shield_risk_context_warns_only_on_b(load_plugin, monkeypatch, tmp_path):
    verdicts = iter([{"verdict": []}, {"verdict": ["B"]}])
    h = setup(load_plugin, monkeypatch, {"verdict": lambda _r: next(verdicts),
                                         "riskLevel": {"riskLevel": "low"}})
    stage(tmp_path, "notes.txt", "# token rotation reminder for ops\n")
    workspace_for("t-rk", tmp_path)
    first, = screen(h, "t-rk", "terminal", {"command": "git commit -m notes"})
    assert first["risk_review"]["reason"] == "incomplete_context"  # risk S: nothing to say
    row, = shield_lines(h)  # the number and the threshold it was judged against
    assert row["kind"] == "risk" and row["probability"] == 0.0
    assert row["threshold_used"] == h.module.schemas.SHIELD["risk"]["threshold"] == 0.95
    second, = screen(h, "t-rk", "terminal", {"command": "git commit -m notes"}, tool_call_id="c2")
    assert second["action"] == "approve" and "possible hardcoded credential" in second["message"]


@git
@pytest.mark.parametrize("offset,expected", [(-0.001, "approve"), (0.001, "block")])
def test_shield_probability_gate_is_the_configured_threshold(load_plugin, monkeypatch, tmp_path,
                                                            offset, expected):
    """P(B) just under the threshold approves, just over it blocks. The threshold is
    the constant the multi-label head was handed, not a second number: the service
    no longer applies it, so the plugin compares the returned probability with it."""
    h = setup(load_plugin, monkeypatch, {"verdict": lambda _r: choice(threshold + offset),
                                         "riskLevel": {"riskLevel": "medium"}})
    threshold = h.module.schemas.SHIELD["downgrade"]["threshold"]
    stage(tmp_path, "deploy.env", f"{SECRET_LINE}\n")
    workspace_for("t-gate", tmp_path)
    directive, = screen(h, "t-gate", "terminal", {"command": "git commit -m x"})
    assert directive["action"] == expected
    row, = [r for r in shield_lines(h) if r["kind"] == "downgrade"]
    assert row["probability"] == pytest.approx(threshold + offset)
    assert row["threshold_used"] == threshold
    assert row["decision"] == ("block" if expected == "block" else "warn")


@git
def test_shield_approves_the_documented_aws_example_key(load_plugin, monkeypatch, tmp_path):
    """AKIAIOSFODNN7EXAMPLE is AWS's own documentation key. Measured on the deployed
    engine (2026-10-27) the choice question answers S with P(B)=0.0513, so the line
    is only warned about and the commit goes ahead. The multi-label path blocked it:
    it asked for P(S) and P(B) independently, and both cleared 0.3775 at once, while
    a choice question normalises across the two options. Deliberate precision gain on
    a security gate, and the recorded row keeps the class auditable."""
    h = setup(load_plugin, monkeypatch, {"verdict": choice(0.0513, "S"),
                                         "riskLevel": {"riskLevel": "medium"}})
    stage(tmp_path, "deploy.env", "AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE\n")
    workspace_for("t-aws", tmp_path)
    directive, = screen(h, "t-aws", "terminal", {"command": "git commit -m x"})
    assert directive["action"] == "approve"
    assert "possible hardcoded credential" in directive["message"]
    row, = shield_lines(h)
    assert row["probability"] == 0.0513 and row["decision"] == "warn"


@git
def test_shield_blocks_on_probability_not_on_the_argmax_answer(load_plugin, monkeypatch, tmp_path):
    """The engine's own choice is never consulted. Measured JWT: `S` wins the argmax
    but P(B)=0.4688, which is over 0.3775, so the shield still blocks."""
    h = setup(load_plugin, monkeypatch, {"verdict": choice(0.4688, "S"),
                                         "riskLevel": {"riskLevel": "medium"}})
    stage(tmp_path, "deploy.env", "TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
                                  ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
                                  ".dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk\n")
    workspace_for("t-jwt", tmp_path)
    directive, = screen(h, "t-jwt", "terminal", {"command": "git commit -m x"})
    assert directive["action"] == "block"
    assert "deploy.env:1" in directive["message"]
    row, = shield_lines(h)
    assert row["probability"] == 0.4688 and row["decision"] == "block"


@git
def test_shield_fails_closed_on_an_unusable_answer(load_plugin, monkeypatch, tmp_path):
    """Every way an answer can fail to be a number is the same thing to the gate: no
    number, so the line blocks. A transport error is the same path (see
    test_shield_fails_closed_when_classifier_down); no key is the same path without
    a call, checked last."""
    for unusable in ({"model": "fake"}, {}, {"answers": None}, {"answers": {"verdict": "B"}},
                     {"answers": {"verdict": {"probabilities": {"B": "0.9"}}}},
                     {"answers": {"verdict": {"probabilities": {"B": 4.0}}}},
                     {"answers": {"verdict": {"probabilities": {}}}}):
        h = setup(load_plugin, monkeypatch, {"verdict": unusable, "riskLevel": {"riskLevel": "low"}})
        stage(tmp_path, "deploy.env", f"{SECRET_LINE}\n")
        workspace_for("t-bad", tmp_path)
        directive, = screen(h, "t-bad", "terminal", {"command": "git commit -m x"})
        assert directive["action"] == "block", unusable
        assert shield_lines(h)[-1] == {"location": "deploy.env:1", "kind": "downgrade",
                                       "probability": None,
                                       "threshold_used": h.module.schemas.SHIELD["downgrade"]["threshold"],
                                       "decision": "block"}
    h = setup(load_plugin, monkeypatch, {"riskLevel": {"riskLevel": "low"}})
    monkeypatch.delenv("META_TEST_KEY")  # after load: the client reads the key per call
    workspace_for("t-nokey", tmp_path)
    directive, = screen(h, "t-nokey", "terminal", {"command": "git commit -m x"})
    assert directive["action"] == "block"
    assert not [r for r in h.requests if "questions" in r]


@git
def test_shield_ignores_clean_diff_and_non_git_push_words(load_plugin, monkeypatch, tmp_path):
    h = setup(load_plugin, monkeypatch, {"riskLevel": {"riskLevel": "low"}})
    stage(tmp_path, "readme.md", "hello\n")
    workspace_for("t-clean", tmp_path)
    screen(h, "t-clean", "terminal", {"command": "git commit -m hi"})
    screen(h, "t-clean", "terminal", {"command": "git log | grep push"})
    assert not [r for r in h.requests if "questions" in r], "nothing to screen"
    assert all("riskLevel" in r["schema"] for r in h.requests)


@git
def test_shield_screens_worktree_changes_that_commit_stages_later(load_plugin, monkeypatch, tmp_path):
    """`git commit -a`/pathspec stage at commit time, so the index is still empty here."""
    h = setup(load_plugin, monkeypatch, {"verdict": {"verdict": ["B"]},
                                         "riskLevel": {"riskLevel": "medium"}})
    stage(tmp_path, "tracked.txt", "clean\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x", "HOME": str(tmp_path)}
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True, env=env)
    (tmp_path / "tracked.txt").write_text(f"clean\n{SECRET_LINE}\n")  # edited, never staged
    workspace_for("t-wt", tmp_path)
    directive, = screen(h, "t-wt", "terminal", {"command": "git commit -am 'edit'"})
    assert directive["action"] == "block"
    assert "tracked.txt:2" in directive["message"]


@git
def test_shield_refuses_a_push_whose_range_cannot_be_determined(load_plugin, monkeypatch, tmp_path):
    """Unpushed commits with nothing staged must not read as 'nothing to screen'."""
    h = setup(load_plugin, monkeypatch, {"verdict": {"verdict": ["B"]}})
    stage(tmp_path, "deploy.env", f"{SECRET_LINE}\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x", "HOME": str(tmp_path)}
    subprocess.run(["git", "commit", "-qm", "secret"], cwd=tmp_path, check=True, env=env)
    # A remote that was never fetched: no remote-tracking ref, no upstream, so the publish
    # range is genuinely unknowable. The shield must refuse instead of passing silently.
    subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/x.git"],
                   cwd=tmp_path, check=True, env=env)
    workspace_for("t-p2", tmp_path)
    directive, = screen(h, "t-p2", "terminal", {"command": "git push origin master"})
    assert directive["action"] == "block"
    assert "cannot determine" in directive["message"]


def test_shield_contexts_from_diff(load_plugin):
    h = load_plugin("meta-classifier")
    diff = ("diff --git a/s.sh b/s.sh\n--- a/s.sh\n+++ b/s.sh\n@@ -1,2 +1,4 @@\n # setup\n"
            f"+export {SECRET_LINE}\n+# api_key placeholder below\n echo done\n")
    contexts = h.module.shield_contexts(diff)
    assert [(c["kind"], c["location"]) for c in contexts] == [("downgrade", "s.sh:2"), ("risk", "s.sh:3")]
    payload = contexts[0]["payload"]
    assert payload["extension"] == "sh" and len(payload["lines"]) == 3 and payload["focus_line"] == 1
    assert "wJalrXUtnFEMI" not in json.dumps(payload)


# ---------------------------------------------------------------- local signals
LABELS = {"primaryIntent": "debugging", "intentSuccess": "Complete", "requestFulfillment": "Complete",
          "processCompliance": "OK", "evidenceConfidence": "High", "documentsWorkedOn": [],
          "frictionPresent": "no", "frictionCategory": [], "frictionSeverity": "Low",
          "expertiseLevel": "advanced", "successSignalStrength": "4", "failureSignalStrength": "0"}
HISTORY = [{"role": "user", "content": "why does pytest hang?"},
           {"role": "assistant", "content": "A socket is left open; fixed in conftest."}]


def test_signals_classified_once_per_unchanged_window(load_plugin, monkeypatch):
    h = setup(load_plugin, monkeypatch, {"primaryIntent": LABELS}, signals=True,
              signals_triggers=["turn_end", "session_end"])
    h.hook("pre_llm_call", session_id="sig", conversation_history=HISTORY)
    h.hook("on_session_end", session_id="sig", completed=True)
    h.hook("on_session_end", session_id="sig", completed=True)  # unchanged: skipped
    h.hook("on_session_finalize", session_id="sig", reason="session_boundary")  # unchanged: skipped
    rows = [json.loads(line) for line in
            (h.home / "logs" / "meta-classifier-signals.jsonl").read_text().splitlines()]
    assert [r["triggerReason"] for r in rows] == ["turn_end"]
    assert rows[0]["classification"] == LABELS and rows[0]["messageRange"] == {"count": 2}
    request = h.requests[-1]
    assert len(request["schema"]) == 12
    assert request["schema"]["frictionCategory"]["multi_label"] is True
    assert "USER [id=0]: why does pytest hang?" in request["text"]


def test_signals_garbage_reply_is_an_error_not_defaults(load_plugin, monkeypatch):
    h = setup(load_plugin, monkeypatch, {"primaryIntent": ["not", "a", "dict"]}, signals=True)
    h.hook("pre_llm_call", session_id="g", conversation_history=HISTORY)
    h.hook("on_session_reset", session_id="g", reason="new_session")
    row, = [json.loads(line) for line in
            (h.home / "logs" / "meta-classifier-signals.jsonl").read_text().splitlines()]
    assert row["error"] == "classifier_unavailable_or_invalid" and "classification" not in row


def test_session_reset_forgets_the_outgoing_sessions_transcript(load_plugin, monkeypatch):
    """The host rotates with old_session_id; keying on previous_session_id kept the outgoing
    transcript forever, so a later turn_end still classified a session that had ended."""
    h = setup(load_plugin, monkeypatch, {"primaryIntent": LABELS}, signals=True,
              signals_triggers=["turn_end"])
    h.hook("pre_llm_call", session_id="sess-old", conversation_history=HISTORY)
    h.hook("on_session_reset", session_id="sess-new", reason="new_session", platform="cli",
           old_session_id="sess-old", new_session_id="sess-new")
    h.hook("on_turn_end", session_id="sess-old")  # the dead session must find no transcript
    path = h.home / "logs" / "meta-classifier-signals.jsonl"
    assert not path.exists() or not path.read_text().strip(), path.read_text()


def test_signals_off_by_default(load_plugin, monkeypatch):
    h = setup(load_plugin, monkeypatch)
    h.hook("pre_llm_call", session_id="off", conversation_history=HISTORY)
    h.hook("on_session_finalize", session_id="off")
    assert h.requests == [] and not (h.home / "logs" / "meta-classifier-signals.jsonl").exists()


# ---------------------------------------------------------------- decision_model tool
def test_decision_model_tool_converts_and_answers(load_plugin, monkeypatch):
    h = setup(load_plugin, monkeypatch, {"severity": {"severity": "2", "flaky": "yes", "kind": "bug"}})
    result = json.loads(h.tool("decision_model", {
        "state": {"log": "test failed twice then passed; key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"},
        "questions": {
            "kind": {"type": "choice", "criteria": {"bug": {"definition": "defect"}, "flake": "nondeterminism"}},
            "flaky": {"type": "noul", "criteria": {"true": "passes on retry", "false": "fails every run"}},
            "severity": {"type": "score", "criteria": ["none", "minor", "major"]},
        }}))
    assert result["success"] is True
    assert result["answers"]["kind"] == {"type": "choice", "choice": "bug"}
    assert result["answers"]["flaky"] == {"type": "noul", "noul": 1.0}
    assert result["answers"]["severity"]["score"] == 2 and result["answers"]["severity"]["legend"][2] == "major"
    request = h.requests[-1]
    assert request["schema"]["kind"]["labels"] == {"bug": "defect", "flake": "nondeterminism"}
    assert request["schema"]["severity"]["labels"] == {"0": "none", "1": "minor", "2": "major"}
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef" not in request["text"]


def test_decision_model_rejects_malformed_requests(load_plugin, monkeypatch):
    h = setup(load_plugin, monkeypatch)
    result = json.loads(h.tool("decision_model", {"questions": {"q": {"type": "ranking", "criteria": {}}}}))
    assert result["success"] is False and "unknown type" in result["error"]
    assert h.requests == []


# ---------------------------------------------------------------- replay
captures = pytest.mark.skipif(not CAPTURES.exists(), reason="droid captures not present")


@captures
def test_replay_builds_droid_payloads_with_plugin_schemas(load_plugin, capsys):
    h = load_plugin("meta-classifier")
    payloads = h.module.replay.build_payloads(CAPTURES)
    ids = [p["id"] for p in payloads]
    assert len(payloads) == 13
    assert ids[:3] == ["risk-jev/git-status", "risk-jev/git-push", "risk-luna/git-push"]
    assert ids[-4:] == ["signals/intent_completeness", "signals/friction", "signals/expertise", "router/session-1"]
    by_id = {p["id"]: p for p in payloads}
    assert by_id["risk-jev/git-push"]["expect"] == {"riskLevel": "high"}
    assert by_id["risk-luna/git-push"]["schema"] == h.module.schemas.risk_schema("luna")
    assert by_id["shield/shield-downgrade-0"]["schema"]["verdict"]["cls_threshold"] == 0.3775
    assert json.loads(by_id["shield/shield-downgrade-0"]["text"])["focus_line"] == 1
    assert len(by_id["router/session-1"]["schema"]) == 7
    h.module.replay.dry_run(payloads, print)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 13 and json.loads(lines[0])["textChars"] == len(payloads[0]["text"])


@captures
def test_plugin_risk_criteria_match_captured_jev_request(load_plugin):
    h = load_plugin("meta-classifier")
    captured = json.loads((CAPTURES / "risk/jev-request-git-push.json").read_text())["questions"]["riskLevel"]
    plugin = h.module.schemas.JEV_RISK_QUESTIONS["riskLevel"]
    assert plugin["instructions"] == captured["instructions"]
    for tier, criteria in plugin["criteria"].items():
        assert {k: captured["criteria"][tier][k] for k in criteria} == criteria


def test_replay_run_reports_pass_and_check(load_plugin, monkeypatch, capsys):
    h = setup(load_plugin, monkeypatch, {"riskLevel": {"riskLevel": "high"}})
    payloads = [{"id": "a", "expect": {"riskLevel": "high"}, "text": "x", "schema": {"riskLevel": ["low", "high"]}},
                {"id": "b", "expect": {"riskLevel": "low"}, "text": "y", "schema": {"riskLevel": ["low", "high"]}}]
    h.module.replay.run(payloads, h.module.Classifier(h.context, cooldown=0), print)
    out = capsys.readouterr().out
    assert "a ok PASS" in out and "b ok CHECK" in out
