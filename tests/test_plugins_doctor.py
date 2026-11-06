"""plugins-doctor must report links, settings, host seams and stale imports as verdict rows."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Protocol

import pytest

REPO_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "plugins-doctor"
CHECK_KEYS = {"check", "plugin", "severity", "detail"}
RANK = {"ok": 2, "warn": 1, "fail": 0}
SCHEMA: Mapping[str, object] = {"stale_records_keep": {"type": "int", "default": 16}}


class Row(Protocol):
    """The finding shape the doctor returns; asserted rather than imported by module path."""

    check: str
    plugin: str | None
    severity: str
    detail: str


@dataclass(frozen=True, slots=True)
class Manifest:
    """The manifest slice ``check_settings`` reads, without importing the host parser."""

    name: str
    config_schema: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Doctor:
    """The loaded plugin plus the two modules under test."""

    handle: SimpleNamespace
    module: ModuleType
    checks: ModuleType


@pytest.fixture
def doctor(load_plugin: Callable[..., SimpleNamespace]) -> Doctor:
    handle = load_plugin("plugins-doctor", {"stale_records_keep": 16})
    return Doctor(handle=handle, module=handle.module, checks=handle.module.checks)


@pytest.fixture
def good_plugin(doctor: Doctor) -> Path:
    """A loadable plugin entry: ``good/`` with a manifest and a module."""
    good = doctor.handle.home / "plugins" / "good"
    good.mkdir(parents=True)
    (good / "plugin.yaml").write_text("name: good\n", encoding="utf-8")
    (good / "impl.py").write_text("VALUE = 1\n", encoding="utf-8")
    return good


@pytest.fixture
def clean_plugins(good_plugin: Path) -> Path:
    """A plugins dir whose only entry resolves to a manifest, plus a symlink to it."""
    plugins = good_plugin.parent
    (plugins / "good-link").symlink_to(good_plugin, target_is_directory=True)
    return plugins


@pytest.fixture
def broken_plugins(clean_plugins: Path) -> Path:
    """The clean tree plus the two installation faults the doctor exists to catch."""
    (clean_plugins / "dangling").symlink_to(clean_plugins / "never-installed", target_is_directory=True)
    (clean_plugins / "noyaml").mkdir()
    return clean_plugins


def rows(findings: Sequence[Row], check: str) -> list[Row]:
    return [row for row in findings if row.check == check]


def failures(findings: Sequence[Row]) -> list[Row]:
    return [row for row in findings if row.severity == "fail"]


def record(pid: int, plugins: Mapping[str, str], argv0: str = "hermes chat") -> dict[str, object]:
    """One persisted load record with the shape ``record_load`` writes."""
    return {
        "v": 1,
        "pid": pid,
        "argv0": argv0,
        "started_at": "2026-11-06T00:00:00+00:00",
        "plugins": dict(plugins),
    }


def dead_pid() -> int:
    """A pid this test reaped, so the doctor must treat its imports as gone."""
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child.wait(timeout=30)
    return int(child.pid)


def run_cli(doctor: Doctor, argv: Sequence[str]) -> int:
    """Dispatch the registered CLI command exactly as ``hermes_cli.main`` does."""
    entry = doctor.handle.manager._cli_commands["plugins-doctor"]
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command", required=True)
    child = subparsers.add_parser(entry["name"], help=entry["help"])
    entry["setup_fn"](child)
    child.set_defaults(func=entry["handler_fn"])
    args = parser.parse_args(list(argv))
    return int(args.func(args))


def tree(root: Path) -> set[str]:
    return set() if not root.is_dir() else {str(p.relative_to(root)) for p in root.rglob("*")}


# --------------------------------------------------------------------------- (a) links
def test_links_fails_a_dangling_symlink_and_a_manifestless_dir(broken_plugins: Path, doctor: Doctor) -> None:
    findings = doctor.checks.check_links(broken_plugins)
    by_plugin = {row.plugin: row for row in rows(findings, "links") if row.plugin}
    assert by_plugin["dangling"].severity == "fail"
    assert "never-installed" in by_plugin["dangling"].detail
    assert by_plugin["noyaml"].severity == "fail"
    assert "plugin.yaml" in by_plugin["noyaml"].detail
    assert "good" not in by_plugin and "good-link" not in by_plugin
    assert [row for row in rows(findings, "links") if row.severity == "ok"]


def test_links_fails_when_the_plugins_dir_is_absent(doctor: Doctor, tmp_path: Path) -> None:
    findings = doctor.checks.check_links(tmp_path / "no-plugins")
    assert [row.severity for row in findings] == ["fail"]


def test_links_ignores_an_absent_profile_dir(broken_plugins: Path, doctor: Doctor, tmp_path: Path) -> None:
    profile_dir = tmp_path / "profiles" / "work" / "plugins"
    findings = doctor.checks.check_links(broken_plugins, profile_dir)
    assert [row for row in findings if str(profile_dir) in row.detail] == []
    assert [row.plugin for row in failures(findings)] == ["dangling", "noyaml"]


def test_profile_dir_is_scanned_and_reported(doctor: Doctor, good_plugin: Path, tmp_path: Path) -> None:
    profile_dir = tmp_path / "profiles" / "work" / "plugins"
    profile_dir.mkdir(parents=True)
    (profile_dir / "stray").mkdir()
    findings = doctor.checks.check_links(good_plugin.parent, profile_dir)
    stray = [row for row in rows(findings, "links") if row.plugin == "stray"]
    assert [row.severity for row in stray] == ["fail"]
    assert any(str(profile_dir) in row.detail for row in findings)


def test_plugin_dirs_names_a_profile_only_when_one_is_set(doctor: Doctor) -> None:
    home = Path("/tmp/hermes-home-under-test")
    assert doctor.checks.plugin_dirs(home, None) == (home / "plugins",)
    assert doctor.checks.plugin_dirs(home, "default") == (home / "plugins",)
    assert doctor.checks.plugin_dirs(home, "  ") == (home / "plugins",)
    assert doctor.checks.plugin_dirs(home, "work") == (
        home / "plugins",
        home / "profiles" / "work" / "plugins",
    )


# ------------------------------------------------------------------------ (b) settings
def test_settings_warns_on_unknown_keys_and_fails_wrong_types(doctor: Doctor) -> None:
    config = {
        "plugins": {
            "enabled": ["good"],
            "entries": {
                "good": {
                    "enabled": True,
                    "settings": {"stale_records_keep": "sixteen", "bogus": 1},
                },
            },
        },
    }
    findings = doctor.checks.check_settings(config, {"good": Manifest("good", SCHEMA)})
    unknown = [row for row in rows(findings, "settings") if "bogus" in row.detail]
    assert [row.severity for row in unknown] == ["warn"]
    wrong = [row for row in rows(findings, "settings") if "stale_records_keep" in row.detail]
    assert [row.severity for row in wrong] == ["fail"]
    assert "should be int" in wrong[0].detail


def test_settings_fails_an_enabled_plugin_without_a_manifest(doctor: Doctor) -> None:
    findings = doctor.checks.check_settings({"plugins": {"enabled": ["ghost"]}}, {})
    assert [(row.plugin, row.severity) for row in failures(findings)] == [("ghost", "fail")]


def test_settings_fails_settings_that_are_not_a_mapping(doctor: Doctor) -> None:
    config = {"plugins": {"entries": {"good": {"enabled": True, "settings": "yes"}}}}
    findings = doctor.checks.check_settings(config, {"good": Manifest("good", SCHEMA)})
    assert [row.severity for row in failures(findings)] == ["fail"]
    assert "must be a mapping" in failures(findings)[0].detail


def test_settings_passes_a_matching_plugin_and_ignores_disabled_ones(doctor: Doctor) -> None:
    config = {
        "plugins": {
            "entries": {
                "good": {"enabled": True, "settings": {"stale_records_keep": 4}},
                "ghost": {"enabled": False, "settings": {"bogus": 1}},
            },
        },
    }
    findings = doctor.checks.check_settings(config, {"good": Manifest("good", SCHEMA)})
    assert [(row.plugin, row.severity) for row in rows(findings, "settings")] == [("good", "ok")]


def test_settings_reports_no_enabled_plugins_honestly(doctor: Doctor) -> None:
    findings = doctor.checks.check_settings({"plugins": {"enabled": [], "entries": {}}}, {})
    assert [(row.plugin, row.severity) for row in findings] == [(None, "ok")]


# --------------------------------------------------------------------------- (c) seams
def test_seams_pass_and_name_every_prerequisite_on_this_host(doctor: Doctor) -> None:
    findings = rows(doctor.checks.check_seams(), "seams")
    assert [row.detail for row in failures(findings)] == []
    covered = "\n".join(row.detail for row in findings)
    for name in (
        *doctor.checks.REQUIRED_CONTEXT_MEMBERS,
        *doctor.checks.REQUIRED_HOOKS,
        *doctor.checks.REQUIRED_MODULES,
        doctor.checks.REQUIRED_MCP_KWARG,
    ):
        assert name in covered


def test_seams_fail_when_the_host_module_is_gone(doctor: Doctor, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.checks, "_host_plugins_module", lambda: None)
    findings = doctor.checks.check_seams()
    assert [row.severity for row in findings] == ["fail"]


def test_seams_name_a_missing_hook_and_a_missing_context_member(
    doctor: Doctor, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.plugins as host

    monkeypatch.setattr(host, "VALID_HOOKS", {"pre_tool_call"})
    monkeypatch.setattr(host.PluginContext, "cancel_model_call", None, raising=False)
    details = "\n".join(row.detail for row in failures(doctor.checks.check_seams()))
    assert "post_compaction" in details
    assert "PluginContext.cancel_model_call" in details


def test_seams_name_a_call_mcp_without_the_replay_parameter(
    doctor: Doctor, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.plugins as host

    monkeypatch.setattr(host.PluginContext, "call_mcp", lambda self, server, tool: None)
    failing = [row for row in failures(doctor.checks.check_seams()) if "call_mcp" in row.detail]
    assert [row.severity for row in failing] == ["fail"]
    assert doctor.checks.REQUIRED_MCP_KWARG in failing[0].detail


# --------------------------------------------------------------------- (d) stale imports
def test_digest_changes_when_a_source_file_changes(clean_plugins: Path, doctor: Doctor) -> None:
    entries = doctor.checks.merged_entries([clean_plugins])
    before = doctor.checks.digest_plugins(entries)
    (clean_plugins / "good" / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    after = doctor.checks.digest_plugins(entries)
    assert set(before) == {"good", "good-link"}
    assert before["good"] == before["good-link"], "a symlink digests as the plugin it resolves to"
    assert before["good"] != after["good"]


def test_stale_fails_a_live_process_importing_old_source(broken_plugins: Path, doctor: Doctor) -> None:
    current = doctor.checks.digest_plugins(doctor.checks.merged_entries([broken_plugins]))
    findings = doctor.checks.check_stale([record(os.getpid(), {"good": "0" * 64})], broken_plugins)
    stale = failures(findings)
    assert [(row.plugin, row.severity) for row in stale] == [("good", "fail")]
    assert f"pid {os.getpid()}" in stale[0].detail
    assert "hermes chat" in stale[0].detail
    assert current["good"][:12] in stale[0].detail


def test_stale_passes_when_the_digests_match(broken_plugins: Path, doctor: Doctor) -> None:
    current = doctor.checks.digest_plugins(doctor.checks.merged_entries([broken_plugins]))
    findings = doctor.checks.check_stale([record(os.getpid(), current)], broken_plugins)
    assert failures(findings) == []
    assert [row.severity for row in rows(findings, "stale_import")] == ["ok"]


def test_stale_prunes_a_record_whose_process_is_gone(broken_plugins: Path, doctor: Doctor) -> None:
    stale = failures(doctor.checks.check_stale([record(dead_pid(), {"good": "0" * 64})], broken_plugins))
    assert stale == []


def test_stale_reports_a_plugin_that_no_longer_exists(broken_plugins: Path, doctor: Doctor) -> None:
    findings = doctor.checks.check_stale([record(os.getpid(), {"uninstalled": "0" * 64})], broken_plugins)
    assert [row.plugin for row in failures(findings)] == ["uninstalled"]
    assert "absent" in failures(findings)[0].detail


def test_stale_counts_malformed_records_instead_of_ignoring_them(
    broken_plugins: Path, doctor: Doctor
) -> None:
    findings = doctor.checks.check_stale(
        [{"v": 99}, "not-a-record", {"pid": os.getpid()}], broken_plugins
    )
    summary = [row for row in rows(findings, "stale_import") if row.plugin is None]
    assert [row.severity for row in summary] == ["warn"]
    assert "3 malformed" in summary[0].detail


def test_stale_declines_when_the_plugins_dir_is_missing(doctor: Doctor, tmp_path: Path) -> None:
    findings = doctor.checks.check_stale([record(os.getpid(), {"good": "0" * 64})], tmp_path / "gone")
    assert [(row.plugin, row.severity) for row in findings] == [(None, "warn")]


def test_process_alive_distinguishes_this_process_from_a_dead_one(doctor: Doctor) -> None:
    assert doctor.checks.process_alive(os.getpid()) is True
    assert doctor.checks.process_alive(dead_pid()) is False
    assert doctor.checks.process_alive(0) is False


# ------------------------------------------------------------- load records (the mechanism)
def test_register_records_this_processs_digests_and_argv0(
    clean_plugins: Path, doctor: Doctor
) -> None:
    doctor.module.record_load(doctor.handle.context, doctor.module.DoctorSettings(16), [clean_plugins])
    stored = doctor.handle.context.state.get("loads")
    mine = [item for item in stored if item["pid"] == os.getpid()]
    assert len(mine) == 1, "a reload must re-snapshot its own pid, never duplicate it"
    assert mine[0]["v"] == 1
    assert mine[0]["argv0"] == sys.argv[0][: doctor.checks.MAX_ARGV_CHARS]
    assert datetime.fromisoformat(mine[0]["started_at"]).tzinfo is not None
    assert set(mine[0]["plugins"]) == {"good", "good-link"}


def test_load_records_keep_only_the_newest(doctor: Doctor, good_plugin: Path) -> None:
    seeded = [record(900_000 + offset, {}) for offset in range(5)]
    doctor.handle.context.state.set("loads", seeded)
    doctor.module.record_load(
        doctor.handle.context, doctor.module.DoctorSettings(3), [good_plugin.parent]
    )
    stored = doctor.handle.context.state.get("loads")
    assert len(stored) == 3
    assert stored[-1]["pid"] == os.getpid()
    assert [item["pid"] for item in stored[:-1]] == [900_003, 900_004]


def test_a_bad_record_count_still_records_the_load(doctor: Doctor, good_plugin: Path) -> None:
    doctor.handle.context.state.set("loads", "corrupted")
    doctor.module.record_load(
        doctor.handle.context, doctor.module.DoctorSettings(16), [good_plugin.parent]
    )
    assert [item["pid"] for item in doctor.handle.context.state.get("loads")] == [os.getpid()]


def test_record_count_below_one_falls_back_without_silencing_the_doctor(
    doctor: Doctor, caplog: pytest.LogCaptureFixture
) -> None:
    assert doctor.module._read_settings(StubContext(0)).stale_records_keep == (
        doctor.module.DEFAULT_RECORDS_KEEP
    )
    assert doctor.module._read_settings(StubContext(True)).stale_records_keep == 16
    assert doctor.module._read_settings(StubContext(7)).stale_records_keep == 7
    assert [r for r in caplog.records if "stale_records_keep" in r.getMessage()]


class StubContext:
    """The one ``PluginContext`` method the settings reader uses."""

    def __init__(self, value: object) -> None:
        self._value = value

    def get_config(self, key: str, default: object = None) -> object:
        assert key == "stale_records_keep"
        return self._value


# ------------------------------------------------------------------------- the CLI
def test_cli_json_reports_every_check_and_puts_failures_first(
    broken_plugins: Path, doctor: Doctor, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = run_cli(doctor, ["plugins-doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list) and payload
    assert all(set(row) == CHECK_KEYS for row in payload)
    assert {row["check"] for row in payload} == {"links", "settings", "seams", "stale_import"}
    assert [row["severity"] for row in payload] == sorted(
        [row["severity"] for row in payload], key=RANK.__getitem__
    )
    assert any(row["severity"] == "fail" and row["plugin"] == "dangling" for row in payload)
    assert exit_code == 1


def test_cli_table_names_the_worst_rows(
    broken_plugins: Path, doctor: Doctor, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = run_cli(doctor, ["plugins-doctor"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["CHECK", "PLUGIN", "SEVERITY", "DETAIL"]
    assert exit_code == 1
    assert any(line.split()[2] == "fail" and "dangling" in line for line in lines[1:])


def test_cli_exits_zero_when_nothing_fails(
    clean_plugins: Path, doctor: Doctor, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A clean tree plus an empty config: the settings rows that depend on which plugins another
    # test happened to enable are covered directly by the check_settings tests above.
    monkeypatch.setattr(doctor.module, "_read_config", lambda: ({}, None))
    (clean_plugins / "plugins-doctor").symlink_to(REPO_PLUGIN_DIR, target_is_directory=True)
    assert run_cli(doctor, ["plugins-doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)


def test_cli_writes_nothing_outside_the_fixture_home(
    broken_plugins: Path, doctor: Doctor
) -> None:
    real_home = Path.home() / ".hermes"
    before_home, before_plugins = tree(real_home), tree(broken_plugins)
    run_cli(doctor, ["plugins-doctor", "--json"])
    assert tree(real_home) == before_home
    assert tree(broken_plugins) == before_plugins
    assert list((doctor.handle.home / "plugin-data").rglob("state.json"))
