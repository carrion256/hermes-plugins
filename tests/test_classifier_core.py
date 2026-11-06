"""The shared classifier client must fail visibly, share one breaker, and never leak the key.

Every test injects the transport (a scripted opener) and the clock; nothing here touches a
socket, except the two cases that deliberately prove the health probe DOES.

No logger is imported here: the level check below uses the numeric WARNING value directly,
because structlog (the house logging library) is not installed in the host environment and
the module under test therefore logs through stdlib ``logging`` — ``caplog`` bridges that
without this module needing a logger of its own.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import json
import sys
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Final

import pytest

MODULE_KEY: Final[str] = "hermes_plugins_classifier_core"
CLIENT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1] / "plugins" / "classifier-core" / "client.py")
PLUGIN: Final[str] = "classifier-core"
KEY_ENV: Final[str] = "CLASSIFIER_TEST_KEY"
KEY: Final[str] = "sup3r-s3cr3t-bearer"
SERVICE_URL: Final[str] = "http://a.test:8102/v1/systemone"
WARNING: Final[int] = 30  # logging.WARNING, without importing a logger into a test module
SCHEMA: Final[dict[str, dict[str, object]]] = {
    "risk": {"labels": {"high": "destructive or irreversible", "low": "reversible"}},
}


class Clock:
    """Injectable monotonic clock."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Reply:
    """A canned urllib reply: bounded ``read``, closeable, with an HTTP status."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self._body = body
        self.closed = False

    def read(self, amt: int) -> bytes:
        return self._body[:amt]

    def close(self) -> None:
        self.closed = True


class Opener:
    """Scripted transport; records every request and timeout, and can age the clock."""

    def __init__(self, *outcomes: object, clock: Clock | None = None,
                 advance: float = 0.0) -> None:
        self._outcomes = list(outcomes)
        self._clock = clock
        self._advance = advance
        self.requests: list[tuple[urllib.request.Request, float]] = []

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> Reply:
        self.requests.append((request, timeout))
        if self._clock is not None:
            self._clock.advance(self._advance)
        outcome = self._outcomes.pop(0) if self._outcomes else answers(
            {"risk": {"choice": "low"}})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def calls(self) -> int:
        return len(self.requests)

    def payload(self, index: int = 0) -> dict[str, object]:
        request = self.requests[index][0]
        assert request.data is not None
        body: dict[str, object] = json.loads(request.data)
        return body


def answers(answer: dict[str, object]) -> Reply:
    return Reply(json.dumps({"answers": answer}).encode("utf-8"))


def http_error(code: int, reason: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(SERVICE_URL, code, reason, Message(), io.BytesIO(b""))


def settings_for(module, endpoint: str, **overrides: object):
    """Settings for this service, with only the named fields changed."""
    return module.ClassifierSettings(endpoint=endpoint, api_key_env=KEY_ENV, **overrides)


@pytest.fixture
def client_module():
    """The client module exactly as a sibling plugin path-loads it, with a fresh registry."""
    sys.modules.pop(MODULE_KEY, None)
    spec = importlib.util.spec_from_file_location(MODULE_KEY, CLIENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_KEY] = module  # before exec: dataclass annotation resolution needs it
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(MODULE_KEY, None)


@pytest.fixture
def keyed(monkeypatch):
    """The Bearer key present, as a deployed node would have it."""
    monkeypatch.setenv(KEY_ENV, KEY)
    return KEY


def build(module, *outcomes: object, endpoint: str = "http://a.test:8102",
          advance: float = 0.0, **overrides: object):
    """A client wired to a scripted transport and a fake monotonic clock."""
    clock = Clock()
    opener = Opener(*outcomes, clock=clock if advance else None, advance=advance)
    client = module.ClassifierClient(
        settings_for(module, endpoint, **overrides), clock=clock, opener=opener)
    return client, opener, clock


def declined(status, caller: str, reason: str) -> int:
    return status.callers[caller].declined[reason]


# ------------------------------------------------------------------ label-schema translation
def test_classify_posts_typed_questions_and_back_maps_the_label(client_module, keyed) -> None:
    client, opener, _clock = build(client_module, answers({"risk": {"choice": "high"}}))

    labels = client.classify("rm -rf /", SCHEMA, caller="meta-classifier")

    assert labels == {"risk": "high"}
    assert opener.calls == 1
    request, _timeout = opener.requests[0]
    assert request.full_url == SERVICE_URL
    assert request.get_header("Authorization") == f"Bearer {keyed}"
    body = opener.payload()
    assert body["state"] == "rm -rf /"
    assert body["model"] == "qwen3.8-flash-next"
    # descriptions ride verbatim as criteria; the head becomes ONE typed choice question
    assert body["questions"] == {"risk": {"type": "choice", "criteria": {
        "high": "destructive or irreversible", "low": "reversible"}}}
    assert declined(client.status(), "meta-classifier", "unusable_body") == 0


def test_classify_multi_label_head_offers_none_and_answers_a_list(client_module, keyed) -> None:
    schema = {"tags": {"labels": {"spam": "junk", "ops": "operations"}, "multi_label": True}}
    client, opener, _clock = build(
        client_module, answers({"tags": {"choice": "spam"}}), answers({"tags": {"choice": "none"}}))

    assert client.classify("x", schema, caller="c") == {"tags": ["spam"]}
    assert "none" in opener.payload()["questions"]["tags"]["criteria"]
    # the explicit none is an empty list, never a fabricated label and never the head "none"
    assert client.classify("x", schema, caller="c") == {"tags": []}


def test_classify_leaves_an_unanswered_head_absent_and_never_guesses(client_module, keyed) -> None:
    client, _opener, _clock = build(client_module, answers({
        "risk": {"choice": "high"}, "other": {"choice": "off-menu"}, "missing": {}}))

    got = client.classify("x", {"risk": {"labels": {"high": "h"}},
                               "other": {"labels": {"a": "a"}}}, caller="c")

    assert got == {"risk": "high"}


def test_classify_with_no_answerable_head_is_a_counted_decline(client_module, keyed) -> None:
    client, _opener, _clock = build(client_module, Reply(b'{"answers": {"risk": {}}}'))

    assert client.classify("x", SCHEMA, caller="c") is None
    assert declined(client.status(), "c", "unusable_body") == 1


@pytest.mark.parametrize("schema", [{}, {"risk": "not a head"}], ids=["empty", "malformed"])
def test_classify_refuses_an_unusable_schema_without_a_request(
    client_module, keyed, schema: dict[str, object],
) -> None:
    client, opener, _clock = build(client_module)

    assert client.classify("x", schema, caller="c") is None
    assert opener.calls == 0
    assert declined(client.status(), "c", "text_limits") == 1


def test_state_outside_service_limits_is_declined_without_a_request(client_module, keyed) -> None:
    client, opener, _clock = build(client_module, max_text=10)

    assert client.classify("x" * 11, SCHEMA, caller="c") is None
    assert client.ask("", {"q": {}}, caller="c") is None
    assert opener.calls == 0
    assert declined(client.status(), "c", "text_limits") == 2


# ------------------------------------------------------------------------- ask / envelope
def test_ask_returns_the_raw_envelope_including_probabilities(client_module, keyed) -> None:
    envelope = {"answers": {"risk": {"choice": "high", "probabilities": {"high": 0.9}}}}
    client, opener, _clock = build(client_module, Reply(json.dumps(envelope).encode("utf-8")))

    got = client.ask("state", {"risk": {"type": "choice", "criteria": {"high": "h"}}},
                     caller="c")

    assert got == envelope  # probabilities included: the raw envelope, not a stripped answer set
    assert opener.payload()["questions"]["risk"]["criteria"] == {"high": "h"}


def test_missing_key_declines_as_not_configured_without_touching_transport(
    client_module, monkeypatch,
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    client, opener, _clock = build(client_module)

    assert client.ask("state", {"q": {}}, caller="c") is None
    assert opener.calls == 0
    assert declined(client.status(), "c", "not_configured") == 1


def test_unusable_body_is_counted_and_does_not_trip_the_breaker(client_module, keyed) -> None:
    client, _opener, _clock = build(client_module, Reply(b"<html>not json</html>"),
                                    Reply(b'["not an envelope"]'))

    assert client.ask("state", {"q": {}}, caller="c") is None
    assert client.ask("state", {"q": {}}, caller="c") is None

    status = client.status()
    assert status.breaker == "closed"
    assert declined(status, "c", "unusable_body") == 2
    assert status.last_error is not None and "answers" in status.last_error


# ------------------------------------------------------------------------- breaker law
def test_breaker_opens_after_consecutive_service_errors_and_stops_calling_transport(
    client_module, keyed,
) -> None:
    down = urllib.error.URLError("connection refused")
    client, opener, _clock = build(client_module, down, down, down)

    for _ in range(4):
        assert client.ask("state", {"q": {}}, caller="c") is None

    status = client.status()
    assert opener.calls == 3  # the fourth request never reached the transport
    assert status.breaker == "open"
    assert status.consecutive_failures == 3
    assert status.open_until == pytest.approx(60.0)
    assert declined(status, "c", "service_error") == 3
    assert declined(status, "c", "breaker_open") == 1


def test_half_open_probe_is_single_and_closes_the_breaker(client_module, keyed, caplog) -> None:
    down = urllib.error.URLError("down")
    client, opener, clock = build(client_module, down, down, down,
                                  answers({"risk": {"choice": "high"}}))
    for _ in range(3):
        client.ask("state", {"q": {}}, caller="c")

    with caplog.at_level(WARNING, logger="classifier-core"):
        clock.advance(60.0)
        assert client.ask("state", {"q": {}}, caller="c") == {
            "answers": {"risk": {"choice": "high"}}}

    status = client.status()
    assert status.breaker == "closed" and status.consecutive_failures == 0
    assert opener.calls == 4
    assert "half-open" in caplog.text and "closed" in caplog.text
    assert "http://a.test:8102" in caplog.text and KEY not in caplog.text


def test_half_open_probe_failure_reopens_without_a_second_probe(client_module, keyed) -> None:
    down = urllib.error.URLError("down")
    client, opener, clock = build(client_module, down, down, down, down)
    for _ in range(3):
        client.ask("state", {"q": {}}, caller="c")
    clock.advance(60.0)

    assert client.ask("state", {"q": {}}, caller="c") is None
    assert client.ask("state", {"q": {}}, caller="c") is None

    status = client.status()
    assert opener.calls == 4  # one probe only; the next caller is declined, not sent
    assert status.breaker == "open"
    assert declined(status, "c", "service_error") == 4
    assert declined(status, "c", "breaker_open") == 1


def test_success_clears_consecutive_failures_before_the_breaker_opens(client_module, keyed) -> None:
    down = urllib.error.URLError("blip")
    client, _opener, _clock = build(
        client_module, down, answers({"risk": {"choice": "high"}}), down, down)

    for _ in range(4):
        client.ask("state", {"q": {}}, caller="c")

    assert client.status().breaker == "closed"
    assert client.status().consecutive_failures == 2


def test_429_is_retried_once_and_500_is_not(client_module, keyed) -> None:
    client, _opener, _clock = build(client_module, http_error(429, "slow down"),
                                    answers({"risk": {"choice": "high"}}))

    assert client.ask("state", {"q": {}}, caller="c") == {"answers": {"risk": {"choice": "high"}}}
    assert client.status().breaker == "closed"

    refused, opener, _clock = build(client_module, http_error(500, "boom"))
    assert refused.ask("state", {"q": {}}, caller="c") is None
    assert opener.calls == 1
    assert declined(refused.status(), "c", "service_error") == 1
    assert refused.status().last_error == "HTTPError: HTTP 500"


def test_503_status_without_an_exception_is_also_retried_once(client_module, keyed) -> None:
    client, opener, _clock = build(client_module, Reply(b"overloaded", status=503),
                                   answers({"risk": {"choice": "high"}}))

    assert client.ask("state", {"q": {}}, caller="c") == {"answers": {"risk": {"choice": "high"}}}
    assert opener.calls == 2


def test_cancelled_transport_is_a_visible_decline_not_a_service_error(client_module, keyed) -> None:
    client, _opener, _clock = build(client_module, asyncio.CancelledError("turn interrupted"))

    assert client.ask("state", {"q": {}}, caller="c") is None

    status = client.status()
    assert status.breaker == "closed"
    assert declined(status, "c", "cancelled") == 1
    assert declined(status, "c", "service_error") == 0


# ------------------------------------------------------------------------- timeouts, key
def test_timeout_floor_grows_with_the_payload_and_an_explicit_timeout_wins(
    client_module, keyed,
) -> None:
    client, opener, _clock = build(client_module)

    client.ask("x" * 10, {"q": {}}, caller="c")
    client.ask("x" * 100_000, {"q": {}}, caller="c")  # measured cost: 0.5 + 100k * 0.00025
    client.ask("x" * 10, {"q": {}}, caller="c", timeout=0.25)

    assert [timeout for _request, timeout in opener.requests] == [2.5, 25.5, 0.25]


def test_computed_timeout_never_exceeds_the_ceiling(client_module, keyed) -> None:
    client, opener, _clock = build(client_module, char_seconds=0.01)

    client.ask("x" * 10_000, {"q": {}}, caller="c")

    assert opener.requests[0][1] == 30.0


def test_key_never_reaches_status_or_logs(client_module, keyed, caplog) -> None:
    client, _opener, _clock = build(
        client_module, urllib.error.URLError(f"401 for {KEY} rejected"),
        http_error(401, f"invalid key {KEY}"))

    with caplog.at_level(WARNING, logger="classifier-core"):
        client.ask("state", {"q": {}}, caller="c")
        client.ask("state", {"q": {}}, caller="c")

    status = client.status()
    assert status.last_error is not None and KEY not in status.last_error
    assert KEY not in json.dumps(status.as_dict())
    assert KEY not in caplog.text
    assert "URLError" in (status.last_error or "") or "HTTPError" in (status.last_error or "")


# ------------------------------------------------------------------------- health probe
def test_health_reports_liveness_without_touching_counters(client_module, keyed) -> None:
    client, opener, _clock = build(client_module, Reply(b'{"status":"ok"}'), advance=0.05)

    report = client.health()

    assert report.ok is True and report.status_code == 200 and report.error is None
    assert report.latency_ms == pytest.approx(50.0)
    request, timeout = opener.requests[0]
    assert request.full_url == "http://a.test:8102/health"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") is None  # the key never rides a probe
    assert timeout == 2.0
    status = client.status()
    assert status.requests == 0 and dict(status.callers) == {}


def test_health_reports_a_refused_connection_as_a_failure(client_module, keyed) -> None:
    client, _opener, _clock = build(client_module, urllib.error.URLError(f"refused {KEY}"))

    report = client.health()

    assert report.ok is False and report.status_code is None
    assert "URLError" in (report.error or "") and KEY not in (report.error or "")


# ------------------------------------------------------------------------- shared registry
def test_shared_client_is_one_instance_per_endpoint(client_module, keyed) -> None:
    endpoint = "http://shared-a.test:8102"
    first = settings_for(client_module, endpoint)
    trailing_slash = settings_for(client_module, endpoint + "/")
    other = settings_for(client_module, "http://shared-b.test:8102")

    shared = client_module.shared_client(first)
    assert client_module.shared_client(trailing_slash) is shared
    assert client_module.shared_client(other) is not shared
    assert client_module.shared_client(first).status().endpoint == endpoint


def test_client_module_is_shared_through_sys_modules(client_module) -> None:
    assert sys.modules[MODULE_KEY] is client_module
    assert client_module.MODULE_KEY == MODULE_KEY


# ------------------------------------------------------------------------- settings parsing
def test_settings_default_to_todays_shared_values(client_module) -> None:
    settings = client_module.ClassifierSettings.from_config(lambda _key, default: default)

    assert settings == client_module.ClassifierSettings(
        endpoint="http://127.0.0.1:8102", served_model="qwen3.8-flash-next",
        api_key_env="CLASSIFIER_API_KEY", timeout_floor=2.5, max_timeout=30.0,
        char_seconds=0.00025, cooldown=45.0, breaker_failures=3, breaker_reset=60.0,
        max_text=100_000, read_cap=262_144)


@pytest.mark.parametrize(("key", "value", "exc"), [
    ("endpoint", 42, TypeError),           # a number where a string belongs
    ("max_text", "many", ValueError),      # right type, unusable value
    ("breaker_reset", 0, ValueError),      # below the usable range
    ("read_cap", 16, ValueError),
])
def test_unusable_settings_are_refused_at_register(
    client_module, key: str, value: object, exc: type[Exception],
) -> None:
    configured = {key: value}
    with pytest.raises(exc, match=f"classifier-core: '{key}'"):
        client_module.ClassifierSettings.from_config(
            lambda name, _default: configured.get(name))


def test_timeout_floor_above_ceiling_is_refused(client_module) -> None:
    values = {"timeout_floor": 40.0}
    with pytest.raises(ValueError, match="timeout_floor"):
        client_module.ClassifierSettings.from_config(lambda name, _default: values.get(name))


# --------------------------------------------------------------- plugin surface (tool + CLI)
TOOL_ENDPOINT: Final[str] = "http://tool.test:8102"


@pytest.fixture
def plugin(load_plugin):
    """The plugin loaded against an endpoint nothing here dials: the transport is injected."""
    sys.modules.pop(MODULE_KEY, None)  # a fresh module means a fresh client registry
    try:
        yield load_plugin(PLUGIN, {"endpoint": TOOL_ENDPOINT, "api_key_env": KEY_ENV})
    finally:
        sys.modules.pop(MODULE_KEY, None)


def shared(module):
    """The SAME client the plugin's tool reports on, obtained as a consumer plugin would."""
    settings = module.ClassifierSettings.from_config(lambda name, default: {
        "endpoint": TOOL_ENDPOINT, "api_key_env": KEY_ENV}.get(name, default))
    return module.shared_client(settings)


