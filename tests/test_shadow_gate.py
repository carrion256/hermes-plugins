"""Shadow-gate: ledger storage, join law, promotion gates, and both read surfaces.

Every test asserts an observable behaviour (a joined verdict, a counted corrupt
line, a named gate, an exit code) — none of them would pass if the branch it
covers were sabotaged.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

MODULE_NAME = "hermes_plugins_shadow_gate"
BASE = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
# A policy that only passes on real agreement: 0.9 floor, two observations, no
# false positives tolerated above 0.9 (so a single gate can fail at a time).
GOOD_POLICY = {
    "min_pairs": 2,
    "min_agreement": 0.9,
    "max_false_positive": 0.9,
    "positive_verdicts": ["yes"],
    "positive_outcomes": ["ok"],
}


class Clock:
    """Deterministic injected UTC clock; it moves only when a test says so."""

    def __init__(self, start: datetime = BASE) -> None:
        self._instant = start

    def __call__(self) -> datetime:
        return self._instant

    def advance(self, seconds: int) -> None:
        self._instant = self._instant + timedelta(seconds=seconds)


@pytest.fixture
def api(load_plugin) -> ModuleType:
    """The process-wide ledger module, reached the way the plugin reaches it."""
    load_plugin("shadow-gate")
    module = sys.modules[MODULE_NAME]
    assert module.MODULE_NAME == MODULE_NAME
    return module


def _config(values: dict[str, object]) -> Callable[[str, object], object]:
    def get(key: str, default: object) -> object:
        return values.get(key, default)

    return get


def _pair(
    api: ModuleType,
    key: str,
    verdict_value: str,
    outcome_value: str,
    *,
    at: datetime = BASE,
) -> object:
    verdict = api.VerdictRecord(
        decision="d", key=key, input_digest="digest", verdict=verdict_value, recorded_at=at
    )
    outcome = api.OutcomeRecord(
        decision="d", key=key, outcome=outcome_value, recorded_at=at
    )
    return api.Pair(decision="d", key=key, verdict=verdict, outcome=outcome)


def _policy(
    api: ModuleType,
    *,
    min_pairs: int = 1,
    min_agreement: float = 1.0,
    max_false_positive: float = 0.0,
) -> object:
    return api.promotion_policy(
        min_pairs=min_pairs,
        min_agreement=min_agreement,
        max_false_positive=max_false_positive,
        positive_verdicts=["yes"],
        positive_outcomes=["ok"],
    )


def _load_config_plugin(load_plugin, tmp_path: Path, **values: object):
    settings: dict[str, object] = {"ledger_dir": str(tmp_path / "led")}
    settings.update(values)
    return load_plugin("shadow-gate", settings)


def _cli(plugin):
    entry = plugin.manager._cli_commands["shadow-status"]
    parser = argparse.ArgumentParser(prog="shadow-status")
    entry["setup_fn"](parser)
    return entry["handler_fn"], parser


# ----------------------------------------------------------------------- ledger


def test_records_round_trip_through_the_jsonl_file(api: ModuleType, tmp_path: Path) -> None:
    clock = Clock()
    ledger = api.ShadowLedger(tmp_path / "led", now=clock)

    verdict = ledger.record_verdict(
        "route-tier", "child-1", "sha256:aa", "yes", meta={"producer": "subagent-effort"}
    )
    clock.advance(5)
    outcome = ledger.record_outcome("route-tier", "child-1", "ok")

    assert verdict.v == 1 and verdict.recorded_at == BASE
    assert outcome.recorded_at == BASE + timedelta(seconds=5)
    path = tmp_path / "led" / "route-tier.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["kind"] for row in rows] == ["verdict", "outcome"]
    assert rows[0]["meta"] == {"producer": "subagent-effort"}
    assert rows[0]["recorded_at"] == "2026-11-06T12:00:00+00:00"

    view = ledger.snapshot("route-tier")
    assert view.corrupt_lines == 0
    assert len(view.pairs) == 1
    pair = view.pairs[0]
    assert (pair.decision, pair.key) == ("route-tier", "child-1")
    assert pair.verdict.input_digest == "sha256:aa"
    assert dict(pair.verdict.meta) == {"producer": "subagent-effort"}
    assert pair.outcome.outcome == "ok"
    assert pair.verdict.recorded_at.utcoffset() == timedelta(0)
    assert ledger.decisions() == ("route-tier",)


def test_missing_file_reads_as_no_evidence(api: ModuleType, tmp_path: Path) -> None:
    ledger = api.ShadowLedger(tmp_path / "never-written", now=Clock())
    view = ledger.snapshot("route-tier")
    assert (view.pairs, view.unpaired_verdicts, view.unpaired_outcomes) == ((), (), ())
    assert view.corrupt_lines == 0
    assert ledger.decisions() == ()


def test_corrupt_lines_are_skipped_and_counted(
    api: ModuleType, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ledger = api.ShadowLedger(tmp_path / "led", now=Clock())
    ledger.record_verdict("route-tier", "k1", "dig", "yes")
    ledger.record_outcome("route-tier", "k1", "ok")
    path = tmp_path / "led" / "route-tier.jsonl"
    damage = "\n".join(
        [
            "not json at all",
            "",  # blank lines are noise, not corruption
            "[1, 2]",
            json.dumps(
                {
                    "v": 2,
                    "kind": "verdict",
                    "decision": "route-tier",
                    "key": "k2",
                    "input_digest": "dig",
                    "verdict": "yes",
                    "recorded_at": "2026-11-06T12:00:00+00:00",
                }
            ),
            "",
        ]
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(damage)

    view = ledger.snapshot("route-tier")
    assert view.corrupt_lines == 3
    assert len(view.pairs) == 1
    assert (view.unpaired_verdicts, view.unpaired_outcomes) == ((), ())
    assert "corrupt" in caplog.text  # fail-open must be visible, not silent


def test_join_takes_the_latest_verdict_before_each_outcome(
    api: ModuleType, tmp_path: Path
) -> None:
    clock = Clock()
    ledger = api.ShadowLedger(tmp_path / "led", now=clock)
    ledger.record_verdict("d", "a", "dig", "low")
    ledger.record_outcome("d", "a", "ok")  # same instant: file order decides
    clock.advance(1)
    ledger.record_verdict("d", "a", "dig", "high")
    clock.advance(1)
    ledger.record_outcome("d", "a", "bad")
    ledger.record_verdict("d", "b", "dig", "low", recorded_at=clock())
    ledger.record_outcome("d", "b", "ok", recorded_at=clock())
    ledger.record_outcome("d", "c", "ok", recorded_at=BASE)  # nothing precedes it

    pairs = ledger.pairs("d")
    assert [(p.key, p.verdict.verdict, p.outcome.outcome) for p in pairs] == [
        ("a", "low", "ok"),
        ("a", "high", "bad"),
        ("b", "low", "ok"),
    ]
    view = ledger.snapshot("d")
    assert [o.key for o in view.unpaired_outcomes] == ["c"]
    assert view.unpaired_verdicts == ()


def test_superseded_verdict_stays_visible_as_unpaired(api: ModuleType, tmp_path: Path) -> None:
    clock = Clock()
    ledger = api.ShadowLedger(tmp_path / "led", now=clock)
    ledger.record_verdict("d", "a", "dig", "low")
    clock.advance(1)
    ledger.record_verdict("d", "a", "dig", "high")
    clock.advance(1)
    ledger.record_outcome("d", "a", "ok")
    ledger.record_outcome("d", "ghost", "ok")

    view = ledger.snapshot("d")
    assert len(view.pairs) == 1
    assert view.pairs[0].verdict.verdict == "high"
    assert [v.verdict for v in view.unpaired_verdicts] == ["low"]
    assert [o.key for o in view.unpaired_outcomes] == ["ghost"]


def test_rotation_keeps_only_the_newest_rows(api: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "led"
    ledger = api.ShadowLedger(root, now=Clock(), max_rows_per_decision=3)
    for index in range(5):
        ledger.record_verdict("d", f"k{index}", "dig", f"v{index}")

    rows = [
        json.loads(line)
        for line in (root / "d.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["verdict"] for row in rows] == ["v2", "v3", "v4"]
    view = ledger.snapshot("d")
    assert [v.verdict for v in view.unpaired_verdicts] == ["v2", "v3", "v4"]
    assert list(root.iterdir()) == [root / "d.jsonl"]  # atomic replace left no temp file


def test_row_budget_below_one_is_refused(api: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_rows_per_decision"):
        api.ShadowLedger(tmp_path, now=Clock(), max_rows_per_decision=0)


def test_decision_names_cannot_escape_the_ledger_directory(
    api: ModuleType, tmp_path: Path
) -> None:
    root = tmp_path / "led"
    ledger = api.ShadowLedger(root, now=Clock())
    for bad in ("../x", "Upper", "", "a/b", "1abc", ".hidden", "a b", "a\n", "a" * 65):
        with pytest.raises(ValueError, match="decision name"):
            api.validate_decision(bad)
        with pytest.raises(ValueError, match="decision name"):
            ledger.record_verdict(bad, "k", "dig", "yes")
        with pytest.raises(ValueError, match="decision name"):
            ledger.snapshot(bad)
    assert not (tmp_path / "x.jsonl").exists()
    assert not root.exists()


def test_recording_rejects_unusable_inputs(api: ModuleType, tmp_path: Path) -> None:
    ledger = api.ShadowLedger(tmp_path / "led", now=Clock())
    with pytest.raises(ValueError, match="key"):
        ledger.record_verdict("d", "", "dig", "yes")
    with pytest.raises(ValueError, match="input_digest"):
        ledger.record_verdict("d", "k", "", "yes")
    with pytest.raises(ValueError, match="verdict"):
        ledger.record_verdict("d", "k", "dig", "")
    with pytest.raises(ValueError, match="outcome"):
        ledger.record_outcome("d", "k", "")
    with pytest.raises(TypeError, match="meta"):
        ledger.record_verdict("d", "k", "dig", "yes", meta={"count": 1})
    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.record_verdict("d", "k", "dig", "yes", recorded_at=datetime(2026, 11, 6))
    assert not (tmp_path / "led").exists()


def test_shared_ledger_is_one_instance_per_root(api: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "shared"
    first = api.shared_ledger(root)
    assert api.shared_ledger(root) is first
    first.record_verdict("d", "k", "dig", "yes")
    assert len(api.shared_ledger(root).snapshot("d").unpaired_verdicts) == 1
    assert api.shared_ledger(tmp_path / "other") is not first


# -------------------------------------------------------------------- evaluator


def test_evaluate_qualifies_only_when_every_gate_passes(api: ModuleType) -> None:
    policy = _policy(api, min_pairs=2, min_agreement=1.0, max_false_positive=0.0)
    pairs = (_pair(api, "k1", "yes", "ok"), _pair(api, "k2", "yes", "ok"))

    report = api.evaluate(pairs, policy)
    assert report.status == "QUALIFIED" and report.reasons == ()
    assert report.pairs == 2 and report.agreement == 1.0 and report.false_positive_rate == 0.0

    short = api.evaluate(pairs[:1], policy)  # one observation below the floor
    assert short.status == "NOT_QUALIFIED" and short.reasons == ("insufficient_pairs",)


def test_negative_agreement_compares_polarities_not_labels(api: ModuleType) -> None:
    # "no" and "bad" are both non-positive: agreement is (verdict positive) ==
    # (outcome positive), never verdict == outcome.
    report = api.evaluate((_pair(api, "k1", "no", "bad"),), _policy(api, min_pairs=1))
    assert report.status == "QUALIFIED" and report.agreement == 1.0


def test_evaluate_names_each_failed_gate(api: ModuleType) -> None:
    pairs = (_pair(api, "k1", "yes", "ok"), _pair(api, "k2", "yes", "bad"))

    low = api.evaluate(pairs, _policy(api, min_pairs=1, min_agreement=0.9, max_false_positive=0.9))
    assert (low.agreement, low.false_positive_rate) == (0.5, 0.5)
    assert low.reasons == ("low_agreement",)

    # measured agreement exactly at the floor passes; only the FPR gate bites.
    fpr = api.evaluate(pairs, _policy(api, min_pairs=1, min_agreement=0.5, max_false_positive=0.0))
    assert fpr.reasons == ("false_positive_rate",)

    both = api.evaluate(pairs, _policy(api, min_pairs=1, min_agreement=0.9, max_false_positive=0.0))
    assert both.status == "NOT_QUALIFIED"
    assert both.reasons == ("low_agreement", "false_positive_rate")


def test_missing_policy_is_never_a_qualification(api: ModuleType) -> None:
    pairs = tuple(_pair(api, f"k{i}", "yes", "ok") for i in range(4))
    report = api.evaluate(pairs, None, unpaired_verdicts=3, unpaired_outcomes=2)
    assert report.status == "NOT_QUALIFIED" and report.reasons == ("no_policy",)
    assert report.pairs == 4
    assert (report.unpaired_verdicts, report.unpaired_outcomes) == (3, 2)


def test_no_rows_at_all_reports_insufficient_pairs(api: ModuleType) -> None:
    report = api.evaluate((), _policy(api, min_pairs=1))
    assert report.status == "NOT_QUALIFIED" and report.reasons == ("insufficient_pairs",)
    assert (report.pairs, report.agreement, report.false_positive_rate) == (0, 0.0, 0.0)


def test_self_qualifying_policies_are_refused(api: ModuleType) -> None:
    with pytest.raises(ValueError, match="positive_verdicts"):
        _policy_with(api, positive_verdicts=[], positive_outcomes=["ok"])
    with pytest.raises(ValueError, match="positive_outcomes"):
        _policy_with(api, positive_verdicts=["yes"], positive_outcomes=[""])
    with pytest.raises(ValueError, match="min_pairs"):
        _policy_with(api, min_pairs=0)
    with pytest.raises(ValueError, match="min_agreement"):
        _policy_with(api, min_agreement=1.5)
    with pytest.raises(ValueError, match="max_false_positive"):
        _policy_with(api, max_false_positive=-0.1)


def _policy_with(api: ModuleType, **overrides: object) -> object:
    values: dict[str, object] = {
        "min_pairs": 1,
        "min_agreement": 1.0,
        "max_false_positive": 0.0,
        "positive_verdicts": ["yes"],
        "positive_outcomes": ["ok"],
    }
    values.update(overrides)
    return api.promotion_policy(**values)


# --------------------------------------------------------------- plugin surface


def test_settings_default_to_the_profile_ledger_dir(load_plugin) -> None:
    plugin = load_plugin("shadow-gate")
    settings = plugin.module._build_settings(_config({}), sys.modules[MODULE_NAME])
    assert settings.ledger_dir == Path(plugin.home) / "plugin-data" / "shadow-gate"
    assert settings.max_rows_per_decision == 5000
    assert dict(settings.policies) == {}


def test_bad_policy_entries_are_refused_by_name(load_plugin) -> None:
    plugin = load_plugin("shadow-gate")
    build = plugin.module._build_settings
    api = sys.modules[MODULE_NAME]

    def without(key: str) -> dict[str, object]:
        return {name: value for name, value in GOOD_POLICY.items() if name != key}

    with pytest.raises(ValueError, match=r"route-tier.*is missing min_agreement"):
        build(_config({"decisions": {"route-tier": without("min_agreement")}}), api)
    with pytest.raises(ValueError, match=r"route-tier.*positive_outcomes"):
        build(_config({"decisions": {"route-tier": without("positive_outcomes")}}), api)
    with pytest.raises(TypeError, match=r"route-tier.*min_pairs"):
        build(_config({"decisions": {"route-tier": {**GOOD_POLICY, "min_pairs": "2"}}}), api)
    with pytest.raises(TypeError, match=r"route-tier.*positive_verdicts"):
        build(
            _config({"decisions": {"route-tier": {**GOOD_POLICY, "positive_verdicts": "yes"}}}),
            api,
        )
    with pytest.raises(ValueError, match="decision name"):
        build(_config({"decisions": {"../x": GOOD_POLICY}}), api)
    with pytest.raises(ValueError, match="max_rows_per_decision"):
        build(_config({"max_rows_per_decision": 0}), api)
    with pytest.raises(TypeError, match="ledger_dir"):
        build(_config({"ledger_dir": 7}), api)

    settings = build(
        _config({"decisions": {"route-tier": GOOD_POLICY}, "max_rows_per_decision": 10}), api
    )
    assert settings.max_rows_per_decision == 10
    policy = settings.policies["route-tier"]
    assert policy.min_pairs == 2 and policy.positive_verdicts == frozenset({"yes"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.min_pairs = 3


def test_tool_reports_every_decision_with_its_gates(
    load_plugin, tmp_path: Path
) -> None:
    plugin = _load_config_plugin(load_plugin, tmp_path, decisions={"route-tier": GOOD_POLICY})
    api = sys.modules[MODULE_NAME]
    ledger = api.shared_ledger(tmp_path / "led")
    later = BASE + timedelta(seconds=1)
    ledger.record_verdict("route-tier", "k1", "dig", "yes", recorded_at=BASE)
    ledger.record_outcome("route-tier", "k1", "ok", recorded_at=later)
    ledger.record_verdict("route-tier", "k2", "dig", "yes", recorded_at=BASE)
    ledger.record_outcome("route-tier", "k2", "bad", recorded_at=later)
    ledger.record_verdict("unconfigured", "k9", "dig", "yes", recorded_at=BASE)

    payload = json.loads(plugin.tool("shadow_status", {}))
    assert set(payload) == {"decisions"}
    assert sorted(payload["decisions"]) == ["route-tier", "unconfigured"]
    route = payload["decisions"]["route-tier"]
    assert set(route) == {
        "pairs",
        "agreement",
        "false_positive_rate",
        "unpaired_verdicts",
        "unpaired_outcomes",
        "status",
        "reasons",
    }
    assert route["pairs"] == 2 and route["status"] == "NOT_QUALIFIED"
    assert route["reasons"] == ["low_agreement"]
    assert (route["agreement"], route["false_positive_rate"]) == (0.5, 0.5)
    assert payload["decisions"]["unconfigured"] == {
        "pairs": 0,
        "agreement": 0.0,
        "false_positive_rate": 0.0,
        "unpaired_verdicts": 1,
        "unpaired_outcomes": 0,
        "status": "NOT_QUALIFIED",
        "reasons": ["no_policy"],
    }

    scoped = json.loads(plugin.tool("shadow_status", {"decision": "route-tier"}))
    assert list(scoped["decisions"]) == ["route-tier"]

    failed = plugin.tool("shadow_status", {"decision": "../x"})
    assert "decision name" in json.dumps(
        json.loads(failed) if isinstance(failed, str) else failed
    )


def test_cli_prints_table_and_json_and_exits_zero(
    load_plugin, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plugin = _load_config_plugin(load_plugin, tmp_path, decisions={"route-tier": GOOD_POLICY})
    api = sys.modules[MODULE_NAME]
    ledger = api.shared_ledger(tmp_path / "led")
    for index in range(2):
        ledger.record_verdict("route-tier", f"k{index}", "dig", "yes", recorded_at=BASE)
        ledger.record_outcome(
            "route-tier", f"k{index}", "ok", recorded_at=BASE + timedelta(seconds=1)
        )
    handler, parser = _cli(plugin)

    assert handler(parser.parse_args([])) == 0
    table = capsys.readouterr().out
    assert "decision" in table and "route-tier" in table
    assert "QUALIFIED" in table and "1.000" in table and "0.000" in table

    assert handler(parser.parse_args(["--json"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decisions"]["route-tier"]["status"] == "QUALIFIED"
    assert payload["decisions"]["route-tier"]["reasons"] == []

    # An unrecorded decision is an honest empty row, still exit 0: NOT_QUALIFIED
    # is a normal answer, never a failed command.
    assert handler(parser.parse_args(["--decision", "nope"])) == 0
    quiet = capsys.readouterr().out
    assert "nope" in quiet and "no_policy" in quiet


def test_cli_exits_two_when_the_ledger_directory_is_unusable(
    load_plugin, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    plugin = _load_config_plugin(
        load_plugin, tmp_path, ledger_dir=str(blocker), decisions={"route-tier": GOOD_POLICY}
    )
    handler, parser = _cli(plugin)

    assert handler(parser.parse_args([])) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "shadow-status:" in captured.err and "blocked" in captured.err
