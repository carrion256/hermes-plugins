import json
import urllib.error
from pathlib import Path

import pytest
import yaml

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "intake"


def _load_core():
    import importlib.util
    spec = importlib.util.spec_from_file_location("intake_core_under_test",
                                                 PLUGIN_DIR / "core.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = _load_core()


class FakeReply:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self, size=-1):
        return self.body if size is None or size < 0 else self.body[:size]


def chat_body(content):
    """OpenAI-wire completion envelope with a DECOY reasoning_content the
    reader must ignore (the model is a reasoning model)."""
    return json.dumps({"model": "fake", "choices": [{"message": {
        "content": content, "reasoning_content": "DECOY chain of thought"}}]}).encode()


class FakeOpener:
    """Scripted model transport: records requests, opens no socket.

    ``content`` serves a chat-completions envelope; ``raw`` (bytes) is served
    verbatim — that is how the /v1/systemone answers envelope is scripted."""

    def __init__(self, content=None, status=200, error=None, raw=None):
        self.content = content
        self.status = status
        self.error = error
        self.raw = raw
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append({"request": request, "timeout": timeout})
        if self.error is not None:
            raise self.error
        body = self.raw if self.raw is not None else chat_body(self.content)
        return FakeReply(body, status=self.status)


class FakeCovey:
    """Scripted covey envelopes keyed by command head; records ALL argv, never
    touches the binary. Each slot is an envelope, a list (consumed in order,
    last entry repeats), or an Exception to raise."""

    def __init__(self, lookup=None, register=None, rows=None, submits=None):
        self.calls = []
        self._lookup = lookup if lookup is not None else {"ok": False, "code": "not_found"}
        self._register = register if register is not None else {
            "ok": True, "data": {"session_token": "tok-1"}}
        self._rows = rows if rows is not None else {"ok": True, "data": []}
        self._submits = list(submits) if submits is not None else [
            {"ok": True, "data": {"meta_task_id": "meta_default"}}]

    def _next(self, scripted):
        if isinstance(scripted, list):
            if not scripted:
                raise AssertionError("scripted covey responses exhausted")
            scripted = scripted.pop(0) if len(scripted) > 1 else scripted[0]
        if isinstance(scripted, Exception):
            raise scripted
        return scripted

    def _meta_list(self, argv):
        """Honour --limit/--offset like the real CLI (whose page defaults to
        the 50 newest). A dict rows slot is the full store and gets sliced
        per call; a list slot is pre-paged responses served verbatim."""
        if isinstance(self._rows, Exception):
            raise self._rows
        if isinstance(self._rows, list):
            return self._next(self._rows)
        payload = self._rows
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return payload
        limit = int(self._flag(argv, "--limit", 50))
        offset = int(self._flag(argv, "--offset", "0"))
        return {"ok": True, "data": rows[offset:offset + limit]}

    @staticmethod
    def _flag(argv, name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    def __call__(self, argv):
        self.calls.append(list(argv))
        head = tuple(argv[:2])
        if head == ("session", "active-for-principal"):
            return self._next(self._lookup)
        if head == ("session", "register"):
            return self._next(self._register)
        if head == ("meta", "list"):
            return self._meta_list(argv)
        return self._next(self._submits)

    @property
    def heads(self):
        return [tuple(argv[:2]) for argv in self.calls]

    def submits(self):
        return [argv for argv in self.calls if argv[:2] == ["meta", "submit"]]


def call_extract(content, *, error=None, status=200, **overrides):
    opener = FakeOpener(content, status=status, error=error)
    kwargs = {"endpoint": "http://127.0.0.1:8102", "model": "qwen3.8-flash-next",
              "api_key": "synthetic-key", "timeout": 5.0}
    kwargs.update(overrides)
    items, reason = core.extract("material", opener=opener, **kwargs)
    return items, reason, opener


# ------------------------------------------------------------------ the wire

def test_extract_posts_openai_wire_request():
    body = json.dumps({"items": [{"content": "Email Anna the draft",
                                  "quote": "send it to Anna"}]})
    items, reason, opener = call_extract(body, endpoint="http://127.0.0.1:8102/",
                                         timeout=7.0)
    assert reason is None
    assert items == [{"content": "Email Anna the draft", "quote": "send it to Anna"}]
    (record,) = opener.requests
    assert record["timeout"] == 7.0
    request = record["request"]
    assert request.full_url == "http://127.0.0.1:8102/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer synthetic-key"
    payload = json.loads(request.data)
    assert payload["model"] == "qwen3.8-flash-next"  # the flare node requires it top-level
    assert payload["temperature"] == 0  # a re-run must extract the same wording
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert payload["messages"][1]["content"] == "material"


def test_extract_strips_code_fences():
    fenced = "```json\n" + json.dumps({"items": [{"content": "Book the dentist"}]}) + "\n```"
    items, reason, _ = call_extract(fenced)
    assert reason is None
    assert [i["content"] for i in items] == ["Book the dentist"]


def test_extract_drops_entries_without_string_content():
    offered = {"items": [{"content": "Keep this one"}, {"content": 42},
                         {"quote": "no content at all"}, {"content": "   "}]}
    items, reason, _ = call_extract(json.dumps(offered))
    assert reason is None  # the others survive
    assert [i["content"] for i in items] == ["Keep this one"]


def test_extract_rejects_top_level_array():
    items, reason, _ = call_extract("[1, 2, 3]")
    assert (items, reason) == ([], "malformed")


def test_extract_empty_items_is_quiet_success():
    items, reason, _ = call_extract('{"items": []}')
    assert (items, reason) == ([], None)


def test_extract_caps_and_dedupes_within_run():
    offered = {"items": [{"content": "First task"}, {"content": "Second task"},
                         {"content": " first  TASK ", "quote": "dup"},
                         {"content": "Third task"}]}
    items, reason, _ = call_extract(json.dumps(offered), max_items=2)
    assert reason is None
    assert [i["content"] for i in items] == ["First task", "Second task"]


@pytest.mark.parametrize("cap", [0, -1])
def test_nonpositive_max_items_files_nothing(cap):
    """A non-positive cap is honoured, not ignored: quiet success, no items."""
    items, reason, _ = call_extract(json.dumps({"items": [{"content": "Any task"}]}),
                                    max_items=cap)
    assert (items, reason) == ([], None)


# ---------------------------------------------------------------- failure paths

def test_extract_http_error_maps_to_status():
    error = urllib.error.HTTPError("http://127.0.0.1:8102/v1/chat/completions",
                                   500, "Internal Server Error", None, None)
    items, reason, _ = call_extract('{"items": []}', error=error)
    assert items == [] and reason == "http_500"


def test_extract_transport_and_timeout_map_to_transport():
    for error in (urllib.error.URLError("no route to host"), TimeoutError("slow")):
        items, reason, _ = call_extract('{"items": []}', error=error)
        assert items == [] and reason == "transport"


@pytest.mark.parametrize("missing", [
    {"api_key": ""}, {"endpoint": ""}, {"model": ""},
    {"api_key": None}, {"endpoint": None}, {"model": None},
])
def test_extract_not_configured_makes_no_requests(missing):
    opener = FakeOpener('{"items": []}')
    kwargs = {"endpoint": "http://127.0.0.1:8102", "model": "m",
              "api_key": "k", "timeout": 1.0}
    kwargs.update(missing)
    items, reason = core.extract("material", opener=opener, **kwargs)
    assert (items, reason) == ([], "not_configured")
    assert opener.requests == []


# --------------------------------------------------------------------- keys

def test_item_key_ignores_case_padding_and_punctuation():
    variants = ["  Buy   MILK.  ", "buy milk!", "\tbuy milk\n", "Buy milk."]
    assert len({core.item_key(v) for v in variants}) == 1
    assert core.item_key("Buy milk").startswith("intake:")
    assert core.item_key("Buy milk") != core.item_key("Sell milk")


def test_clip_preserves_head_and_tail():
    assert core.clip("short text", 100) == "short text"
    text = "HEAD" + "x" * 200 + "TAIL"
    clipped = core.clip(text, 60)
    assert len(clipped) <= 60
    assert clipped.startswith("HEAD") and clipped.endswith("TAIL")
    assert core.CLIP_MARKER in clipped
    assert core.clip(text, 60) == clipped  # stable marker, deterministic output


# ------------------------------------------------------------- covey seam

def _ingest(extractor, covey, text="material", classifier=None, **overrides):
    """Drive core.ingest; default classifier labels nothing so unrelated tests
    never reach the network (the real classifier would hit a blocked socket)."""
    kwargs = {"source": "cli", "endpoint": "http://127.0.0.1:8102",
              "model": "m", "api_key": "k", "timeout": 1.0,
              "max_chars": 100, "max_items": 10, "call_covey": covey}
    kwargs.update(overrides)
    return core.ingest(text, extractor=extractor,
                       classifier=classifier or (lambda item: None), **kwargs)


def test_ingest_records_covey_argv(monkeypatch):
    """Full real path: clip -> meta list (grounding) -> extract (dedupe by
    item_key) -> session lookup -> register -> submit.

    The case/punctuation duplicate must collapse and be submitted exactly
    once; every call carries its explicit argv. urlopen is the same seam
    test_meta_classifier drives the classifier through."""
    covey = FakeCovey(submits=[{"ok": True, "data": {"meta_task_id": "meta_1"}}])
    offered = {"items": [{"content": "Email Anna the draft", "quote": "send it to Anna"},
                         {"content": " email ANNA the draft. ", "quote": None}]}
    sent = []

    def fake_urlopen(request, *_a, **_kw):
        sent.append(request)
        return FakeReply(chat_body(json.dumps(offered)))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    long_text = "A" * 80 + " ask " + "B" * 80
    report = _ingest(None, covey, text=long_text, max_chars=100)
    # extract received the already-clipped text; nothing is filed yet, so no grounding section
    assert json.loads(sent[0].data)["messages"][1]["content"] == core.clip(long_text, 100)
    key = core.item_key("Email Anna the draft")
    assert report["status"] == "ok" and report["reason"] is None
    assert report["submitted"] == [{"content": "Email Anna the draft", "key": key,
                                    "urgency": None, "meta_task_id": "meta_1"}]
    assert report["existing"] == [] and report["failed"] == []
    assert covey.heads == [("meta", "list"), ("session", "active-for-principal"),
                           ("session", "register"), ("meta", "submit")]
    _listing, lookup, register, submit = covey.calls
    assert lookup[lookup.index("--agent-principal-id") + 1] == core.INTAKE_PRINCIPAL
    assert lookup[lookup.index("--role") + 1] == "orchestrator"
    assert register[register.index("--agent-principal-id") + 1] == core.INTAKE_PRINCIPAL
    assert register[register.index("--role") + 1] == "orchestrator"
    assert submit[submit.index("--session-token") + 1] == "tok-1"
    assert submit[submit.index("--idempotency-key") + 1] == key
    assert submit[submit.index("--prompt-text") + 1] == "Email Anna the draft\n\nSource: cli"


def test_submit_failure_is_isolated_and_marks_partial():
    covey = FakeCovey(submits=[{"ok": True, "data": {"meta_task_id": "meta_1"}},
                               {"ok": False, "code": "permission_denied",
                                "message": "wrong role"}])
    items = [{"content": "First task", "quote": None},
             {"content": "Second task", "quote": None}]
    report = _ingest(lambda text: (items, None), covey)
    assert report["status"] == "partial"
    assert [s["content"] for s in report["submitted"]] == ["First task"]
    assert report["failed"] == [{"content": "Second task", "urgency": None,
                                 "code": "permission_denied"}]


def test_session_register_failure_blocks_all_submits():
    covey = FakeCovey(register={"ok": False, "code": "db_locked", "message": "locked"})
    report = _ingest(lambda text: ([{"content": "Any task", "quote": None}], None), covey)
    assert report["status"] == "store_unavailable"
    assert report["reason"] == "store_unavailable:db_locked"
    assert report["submitted"] == [] and report["failed"] == []
    # meta list runs first (grounding), then lookup, then the failed register:
    # ZERO further calls — no submit is ever attempted without a session
    assert covey.heads == [("meta", "list"), ("session", "active-for-principal"),
                           ("session", "register")]


def test_ingest_quiet_when_nothing_actionable():
    covey = FakeCovey()
    report = _ingest(lambda text: ([], None), covey)
    assert report == {"status": "ok", "submitted": [], "existing": [],
                      "failed": [], "reason": None}
    # exactly the grounding read: one paged meta list, no session, no submit
    assert covey.calls == [["meta", "list", "--limit", "500", "--offset", "0"]]


@pytest.mark.parametrize("reason,status", [
    ("not_configured", "not_configured"),
    ("transport", "model_unavailable"),
    ("http_500", "model_unavailable"),
    ("malformed", "model_unavailable"),
])
def test_ingest_maps_extraction_gaps(reason, status):
    covey = FakeCovey()
    report = _ingest(lambda text: ([], reason), covey)
    assert report["status"] == status
    assert report["reason"] == reason
    assert report["submitted"] == [] and report["failed"] == []
    assert report["existing"] == []
    # the store read precedes extraction; the gap stops everything after it
    assert covey.calls == [["meta", "list", "--limit", "500", "--offset", "0"]]


def test_active_session_is_reused_and_never_reregistered():
    covey = FakeCovey(lookup={"ok": True, "data": {"session_token": "tok-live"}})
    report = _ingest(lambda text: ([{"content": "File the expense report",
                                     "quote": None}], None), covey)
    assert report["status"] == "ok"
    assert covey.heads == [("meta", "list"), ("session", "active-for-principal"),
                           ("meta", "submit")]  # no register call, ever
    (submit,) = covey.submits()
    assert submit[submit.index("--session-token") + 1] == "tok-live"


def test_register_conflict_refalls_back_to_the_racer():
    covey = FakeCovey(lookup=[{"ok": False, "code": "not_found"},
                              {"ok": True, "data": {"session_token": "tok-race"}}],
                     register={"ok": False, "code": "conflict",
                               "message": "session already active"})
    report = _ingest(lambda text: ([{"content": "Send the invoice", "quote": None}], None), covey)
    assert report["status"] == "ok"
    assert covey.heads == [("meta", "list"), ("session", "active-for-principal"),
                           ("session", "register"), ("session", "active-for-principal"),
                           ("meta", "submit")]
    (submit,) = covey.submits()  # the registration we lost to IS what we wanted
    assert submit[submit.index("--session-token") + 1] == "tok-race"


def test_meta_list_skips_work_already_filed_and_skips_the_classifier():
    filed = "Email Anna the draft\n\nUrgency: high\n\nSource: earlier"
    covey = FakeCovey(rows={"ok": True, "data": [{"prompt_text": filed}]})
    labeled = []

    def classifier(item):
        labeled.append(item["content"])
        return "low"

    report = _ingest(lambda text: ([{"content": "Email Anna the draft", "quote": None},
                                    {"content": "Book the dentist", "quote": None}], None),
                     covey, classifier=classifier)
    assert report["status"] == "ok"
    assert report["existing"] == [{"content": "Email Anna the draft",
                                   "key": core.item_key("Email Anna the draft")}]
    # only the fresh item is submitted, and the classifier saw only the fresh item
    assert [s["content"] for s in report["submitted"]] == ["Book the dentist"]
    assert [p.splitlines()[0] for p in (a[a.index("--prompt-text") + 1]
                                        for a in covey.submits())] == ["Book the dentist"]
    assert labeled == ["Book the dentist"]


def test_unreadable_meta_list_skips_nothing():
    for rows in ({"ok": False, "code": "db_locked"}, RuntimeError("bridge died")):
        covey = FakeCovey(rows=rows)
        report = _ingest(lambda text: ([{"content": "File the expense report",
                                         "quote": None}], None), covey)
        assert report["status"] == "ok"
        assert report["existing"] == []
        assert len(covey.submits()) == 1


def test_existing_contents_pages_the_store_to_exhaustion():
    """The CLI page defaults to the 50 newest; the read must page to the end
    or a shared worklist silently re-files old work. Scripted pages are
    served verbatim, so the exact paged argv is pinned."""
    page1 = [{"prompt_text": f"Routine task {i}"} for i in range(500)]
    page2 = [{"prompt_text": "Renew the domain"},
             {"prompt_text": "Routine task 500"}, {"prompt_text": "Routine task 501"}]
    covey = FakeCovey(rows=[{"ok": True, "data": page1}, {"ok": True, "data": page2}])
    report = _ingest(lambda text: ([{"content": "Renew the domain", "quote": None}], None), covey)
    listed = [argv for argv in covey.calls if argv[:2] == ["meta", "list"]]
    assert listed == [
        ["meta", "list", "--limit", "500", "--offset", "0"],
        ["meta", "list", "--limit", "500", "--offset", "500"]]
    # page-2 norms are visible to the dedupe: same work, never re-filed
    assert report["existing"] == [{"content": "Renew the domain",
                                   "key": core.item_key("Renew the domain")}]
    assert report["status"] == "ok" and not covey.submits()


def test_mid_paging_failure_keeps_what_was_read():
    first = [{"prompt_text": f"Routine task {i}"} for i in range(499)]
    first.append({"prompt_text": "File the expense report"})
    covey = FakeCovey(rows=[{"ok": True, "data": first},
                            {"ok": False, "code": "db_locked", "message": "locked"}])
    report = _ingest(lambda text: ([{"content": "File the expense report",
                                     "quote": None}], None), covey)
    # the second page died, but page 1's norms still dedupe: no crash, no re-file
    assert report["existing"] == [{"content": "File the expense report",
                                   "key": core.item_key("File the expense report")}]
    assert not covey.submits()


def test_extract_lists_filed_work_in_the_user_message():
    """The paraphrase defense: the prompt itself forbids restating filed
    follow-ups (the store's first-line dedupe can only recognize what the
    model repeats). No filed work -> no instruction section at all."""
    opener = FakeOpener('{"items": []}')
    items, reason = core.extract("the transcript", endpoint="http://127.0.0.1:8102",
                                 model="m", api_key="k", timeout=1.0, opener=opener,
                                 existing=["Email Anna the draft", "Book the dentist"])
    assert (items, reason) == ([], None)
    payload = json.loads(opener.requests[0]["request"].data)
    assert payload["messages"][1]["content"] == (
        "the transcript\n\nAlready filed as open follow-ups — do NOT restate "
        "any of these (or a rewording of one):\n"
        "- Email Anna the draft\n- Book the dentist")
    plain = FakeOpener('{"items": []}')
    core.extract("the transcript", endpoint="http://127.0.0.1:8102", model="m",
                 api_key="k", timeout=1.0, opener=plain, existing=[])
    assert json.loads(plain.requests[0]["request"].data)["messages"][1]["content"] \
        == "the transcript"


def test_injected_extractor_never_receives_the_filed_list():
    """The injection contract: extractor(text) ONLY. A second argument would
    TypeError loudly here; filed work must not leak through the seam — the
    fresh item still files normally alongside an unrelated filed row."""
    covey = FakeCovey(rows={"ok": True, "data": [{"prompt_text": "Book the dentist"}]})
    seen = []

    def extractor(text):  # strict one-positional signature guards the contract
        seen.append(text)
        return ([{"content": "Email Anna the draft", "quote": None}], None)

    report = _ingest(extractor, covey)
    assert seen == ["material"]
    assert report["status"] == "ok"
    assert [s["content"] for s in report["submitted"]] == ["Email Anna the draft"]
    assert len(covey.submits()) == 1


# ------------------------------------------------------------------- urgency

def test_classify_urgency_posts_typed_choice_request():
    opener = FakeOpener(raw=json.dumps(
        {"answers": {"urgency": {"choice": "high"}}}).encode())
    label = core.classify_urgency({"content": "Pay Bob the 50 he is owed",
                                   "quote": "I owe Bob 50, settle this week"},
                                  endpoint="http://127.0.0.1:8102/", model="qv",
                                  api_key="synthetic-key", timeout=1.5, opener=opener)
    assert label == "high"
    (record,) = opener.requests
    assert record["timeout"] == 1.5
    request = record["request"]
    assert request.full_url == "http://127.0.0.1:8102/v1/systemone"
    assert request.get_header("Authorization") == "Bearer synthetic-key"
    payload = json.loads(request.data)
    assert payload["model"] == "qv"
    head = payload["questions"]["urgency"]
    assert head["type"] == "choice"
    assert set(head["criteria"]) == set(core.URGENCY_LABELS)
    assert "Pay Bob the 50 he is owed" in payload["state"]
    assert "I owe Bob 50, settle this week" in payload["state"]  # quote grounds the label


def test_classify_urgency_returns_none_never_raises():
    base = {"endpoint": "http://127.0.0.1:8102", "model": "m", "api_key": "k", "timeout": 1.0}
    item = {"content": "Water the plants", "quote": None}
    off_vocab = FakeOpener(raw=json.dumps({"answers": {"urgency": {"choice": "urgent"}}}).encode())
    assert core.classify_urgency(item, opener=off_vocab, **base) is None
    down = FakeOpener(raw=b"{}", error=urllib.error.URLError("down"))
    assert core.classify_urgency(item, opener=down, **base) is None
    bad_gateway = FakeOpener(raw=json.dumps({"answers": {"urgency": {"choice": "high"}}}).encode(),
                             status=503)
    assert core.classify_urgency(item, opener=bad_gateway, **base) is None
    unconfigured = FakeOpener(raw=b"{}")
    assert core.classify_urgency(item, opener=unconfigured, **{**base, "api_key": ""}) is None
    assert unconfigured.requests == []  # missing config: zero requests


def test_submit_prompt_carries_urgency_between_item_and_source():
    covey = FakeCovey()
    report = _ingest(lambda text: ([{"content": "Email Anna the draft", "quote": None}], None),
                     covey, classifier=lambda item: "high")
    (submit,) = covey.submits()
    assert submit[submit.index("--prompt-text") + 1] == (
        "Email Anna the draft\n\nUrgency: high\n\nSource: cli")
    assert report["submitted"][0]["urgency"] == "high"


@pytest.mark.parametrize("label", [None, "urgent", 7])
def test_submit_omits_urgency_line_when_label_unusable(label):
    covey = FakeCovey()
    report = _ingest(lambda text: ([{"content": "Water the plants", "quote": None}], None),
                     covey, classifier=lambda item, v=label: v)
    (submit,) = covey.submits()
    assert submit[submit.index("--prompt-text") + 1] == "Water the plants\n\nSource: cli"
    # one truth: an unusable label is nothing — not in the prompt, not in the row
    assert report["submitted"][0]["urgency"] is None


# ---------------------------------------------------------------------- tool

def test_tool_dry_run_never_touches_store(load_plugin, monkeypatch, tmp_path):
    h = load_plugin("intake", {"api_key_env": "INTAKE_TEST_KEY",
                               "db_path": str(tmp_path / "covey.db")})
    monkeypatch.setenv("INTAKE_TEST_KEY", "synthetic-key")
    seen = []

    def fake_extract(text, **kwargs):
        seen.append(text)
        return [{"content": "Email Anna the draft", "quote": None}], None

    def fake_annotate(items, **kwargs):  # control the label without the network
        for item in items:
            item["urgency"] = "high"

    def never(argv, **kwargs):
        raise AssertionError("dry_run must not touch the covey store")

    monkeypatch.setattr(h.module.core, "extract", fake_extract)
    monkeypatch.setattr(h.module.core, "annotate_urgency", fake_annotate)
    monkeypatch.setattr(h.module, "run_covey_command", never)
    result = json.loads(h.tool("intake", {"text": "meeting notes", "dry_run": True}))
    assert seen == ["meeting notes"]
    assert result["status"] == "ok" and result["reason"] is None
    assert result["items"] == [{"content": "Email Anna the draft", "quote": None,
                                "urgency": "high"}]  # the preview shows what WOULD be filed


def test_tool_dry_run_works_without_any_store_config(load_plugin, monkeypatch):
    """Preview is the point before a store is configured: empty and relative
    db_path must not block dry_run — the store gate guards submissions only.
    The preview still carries urgency, via the injected classifier seam."""
    for db in ("", "relative/covey.db"):
        h = load_plugin("intake", {"api_key_env": "INTAKE_TEST_KEY", "db_path": db})
        monkeypatch.setenv("INTAKE_TEST_KEY", "synthetic-key")
        monkeypatch.setattr(h.module.core, "extract",
                            lambda text, **kw: ([{"content": "Email Anna the draft",
                                                  "quote": None}], None))
        monkeypatch.setattr(h.module.core, "classify_urgency",
                            lambda item, **kw: "high")
        monkeypatch.setattr(h.module, "run_covey_command",
                            lambda argv, **kw: (_ for _ in ()).throw(
                                AssertionError("dry_run must not touch the store")))
        result = json.loads(h.tool("intake", {"text": "notes", "dry_run": True}))
        assert result["status"] == "ok"
        assert result["items"][0]["content"] == "Email Anna the draft"
        assert result["items"][0]["urgency"] == "high"


def test_tool_requires_exactly_one_of_text_or_path(load_plugin, tmp_path):
    h = load_plugin("intake", {"db_path": str(tmp_path / "covey.db")})
    assert json.loads(h.tool("intake", {}))["status"] == "error"
    both = json.loads(h.tool("intake", {"text": "a", "path": str(tmp_path / "x.txt")}))
    assert both["status"] == "error"
    # "" is PROVIDED text: with a path too, it is still both-given
    empty_pair = json.loads(h.tool("intake", {"text": "", "path": str(tmp_path / "x.txt")}))
    assert empty_pair["status"] == "error"
    missing = json.loads(h.tool("intake", {"path": str(tmp_path / "missing.txt")}))
    assert missing["status"] == "error" and missing["reason"] == "not_a_regular_file"
    directory = json.loads(h.tool("intake", {"path": str(tmp_path)}))
    assert directory["status"] == "error" and directory["reason"] == "not_a_regular_file"


def test_tool_quiet_ok_on_empty_input(load_plugin, monkeypatch, tmp_path):
    """Quiet input says what it means: an ok that files nothing, with the
    model and the store both untouched (their seams would raise)."""
    h = load_plugin("intake", {"api_key_env": "INTAKE_TEST_KEY",
                               "db_path": str(tmp_path / "covey.db")})
    monkeypatch.setenv("INTAKE_TEST_KEY", "synthetic-key")
    monkeypatch.setattr(h.module.core, "extract",
                        lambda text, **kw: (_ for _ in ()).throw(
                            AssertionError("empty input must not reach the model")))
    monkeypatch.setattr(h.module, "run_covey_command",
                        lambda argv, **kw: (_ for _ in ()).throw(
                            AssertionError("empty input must not reach the store")))
    assert json.loads(h.tool("intake", {"text": ""})) == {
        "status": "ok", "submitted": [], "existing": [], "failed": [], "reason": None}
    assert json.loads(h.tool("intake", {"text": "   \n\t "}))["status"] == "ok"
    blank = tmp_path / "blank.txt"
    blank.write_text("   ")
    assert json.loads(h.tool("intake", {"path": str(blank)})) == {
        "status": "ok", "submitted": [], "existing": [], "failed": [], "reason": None}


@pytest.mark.parametrize("db", ["", "relative/covey.db"])
def test_tool_store_unavailable_without_absolute_db(load_plugin, monkeypatch, db):
    h = load_plugin("intake", {"api_key_env": "INTAKE_TEST_KEY", "db_path": db})
    monkeypatch.setenv("INTAKE_TEST_KEY", "synthetic-key")
    result = json.loads(h.tool("intake", {"text": "material"}))
    assert result["status"] == "store_unavailable"
    assert result["reason"]


def test_tool_passes_explicit_db_to_the_bridge(load_plugin, monkeypatch, tmp_path):
    db = str(tmp_path / "covey.db")
    h = load_plugin("intake", {"api_key_env": "INTAKE_TEST_KEY", "db_path": db})
    monkeypatch.setenv("INTAKE_TEST_KEY", "synthetic-key")
    monkeypatch.setattr(h.module.core, "extract",
                        lambda text, **kw: ([{"content": "Email Anna the draft",
                                              "quote": None}], None))
    monkeypatch.setattr(h.module.core, "classify_urgency", lambda item, **kw: None)
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((list(argv), kwargs))
        head = tuple(argv[:2])
        if head == ("session", "active-for-principal"):
            return {"ok": True, "data": {"session_token": "tok-1"}}
        if head == ("meta", "list"):
            return {"ok": True, "data": []}
        return {"ok": True, "data": {"meta_task_id": "meta_1"}}

    monkeypatch.setattr(h.module, "run_covey_command", fake_run)
    result = json.loads(h.tool("intake", {"text": "material", "source": "cli"}))
    assert result["status"] == "ok"
    assert result["submitted"][0]["meta_task_id"] == "meta_1"
    assert [tuple(argv[:2]) for argv, _kwargs in seen] == [
        ("meta", "list"), ("session", "active-for-principal"), ("meta", "submit")]
    for _argv, kwargs in seen:  # explicit db on EVERY call, never ambient
        assert kwargs["db_path"] == db
        assert kwargs["timeout"] == 10.0
        assert isinstance(kwargs["binary"], str) and kwargs["binary"]


# ------------------------------------------------------------------- manifest

def test_manifest_declares_intake_tool_and_full_config_schema():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert manifest["name"] == "intake" == PLUGIN_DIR.name
    assert manifest["provides_tools"] == ["intake"]
    assert manifest["manifest_version"] == 2 and manifest["api_version"] == 1
    schema = manifest["config_schema"]
    assert set(schema) == {"binary", "db_path", "endpoint", "served_model",
                           "api_key_env", "max_chars", "max_items", "timeout",
                           "classifier_endpoint", "classifier_timeout"}
    for entry in schema.values():  # operator-facing law: every key is complete
        assert entry.get("type") and "default" in entry and entry.get("description")