def instrument(monkeypatch, *outcomes: object):
    """Script the transport under the client the plugin reports on; returns (client, opener)."""
    client = shared(sys.modules[MODULE_KEY])
    opener = Opener(*outcomes)
    monkeypatch.setattr(client, "_opener", opener)
    return client, opener


def run_cli(plugin, capsys, *argv: str) -> tuple[int, dict[str, object]]:
    """Run the registered CLI command through its own argparse tree; return exit code + JSON."""
    entry = plugin.manager._cli_commands["classifier-status"]
    parser = argparse.ArgumentParser()
    entry["setup_fn"](parser.add_subparsers().add_parser("classifier-status"))
    code = entry["handler_fn"](parser.parse_args(["classifier-status", *argv]))
    return code, json.loads(capsys.readouterr().out)


def test_classifier_status_tool_reports_the_shared_breaker_and_counters(
    plugin, keyed, monkeypatch,
) -> None:
    client, _opener = instrument(monkeypatch, urllib.error.URLError("connection refused"))
    client.ask("state", {"q": {}}, caller="fast-compaction")

    payload = json.loads(plugin.tool("classifier_status", {}))

    assert set(payload) == {"endpoint", "breaker", "open_until", "consecutive_failures",
                            "totals", "callers", "last_error"}
    assert payload["endpoint"] == TOOL_ENDPOINT
    assert payload["breaker"] == "closed"
    assert payload["totals"] == {"requests": 1, "ok": 0, "declined": 1}
    assert payload["callers"] == {"fast-compaction": {
        "ok": 0, "declined": {"not_configured": 0, "breaker_open": 0, "text_limits": 0,
                              "unusable_body": 0, "service_error": 1, "cancelled": 0}}}
    assert "health" not in payload  # no probe unless asked: the status path never touches the network


