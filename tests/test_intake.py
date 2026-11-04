import json
import urllib.error
from datetime import date
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
    verbatim — that is how the /v1/systemone answers envelope is scripted.
    A CALLABLE ``raw`` is invoked with the request and returns the bytes, so
    per-item responses can depend on what was actually asked (the threaded
    urgency fan-out must be answerable item by item)."""

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
        raw = self.raw(request) if callable(self.raw) else self.raw
        body = raw if raw is not None else chat_body(self.content)
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
    assert items == [{"content": "Email Anna the draft", "quote": "send it to Anna",
                      "due": None}]
    (record,) = opener.requests
    assert record["timeout"] == 7.0
    request = record["request"]
    assert request.full_url == "http://127.0.0.1:8102/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer synthetic-key"
    payload = json.loads(request.data)
    assert payload["model"] == "qwen3.8-flash-next"  # the flare node requires it top-level
    assert payload["temperature"] == 0  # a re-run must extract the same wording
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    # no explicit today: the real date is resolved at call time so the model
    # can turn "by Friday" into an absolute due date
    assert payload["messages"][0]["content"] == (
        core._SYSTEM_PROMPT + f" Today's date is {date.today().isoformat()}.")
    assert payload["messages"][1]["content"] == "material"


def test_extract_states_the_given_today_for_relative_deadlines():
    """A test-pinned date in the system prompt keeps relative-deadline
    extraction reproducible instead of drifting with the calendar."""
    items, reason, opener = call_extract('{"items": []}', today="2026-03-05")
    assert (items, reason) == ([], None)
    payload = json.loads(opener.requests[0]["request"].data)
    assert payload["messages"][0]["content"] == (
        core._SYSTEM_PROMPT + " Today's date is 2026-03-05.")


def test_extract_canonicalizes_due_and_drops_unusable_deadlines():
    """Only a real calendar date survives, canonicalized; anything the model
    could not resolve becomes no deadline — never a dropped item. Pinning
    what ``date.fromisoformat`` actually accepts: compact dates parse, the
    single-digit month/day form does not."""
    offered = {"items": [
        {"content": "Task valid", "due": "2026-11-13"},
        {"content": "Task compact", "due": "20261113"},
        {"content": "Task unpadded", "due": "2026-11-4"},
        {"content": "Task impossible", "due": "2026-13-45"},
        {"content": "Task weekday", "due": "friday"},
        {"content": "Task numeric", "due": 7},
        {"content": "Task missing"}]}
    items, reason, _ = call_extract(json.dumps(offered))
    assert reason is None
    assert items == [
        {"content": "Task valid", "quote": None, "due": "2026-11-13"},
        {"content": "Task compact", "quote": None, "due": "2026-11-13"},
        {"content": "Task unpadded", "quote": None, "due": None},
        {"content": "Task impossible", "quote": None, "due": None},
        {"content": "Task weekday", "quote": None, "due": None},
        {"content": "Task numeric", "quote": None, "due": None},
        {"content": "Task missing", "quote": None, "due": None}]


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
                                    "urgency": None, "due": None,
                                    "meta_task_id": "meta_1"}]
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
                                 "due": None, "code": "permission_denied"}]


def test_submit_conflict_is_existing_work_not_a_failure():
    """ok:false code==conflict means a concurrent run already filed the key:
    same work, one task — reported under existing, never as a failure, and
    it does NOT make the run partial. Every other refusal stays a failure."""
    covey = FakeCovey(submits=[{"ok": False, "code": "conflict",
                                "message": "idempotency key already used"},
                               {"ok": True, "data": {"meta_task_id": "meta_2"}}])
    items = [{"content": "Concurrent one", "quote": None},
             {"content": "Fresh one", "quote": None}]
    report = _ingest(lambda text: (items, None), covey)
    assert report["status"] == "ok"
    assert report["failed"] == []
    assert report["existing"] == [{"content": "Concurrent one",
                                   "key": core.item_key("Concurrent one"),
                                   "urgency": None, "due": None,
                                   "matched_meta_task_id": None}]
    assert [s["content"] for s in report["submitted"]] == ["Fresh one"]
    assert len(covey.submits()) == 2  # the conflict came FROM the store


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
    covey = FakeCovey(rows={"ok": True,
                            "data": [{"prompt_text": filed,
                                      "meta_task_id": "meta_earlier"}]})
    labeled = []

    def classifier(item):
        labeled.append(item["content"])
        return "low"

    report = _ingest(lambda text: ([{"content": "Email Anna the draft", "quote": None},
                                    {"content": "Book the dentist", "quote": None}], None),
                     covey, classifier=classifier)
    assert report["status"] == "ok"
    # the matched row names the task already in the store
    assert report["existing"] == [{"content": "Email Anna the draft",
                                   "key": core.item_key("Email Anna the draft"),
                                   "urgency": None, "due": None,
                                   "matched_meta_task_id": "meta_earlier"}]
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
    # page-2 rows are visible to the dedupe: same work, never re-filed
    assert report["existing"] == [{"content": "Renew the domain",
                                   "key": core.item_key("Renew the domain"),
                                   "urgency": None, "due": None,
                                   "matched_meta_task_id": None}]
    assert report["status"] == "ok" and not covey.submits()
    # the map values describe the filed task, not just its norm
    assert core.existing_contents(covey)[core.normalize_item("Renew the domain")] == {
        "first": "Renew the domain", "meta_task_id": None,
        "state": None, "created": None}


def test_mid_paging_failure_keeps_what_was_read():
    first = [{"prompt_text": f"Routine task {i}"} for i in range(499)]
    first.append({"prompt_text": "File the expense report"})
    covey = FakeCovey(rows=[{"ok": True, "data": first},
                            {"ok": False, "code": "db_locked", "message": "locked"}])
    report = _ingest(lambda text: ([{"content": "File the expense report",
                                     "quote": None}], None), covey)
    # the second page died, but page 1's rows still dedupe: no crash, no re-file
    assert report["existing"] == [{"content": "File the expense report",
                                   "key": core.item_key("File the expense report"),
                                   "urgency": None, "due": None,
                                   "matched_meta_task_id": None}]
    assert not covey.submits()


def test_existing_contents_excludes_terminal_states():
    """Terminal work does not suppress a re-filing; unknown and absent
    states stay in the dedupe set — conservative against duplicates."""
    assert set(core.TERMINAL_META_STATES) == {"cancelled", "done", "completed",
                                              "failed", "abandoned"}
    rows = [{"prompt_text": "Plan the audit", "state": "planning"},
            {"prompt_text": "Stateless task"},
            {"prompt_text": "Blank state task", "state": ""},
            {"prompt_text": "Numeric state task", "state": 7},
            {"prompt_text": "Cancelled task", "state": "cancelled"},
            {"prompt_text": "Done task", "state": "done"},
            {"prompt_text": "Completed task", "state": "completed"},
            {"prompt_text": "Failed task", "state": "failed"},
            {"prompt_text": "Abandoned task", "state": "abandoned"}]
    covey = FakeCovey(rows={"ok": True, "data": rows})
    mapping = core.existing_contents(covey)
    assert set(mapping) == {core.normalize_item(t) for t in
                            ("Plan the audit", "Stateless task",
                             "Blank state task", "Numeric state task")}
    assert mapping[core.normalize_item("Plan the audit")]["state"] == "planning"
    assert mapping[core.normalize_item("Stateless task")]["state"] is None
    # behaviourally: a cancelled follow-up is open again — it files normally
    covey = FakeCovey(rows={"ok": True, "data": [rows[4]]})
    report = _ingest(lambda text: ([{"content": "Cancelled task",
                                     "quote": None}], None), covey)
    assert [s["content"] for s in report["submitted"]] == ["Cancelled task"]
    assert report["existing"] == []


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


ROWS_WITH_DATES = [
    {"prompt_text": "Filed one", "created_at": 100, "meta_task_id": "meta_1"},
    {"prompt_text": "Filed two", "created_at": 300, "meta_task_id": "meta_2"},
    {"prompt_text": "Filed three", "created_at": 200, "meta_task_id": "meta_3"},
    {"prompt_text": "Undated file"},
]


def test_grounding_lists_newest_first_and_undated_last(monkeypatch):
    """The do-not-restate list is a prompt-budget VIEW: newest filed lines
    first, undated rows last — the order the model is most likely to be
    tempted to restate."""
    seen = []

    def capture(text, **kwargs):
        seen.append(list(kwargs["existing"]))
        return ([{"content": "Task for the record", "quote": None}], None)

    monkeypatch.setattr(core, "extract", capture)
    report = _ingest(None, FakeCovey(rows={"ok": True, "data": ROWS_WITH_DATES}))
    assert report["status"] == "ok"
    assert seen == [["Filed two", "Filed three", "Filed one", "Undated file"]]


def test_grounding_cap_trims_the_view_but_never_blinds_the_dedupe(monkeypatch):
    """grounding_max caps only what the model SEES; the dedupe set stays the
    complete paged read, so a trimmed-away row still suppresses a re-file
    (and reports its matched id). 0 sends no list at all."""
    seen = []

    def capture(text, **kwargs):
        seen.append(list(kwargs["existing"]))
        return ([{"content": "Task for the record", "quote": None}], None)

    def capture_replay(text, **kwargs):
        seen.append(list(kwargs["existing"]))
        return ([{"content": "Filed one", "quote": None}], None)

    monkeypatch.setattr(core, "extract", capture)
    _ingest(None, FakeCovey(rows={"ok": True, "data": ROWS_WITH_DATES}),
            grounding_max=2)
    assert seen == [["Filed two", "Filed three"]]  # capped, still newest-first

    monkeypatch.setattr(core, "extract", capture_replay)
    covey = FakeCovey(rows={"ok": True, "data": ROWS_WITH_DATES})
    report = _ingest(None, covey, grounding_max=2)
    # the cap never reaches the model, but the store read still knows it
    assert "Filed one" not in seen[-1]
    assert report["existing"] == [{"content": "Filed one",
                                   "key": core.item_key("Filed one"),
                                   "urgency": None, "due": None,
                                   "matched_meta_task_id": "meta_1"}]
    assert not covey.submits()

    monkeypatch.setattr(core, "extract", capture)
    _ingest(None, FakeCovey(rows={"ok": True, "data": ROWS_WITH_DATES}),
            grounding_max=0)
    assert seen[-1] == []  # disabled listing; the extractor sees no list


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
    assert "Due:" not in payload["state"]  # no deadline stated, no Due line


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


def test_classify_urgency_state_appends_the_deadline_only_when_present():
    """A stated deadline grounds the label alongside the quote; an absent or
    null one adds nothing — the classifier must not invent urgency."""
    def asked_state(item):
        opener = FakeOpener(raw=json.dumps(
            {"answers": {"urgency": {"choice": "high"}}}).encode())
        core.classify_urgency(item, endpoint="http://127.0.0.1:8102", model="m",
                              api_key="k", timeout=1.0, opener=opener)
        return json.loads(opener.requests[0]["request"].data)["state"]

    base = {"content": "Submit the return", "quote": None}
    assert asked_state({**base, "due": "2026-11-13"}).endswith(
        "Submit the return\n\nDue: 2026-11-13")
    assert "Due:" not in asked_state({**base, "due": None})
    assert "Due:" not in asked_state(base)
    assert "Due:" not in asked_state({**base, "due": ""})


def _answers_by_marker(label_by_marker):
    """Build a raw() for FakeOpener: the label depends on the item actually
    asked about, so the threaded fan-out can be checked item by item."""
    def raw(request):
        state = json.loads(request.data)["state"]
        for marker, label in label_by_marker.items():
            if marker in state:
                if label == "explode":
                    raise OSError("classifier died mid-call")
                return json.dumps({"answers": {"urgency": {"choice": label}}}).encode()
        raise AssertionError(f"unscripted state: {state!r}")
    return raw


def test_urgency_fans_out_per_item_and_maps_labels_back_by_index():
    """/v1/systemone takes ONE shared state per request, so per-item urgency
    cannot be batched — it fans out over a thread pool. Results must map
    back BY INDEX (labels in item order) and a call that dies costs its
    label, never the intake."""
    opener = FakeOpener(raw=_answers_by_marker(
        {"ALPHA": "high", "BRAVO": "medium", "CHARLIE": "low", "DELTA": "explode"}))
    items = [{"content": f"{marker} the report", "quote": None, "due": None}
             for marker in ("ALPHA", "BRAVO", "CHARLIE", "DELTA")]
    core.annotate_urgency(items, classifier_endpoint="http://127.0.0.1:8102",
                          model="m", api_key="k", timeout=1.0, opener=opener)
    assert [item["urgency"] for item in items] == ["high", "medium", "low", None]
    assert len(opener.requests) == 4  # one call per item, none skipped

    calls = []

    def flaky(item):  # the injected classifier stays SEQUENTIAL, order-kept
        calls.append(item["content"])
        if item["content"].startswith("BRAVO"):
            raise RuntimeError("classifier exploded")
        return "high"

    injected = [{"content": f"{marker} the report", "quote": None}
                for marker in ("ALPHA", "BRAVO", "CHARLIE")]
    core.annotate_urgency(injected, classifier_endpoint="http://127.0.0.1:8102",
                          model="m", api_key="k", timeout=1.0, classifier=flaky)
    assert calls == ["ALPHA the report", "BRAVO the report", "CHARLIE the report"]
    assert [item["urgency"] for item in injected] == ["high", None, "high"]


def test_urgency_single_item_is_labelled_without_a_fan_out():
    """One item is labelled directly — no pool, one request."""
    opener = FakeOpener(raw=json.dumps(
        {"answers": {"urgency": {"choice": "low"}}}).encode())
    items = [{"content": "Water the plants", "quote": None, "due": None}]
    core.annotate_urgency(items, classifier_endpoint="http://127.0.0.1:8102",
                          model="m", api_key="k", timeout=1.0, opener=opener)
    assert items[0]["urgency"] == "low"
    assert len(opener.requests) == 1


def test_urgency_dead_classifier_costs_labels_never_the_intake():
    """Transport down for every call: all items carry urgency None — the
    annotation itself never raises, whatever the node does."""
    opener = FakeOpener(error=urllib.error.URLError("classifier down"))
    items = [{"content": f"Task {letter}", "quote": None} for letter in "ABC"]
    core.annotate_urgency(items, classifier_endpoint="http://127.0.0.1:8102",
                          model="m", api_key="k", timeout=1.0, opener=opener)
    assert [item["urgency"] for item in items] == [None, None, None]


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


def test_submit_prompt_stamps_due_after_urgency():
    """A stated deadline is structured prompt text between the Urgency line
    and the Source footer, and travels in the reported row."""
    covey = FakeCovey()
    report = _ingest(lambda text: ([{"content": "Email Anna the draft",
                                     "quote": None, "due": "2026-11-13"}], None),
                     covey, classifier=lambda item: "high")
    (submit,) = covey.submits()
    assert submit[submit.index("--prompt-text") + 1] == (
        "Email Anna the draft\n\nUrgency: high\n\nDue: 2026-11-13\n\nSource: cli")
    assert report["submitted"][0]["due"] == "2026-11-13"


@pytest.mark.parametrize("due", [None, "", 7])
def test_submit_omits_due_line_when_deadline_unusable(due):
    """Only a truthy string due earns a Due line — same discipline as the
    Urgency line: decoration never half-appears."""
    covey = FakeCovey()
    report = _ingest(lambda text: ([{"content": "Water the plants", "quote": None,
                                     "due": due}], None), covey)
    (submit,) = covey.submits()
    assert submit[submit.index("--prompt-text") + 1] == "Water the plants\n\nSource: cli"
    assert report["submitted"][0]["due"] == due


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


def _wire_filing_plugin(h, monkeypatch, submits=None):
    """Model and bridge seams for filing tests: one item files ok unless the
    submit envelopes are scripted otherwise."""
    monkeypatch.setenv("INTAKE_TEST_KEY", "synthetic-key")
    monkeypatch.setattr(h.module.core, "extract",
                        lambda text, **kw: ([{"content": "Email Anna the draft",
                                              "quote": None, "due": None}], None))
    monkeypatch.setattr(h.module.core, "classify_urgency", lambda item, **kw: None)
    scripted = list(submits) if submits is not None else [
        {"ok": True, "data": {"meta_task_id": "meta_1"}}]

    def fake_run(argv, **kwargs):
        head = tuple(argv[:2])
        if head == ("session", "active-for-principal"):
            return {"ok": True, "data": {"session_token": "tok-1"}}
        if head == ("meta", "list"):
            return {"ok": True, "data": []}
        if not scripted:
            raise AssertionError("scripted submits exhausted")
        return scripted.pop(0) if len(scripted) > 1 else scripted[0]

    monkeypatch.setattr(h.module, "run_covey_command", fake_run)


def test_decision_log_appends_one_line_per_filing_run(load_plugin, monkeypatch, tmp_path):
    """The audit trail: one JSON line per ok/partial run that FILED, MATCHED
    or LOST something — ts, source, status, submitted, existing, failed —
    appended, never rewritten. Runs that decided nothing log nothing (the
    volume meter is the store, not this log)."""
    db = str(tmp_path / "covey.db")
    h = load_plugin("intake", {"api_key_env": "INTAKE_TEST_KEY", "db_path": db})
    _wire_filing_plugin(h, monkeypatch, submits=[
        {"ok": True, "data": {"meta_task_id": "meta_1"}},
        {"ok": False, "code": "db_locked", "message": "locked"}])
    sidecar = tmp_path / "covey.db.intake.jsonl"
    first = json.loads(h.tool("intake", {"text": "material", "source": "cli"}))
    assert first["status"] == "ok"
    second = json.loads(h.tool("intake", {"text": "material", "source": "cli"}))
    assert second["status"] == "partial"
    lines = sidecar.read_text().splitlines()
    assert len(lines) == 2  # append-only sidecar next to the store
    a, b = (json.loads(line) for line in lines)
    assert set(a) == {"ts", "source", "status", "submitted", "existing", "failed"}
    assert a["status"] == "ok" and a["source"] == "cli" and a["ts"]
    assert a["submitted"] == first["submitted"]
    assert a["existing"] == [] and a["failed"] == []
    assert b["status"] == "partial" and b["failed"] == second["failed"]


def test_decision_log_custom_path_wins_and_unwritable_never_blocks(load_plugin, monkeypatch, tmp_path):
    """decision_log overrides the sidecar default; logging never blocks
    intake — an unwritable path is a debug note, the report comes back
    unaltered."""
    db = str(tmp_path / "covey.db")
    custom = tmp_path / "audit" / "decisions.jsonl"
    custom.parent.mkdir()
    h = load_plugin("intake", {"api_key_env": "INTAKE_TEST_KEY", "db_path": db,
                               "decision_log": str(custom)})
    _wire_filing_plugin(h, monkeypatch)
    assert json.loads(h.tool("intake", {"text": "material"}))["status"] == "ok"
    assert len(custom.read_text().splitlines()) == 1
    assert not (tmp_path / "covey.db.intake.jsonl").exists()  # sidecar unused

    h2 = load_plugin("intake", {"api_key_env": "INTAKE_TEST_KEY", "db_path": db,
                                "decision_log": str(tmp_path / "no-dir" / "d.jsonl")})
    _wire_filing_plugin(h2, monkeypatch)
    result = json.loads(h2.tool("intake", {"text": "material"}))
    assert result["status"] == "ok"
    assert result["submitted"][0]["meta_task_id"] == "meta_1"


def test_decision_log_untouched_by_dry_run_and_store_unavailable(load_plugin, monkeypatch, tmp_path):
    """Only real filings are logged: a preview and a store-less run never
    create a decision log."""
    db = str(tmp_path / "covey.db")
    h = load_plugin("intake", {"api_key_env": "INTAKE_TEST_KEY", "db_path": db})
    monkeypatch.setenv("INTAKE_TEST_KEY", "synthetic-key")
    monkeypatch.setattr(h.module.core, "extract",
                        lambda text, **kw: ([{"content": "Email Anna the draft",
                                              "quote": None}], None))
    monkeypatch.setattr(h.module.core, "classify_urgency", lambda item, **kw: None)
    monkeypatch.setattr(h.module, "run_covey_command",
                        lambda argv, **kw: (_ for _ in ()).throw(
                            AssertionError("dry_run must not touch the store")))
    assert json.loads(h.tool("intake", {"text": "notes",
                                        "dry_run": True}))["status"] == "ok"
    assert not (tmp_path / "covey.db.intake.jsonl").exists()
    h2 = load_plugin("intake", {"api_key_env": "INTAKE_TEST_KEY", "db_path": ""})
    assert json.loads(h2.tool("intake", {"text": "material"}))["status"] == \
        "store_unavailable"
    assert not (Path.cwd() / ".intake.jsonl").exists()  # the empty-db default too


def test_decision_log_records_decisions_not_quiet_runs(load_plugin, monkeypatch, tmp_path):
    """A run that extracted nothing decides nothing: status ok with every
    list empty appends NO line — even after the sidecar already exists from
    a real filing run. The log records decisions, not material volume."""
    db = str(tmp_path / "covey.db")
    h = load_plugin("intake", {"api_key_env": "INTAKE_TEST_KEY", "db_path": db})
    _wire_filing_plugin(h, monkeypatch)
    sidecar = tmp_path / "covey.db.intake.jsonl"
    # a real filing first, so "unchanged" is a meaningful claim
    assert json.loads(h.tool("intake", {"text": "material"}))["status"] == "ok"
    assert len(sidecar.read_text().splitlines()) == 1
    monkeypatch.setattr(h.module.core, "extract", lambda text, **kw: ([], None))
    quiet = json.loads(h.tool("intake", {"text": "plenty of text, no work"}))
    assert quiet == {"status": "ok", "submitted": [], "existing": [],
                     "failed": [], "reason": None}
    assert len(sidecar.read_text().splitlines()) == 1  # no line for no decisions


def test_decision_log_surrogate_source_never_escapes_the_writer(load_plugin, monkeypatch, tmp_path):
    """A lone surrogate in source (argv bytes through the tool args) breaks
    the utf-8 write mid-line — AFTER the store was already mutated. That
    ValueError is caught like an OSError: the call still returns its report
    intact, the half-line simply never lands."""
    db = str(tmp_path / "covey.db")
    h = load_plugin("intake", {"api_key_env": "INTAKE_TEST_KEY", "db_path": db})
    _wire_filing_plugin(h, monkeypatch)
    sidecar = tmp_path / "covey.db.intake.jsonl"
    result = json.loads(h.tool("intake", {"text": "material", "source": "x\udcff"}))
    assert result["status"] == "ok"  # no raise, whatever the writer did
    assert result["submitted"][0]["meta_task_id"] == "meta_1"
    assert not sidecar.exists() or sidecar.read_text() == ""


# ------------------------------------------------------------------- manifest

def test_manifest_declares_intake_tool_and_full_config_schema():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert manifest["name"] == "intake" == PLUGIN_DIR.name
    assert manifest["version"] == "0.2.0"
    assert manifest["provides_tools"] == ["intake"]
    assert manifest["manifest_version"] == 2 and manifest["api_version"] == 1
    schema = manifest["config_schema"]
    assert set(schema) == {"binary", "db_path", "endpoint", "served_model",
                           "api_key_env", "max_chars", "max_items", "timeout",
                           "classifier_endpoint", "classifier_timeout",
                           "grounding_max", "decision_log"}
    assert schema["grounding_max"]["default"] == 100
    assert schema["decision_log"]["default"] == ""
    for entry in schema.values():  # operator-facing law: every key is complete
        assert entry.get("type") and "default" in entry and entry.get("description")