def test_tool_and_consumers_share_one_client_object(plugin) -> None:
    module = sys.modules[MODULE_KEY]
    assert plugin.module._load_client().module is module
    assert shared(module) is shared(module)


def test_tool_probes_health_only_when_asked(plugin, monkeypatch) -> None:
    client, opener = instrument(monkeypatch, Reply(b'{"status":"ok"}'))

    assert "health" not in json.loads(plugin.tool("classifier_status", {}))
    assert opener.requests == []  # register and the plain status path made no request at all

    payload = json.loads(plugin.tool("classifier_status", {"probe": True}))

    assert [request.full_url for request, _timeout in opener.requests] == [
        f"{TOOL_ENDPOINT}/health"]
    assert payload["health"]["ok"] is True and payload["health"]["status_code"] == 200
    assert payload["health"]["error"] is None
    assert isinstance(payload["health"]["latency_ms"], float)  # measured, never fabricated
    assert client.status().requests == 0  # a probe is not a scored request


def test_cli_exit_code_tracks_the_breaker_not_individual_failures(
    plugin, keyed, monkeypatch, capsys,
) -> None:
    code, payload = run_cli(plugin, capsys)
    assert code == 0 and payload["breaker"] == "closed"

    down = urllib.error.URLError("connection refused")
    client, _opener = instrument(monkeypatch, down, down, down)
    client.ask("state", {"q": {}}, caller="subagent-effort")

    code, payload = run_cli(plugin, capsys)
    assert code == 0  # one service error is not yet an outage verdict
    assert payload["callers"]["subagent-effort"]["declined"]["service_error"] == 1

    client.ask("state", {"q": {}}, caller="subagent-effort")
    client.ask("state", {"q": {}}, caller="subagent-effort")  # the third opens the breaker

    code, payload = run_cli(plugin, capsys)
    assert code == 1 and payload["breaker"] == "open"
    assert payload["callers"]["subagent-effort"]["declined"]["service_error"] == 3


def test_cli_exits_one_when_the_endpoint_was_never_configured(
    plugin, monkeypatch, capsys,
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    client, opener = instrument(monkeypatch)
    client.ask("state", {"q": {}}, caller="keyword-steering")

    code, payload = run_cli(plugin, capsys)

    assert code == 1
    assert payload["callers"]["keyword-steering"]["declined"]["not_configured"] == 1
    assert opener.calls == 0  # unconfigured never reaches the transport


def test_cli_probe_adds_health_without_changing_the_verdict(
    plugin, monkeypatch, capsys,
) -> None:
    _client, _opener = instrument(monkeypatch, Reply(b'{"status":"ok"}'))
    plain = json.loads(plugin.tool("classifier_status", {}))

    code, payload = run_cli(plugin, capsys, "--probe")

    assert payload["health"]["ok"] is True and payload["health"]["status_code"] == 200
    assert {key: value for key, value in payload.items() if key != "health"} == plain
    assert code == 0  # a probe never moves the verdict; only the counters do


def test_plugin_registers_no_hooks_and_no_launch_path_network(plugin, keyed) -> None:
    assert plugin.manager._plugins[PLUGIN].hooks_registered == []
    assert plugin.manager._cli_commands["classifier-status"]["plugin"] == PLUGIN
    assert json.loads(plugin.tool("classifier_status", {}))["breaker"] == "closed"
