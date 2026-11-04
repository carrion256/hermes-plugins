"""intake core — arbitrary text in, validated follow-up items and Covey calls out.

The whole pipeline lives here so scripts can import it without a Hermes host:
no ctx, no host imports. ``extract`` talks the OpenAI wire to the profile's
flare chat node (which REQUIRES a top-level ``model`` field) and reads only
``choices[0].message.content`` — the model is a reasoning model and its
``reasoning_content`` is never trusted. ``register_session``/``submit_items``
drive the Covey store through an injected ``call_covey(args) -> envelope``
callable; the caller owns binary and db_path (explicit on every call, never
ambient). Dedupe is delegated to the store: every submit carries an
idempotency key derived from the item, so a replay is a no-op there and this
module keeps no local seen-set.

Failure contract (repo discipline): no public function raises. A model outage,
a dead store, a bad config is a ``status``/``reason`` string in the result,
never an exception. The bearer key is supplied by the caller (read from the
environment at call time) and is never stored, logged, or echoed; neither is
the session token.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import secrets
import string
import urllib.error
import urllib.request

CLIP_MARKER = "[... clipped ...]"
MAX_RESPONSE_BYTES = 1024 * 1024  # hard cap on a chat response read (1 MiB)
DEFAULT_MAX_ITEMS = 20

_SYSTEM_PROMPT = (
    "You extract actionable follow-ups from the material the user provides. "
    'Return ONLY a JSON object of the shape {"items": [{"content": "...", '
    '"quote": "...", "due": "..."}]}. Each item is ONE follow-up that the '
    "owner of the material must do or explicitly committed to: "
    "self-contained (readable without the source), phrased as an imperative "
    "command, at most 120 characters. Never fabricate work that the material "
    "does not commit or require; never merge several actions into one item. "
    "`quote` is a short verbatim span from the material proving the "
    "commitment; omit it when no span supports the item. An item MAY carry "
    "\"due\": \"YYYY-MM-DD\" — the follow-up's deadline resolved against "
    "today's date (a relative phrase like \"by Friday\" becomes an absolute "
    "date); null or omitted when no deadline is stated. When nothing "
    'qualifies return {"items": []}. '
    "No prose, no markdown, no code fences — JSON only."
)


def clip(text, max_chars):
    """Head+tail clip with a stable middle marker; text at/below the cap is
    returned untouched. Deterministic: same input, same clipped output."""
    if not isinstance(text, str) or max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max_chars - len(CLIP_MARKER)
    if keep <= 0:
        return text[:max_chars]
    head = keep // 2
    tail = keep - head
    return text[:head] + CLIP_MARKER + text[-tail:]


def normalize_item(content):
    """Canonical form used ONLY for the dedupe key: collapse whitespace runs,
    strip surrounding punctuation/whitespace, casefold. Display text is never
    rewritten through here."""
    if not isinstance(content, str):
        return ""
    collapsed = re.sub(r"\s+", " ", content).strip()
    return collapsed.strip(string.punctuation + string.whitespace).casefold()


def item_key(content):
    """Stable idempotency key for a follow-up; equal-after-normalization
    content maps to the same key so the store dedupes replays for us."""
    return "intake:" + hashlib.sha256(
        normalize_item(content).encode("utf-8")).hexdigest()[:16]


def _strip_fences(text):
    text = text.strip()
    if text.startswith("```"):  # tolerate one surrounding ```json fence
        text = re.sub(r"\A```[A-Za-z0-9_+-]*[ \t]*\n?", "", text)
        text = re.sub(r"\n?```[ \t]*\Z", "", text)
    return text.strip()


def _parse_items(content, max_items):
    """Strict extraction parsing: ONE object with an ``items`` list of objects
    carrying a non-empty string ``content``. Malformed entries are dropped;
    a valid empty list is a quiet success; anything else is malformed. Every
    kept item carries ``due``: the entry's ISO date validated with
    ``datetime.date.fromisoformat`` and canonicalized, None when absent or
    not a real date — a deadline the model could not resolve is no
    deadline."""
    try:
        parsed = json.loads(_strip_fences(content))
    except ValueError:
        return [], "malformed"
    if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
        return [], "malformed"  # top-level arrays/strings/scalars are rejected
    if not parsed["items"]:
        return [], None  # the model found nothing actionable: quiet success
    if max_items <= 0:
        return [], None  # a non-positive cap files nothing: honoured, not ignored
    items, seen = [], set()
    for entry in parsed["items"]:
        if not isinstance(entry, dict):
            continue
        text = entry.get("content")
        if not isinstance(text, str) or not normalize_item(text):
            continue
        quote = entry.get("quote")
        if quote is not None and not isinstance(quote, str):
            quote = None
        due = entry.get("due")
        if isinstance(due, str):
            try:  # only a real calendar date survives; anything else is None
                due = datetime.date.fromisoformat(due).isoformat()
            except ValueError:
                due = None
        else:
            due = None
        key = item_key(text)
        if key in seen:
            continue  # order-preserving in-run dedupe; the store still gates replays
        seen.add(key)
        items.append({"content": text, "quote": quote, "due": due})
        if len(items) >= max_items:
            break
    if not items:
        return [], "malformed"  # entries were offered, but none carried valid content
    return items, None


def extract(text, *, endpoint, model, api_key, timeout, opener=None,
            max_items=DEFAULT_MAX_ITEMS, existing=None, today=None):
    """(items, reason) from the chat model; never raises.

    items are ``{"content": str, "quote": str|None, "due": str|None}``,
    validated, ordered, deduped by item_key, capped at max_items. reason is
    None on success and one of ``not_configured`` (no key/model/endpoint —
    zero requests made), ``transport`` (transport error or timeout),
    ``http_<code>`` (non-2xx),
    ``malformed`` (unusable body or unparseable model output). The request
    always carries the top-level ``model`` field the flare node requires,
    ``temperature: 0`` (a re-run over the same material must extract the
    same wording — the store's first-line dedupe can only recognize what
    the model repeats), and the Bearer key; only
    ``choices[0].message.content`` is read, never ``reasoning_content``.
    ``existing`` lists follow-ups already filed for this store: the prompt
    tells the model not to restate any of them, which is the paraphrase
    defense the string-level dedupe cannot provide on its own. ``today``
    (an ISO ``YYYY-MM-DD`` string, defaulting to the real date at call
    time) is stated in the system prompt so the model can resolve relative
    deadlines ("by Friday") into absolute per-item ``due`` dates; with no
    deadline stated an item carries ``due`` as None. ``opener``
    (an object with ``.open(request, timeout=...)``) injects the transport;
    the default is urlopen.
    """
    if not (endpoint or "").strip() or not (model or "").strip() or not (api_key or "").strip():
        return [], "not_configured"
    if today is None:
        today = datetime.date.today().isoformat()
    user = text
    if existing:
        listed = "\n".join(f"- {line}" for line in existing)
        user = (f"{text}\n\nAlready filed as open follow-ups — do NOT restate "
                f"any of these (or a rewording of one):\n{listed}")
    try:
        body = json.dumps({
            "model": model,
            "temperature": 0,
            "messages": [{"role": "system",
                          "content": _SYSTEM_PROMPT + f" Today's date is {today}."},
                         {"role": "user", "content": user}],
        }, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return [], "malformed"
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": "Bearer " + api_key})
    open_request = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_request(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0))
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        code = int(error.code)
        try:
            error.close()
        except Exception:
            pass
        return [], f"http_{code}"
    except Exception:  # sanitized catch-all: never echo the request or any body
        return [], "transport"
    if not 200 <= status < 300:
        return [], f"http_{status}"
    if len(raw) > MAX_RESPONSE_BYTES:
        return [], "malformed"
    try:
        payload = json.loads(raw)
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError, UnicodeDecodeError):
        return [], "malformed"
    if not isinstance(content, str):
        return [], "malformed"
    return _parse_items(content, max_items)


INTAKE_PRINCIPAL = "intake"
URGENCY_LABELS = ("high", "medium", "low")
_URGENCY_CRITERIA = {
    "high": "Someone is blocked or waiting on this right now, it carries an "
            "explicit deadline today or tomorrow, or money, health, safety, or "
            "a promise to a named person depends on it.",
    "medium": "A real commitment or follow-up with no near deadline: someone "
              "will be pleased by it, it unblocks later work, or it was "
              "promised for this week.",
    "low": "Routine maintenance or a courtesy follow-up: nobody is waiting, no "
           "deadline is attached, delaying it costs nothing.",
}


def obtain_session(call_covey):
    """(token, reason) — the profile's long-lived orchestrator session for
    principal ``intake``, REUSED across runs. Never raises.

    The store makes this the only correct shape (verified against the real
    binary 2026-11-04): a session is principal-scoped — registering again
    while one is active is ``conflict: session already active`` — and
    idempotency keys are SESSION-scoped, so per-run sessions would silently
    defeat replay dedupe. A live session is kept until explicitly exited, so
    the flow is: look it up, register only when absent, and on a register
    conflict (a racing process won the race) look it up once more. Never
    exit it here: this session IS the intake's persistent identity.
    ``meta submit`` requires role orchestrator (executors get
    permission_denied, wrong role)."""

    def _lookup():
        try:
            payload = call_covey(["session", "active-for-principal",
                                  "--agent-principal-id", INTAKE_PRINCIPAL,
                                  "--role", "orchestrator"])
        except Exception:
            return None
        if isinstance(payload, dict) and payload.get("ok") is True:
            data = payload.get("data")
            token = data.get("session_token") if isinstance(data, dict) else None
            if isinstance(token, str) and token:
                return token
        return None

    token = _lookup()
    if token is not None:
        return token, None
    argv = ["session", "register",
            "--agent-principal-id", INTAKE_PRINCIPAL,
            "--agent-instance-id", f"intake-{os.getpid()}-{secrets.token_hex(4)}",
            "--role", "orchestrator"]
    try:
        payload = call_covey(argv)
    except Exception:
        return None, "store_unavailable:error"
    if isinstance(payload, dict) and payload.get("ok") is True:
        data = payload.get("data")
        token = data.get("session_token") if isinstance(data, dict) else None
        if isinstance(token, str) and token:
            return token, None
        return None, "store_unavailable:invalid_envelope"
    code = payload.get("code") if isinstance(payload, dict) else None
    if code == "conflict":
        token = _lookup()  # the concurrent registration we lost to IS what we wanted
        if token is not None:
            return token, None
    return None, f"store_unavailable:{code if isinstance(code, str) and code else 'error'}"


# Meta-task states that mean the work is finished. A follow-up whose only
# match sits in one of these does NOT suppress re-filing — a cancelled or
# done commitment mentioned again in fresh material is new work. The
# completion-ish states are workflow-driven and were unobserved at writing;
# add any seen in practice to this tuple.
TERMINAL_META_STATES = ("cancelled", "done", "completed", "failed", "abandoned")


def existing_contents(call_covey):
    """``{normalized: row}`` of follow-ups already filed in the store; row is
    ``{"first": str, "meta_task_id": str|None, "state": str|None,
    "created": int|None}`` describing the filed task (the last row seen for
    a normalized line wins, mirroring the store's own order).

    Cross-run dedupe cannot ride on idempotency keys (they are
    session-scoped, verified), so the store itself is queried: a follow-up
    whose first line matches an existing task's first line is the SAME work
    and is skipped, never re-filed. The display lines double as the
    extractor's do-not-restate list — the paraphrase defense the string
    match cannot provide alone. The CLI page defaults to the 50 newest
    tasks, which on a shared worklist is a silent truncation of exactly
    this dedupe; the read therefore pages (500 at a time) to exhaustion.
    An unreadable store yields what was already read — the worst case
    degrades to a within-session replay, which the idempotency key still
    collapses. Rows whose ``state`` is in TERMINAL_META_STATES are EXCLUDED
    — terminal work does not suppress re-filing — while unknown and absent
    states stay in, conservative against duplicates. Completion states
    observed in practice should be added to that constant."""
    seen = {}
    offset = 0
    while True:
        try:
            payload = call_covey(["meta", "list", "--limit", "500",
                                  "--offset", str(offset)])
        except Exception:
            return seen
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return seen
        rows = payload.get("data")
        if not isinstance(rows, list):
            return seen
        for row in rows:
            prompt = row.get("prompt_text") if isinstance(row, dict) else None
            if isinstance(prompt, str) and prompt:
                first = prompt.splitlines()[0]
                norm = normalize_item(first)
                if not norm:
                    continue
                state = row.get("state")
                state = state if isinstance(state, str) and state else None
                if state in TERMINAL_META_STATES:
                    continue  # finished work never suppresses a re-filing
                meta_task_id = row.get("meta_task_id")
                created = row.get("created_at")
                seen[norm] = {
                    "first": first,
                    "meta_task_id": meta_task_id
                    if isinstance(meta_task_id, str) and meta_task_id else None,
                    "state": state,
                    "created": created if isinstance(created, int) else None,
                }
        if len(rows) < 500:
            return seen
        offset += 500


def classify_urgency(item, *, endpoint, model, api_key, timeout, opener=None):
    """Urgency label for ONE item from the profile classifier — a typed
    choice head on /v1/systemone, the same node/wire the classifier plugins
    use. Never raises; returns None whenever the label cannot be had
    (unconfigured, transport, unusable body, off-vocabulary answer).
    Urgency DECORATES intake; a dead classifier costs the label, never the
    intake. The quoted evidence — and the item's stated deadline, when it
    carries one — rides in the state so the label is grounded in what the
    material actually says and stays aware of when the work is due."""
    if not (endpoint or "").strip() or not (model or "").strip() or not (api_key or "").strip():
        return None
    content = item["content"] if isinstance(item, dict) else str(item)
    quote = item.get("quote") if isinstance(item, dict) else None
    state = f"{content}\n\nQuote from the material: {quote}" \
        if isinstance(quote, str) and quote.strip() else content
    due = item.get("due") if isinstance(item, dict) else None
    if isinstance(due, str) and due:
        state += f"\n\nDue: {due}"
    try:
        body = json.dumps({"model": model, "state": state,
                           "questions": {"urgency": {
                               "type": "choice", "criteria": _URGENCY_CRITERIA}}},
                          ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return None
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/systemone", data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": "Bearer " + api_key})
    open_request = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_request(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0))
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except Exception:  # sanitized catch-all: never echo request or body
        return None
    if not 200 <= status < 300 or len(raw) > MAX_RESPONSE_BYTES:
        return None
    try:
        payload = json.loads(raw)
        label = payload["answers"]["urgency"]["choice"]
    except (KeyError, IndexError, TypeError, ValueError, UnicodeDecodeError):
        return None
    return label if isinstance(label, str) and label in URGENCY_LABELS else None


def annotate_urgency(items, *, classifier_endpoint, model, api_key, timeout,
                     classifier=None, opener=None):
    """Attach ``urgency`` to each item dict in place (None when unavailable).

    ``classifier`` injects the labeler for tests (item -> label|None) and
    runs sequentially. Against the real node the per-item calls run
    CONCURRENTLY: /v1/systemone takes ONE shared ``state`` per request, so
    several question heads over identical criteria return identical
    probabilities — per-item urgency cannot be batched into one request and
    instead fans out over a ThreadPoolExecutor (min(8, len(items)) workers),
    results mapped back by index so item order is preserved. A single item
    is labelled directly, without an executor. Never raises either way: a
    failed call costs its label, never the intake."""
    targets = [item for item in items if isinstance(item, dict)]
    if not targets:
        return

    def label(item):
        try:
            return classify_urgency(item, endpoint=classifier_endpoint,
                                    model=model, api_key=api_key,
                                    timeout=timeout, opener=opener)
        except Exception:
            return None

    if classifier is not None:
        got_list = []
        for item in targets:
            try:
                got_list.append(classifier(item))
            except Exception:
                got_list.append(None)
    elif len(targets) == 1:
        got_list = [label(targets[0])]
    else:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, len(targets))) as pool:
            got_list = list(pool.map(label, targets))
    for item, got in zip(targets, got_list):
        # Normalize HERE so the item, the prompt line, and the reported
        # row carry one truth: a valid label or nothing.
        item["urgency"] = got if got in URGENCY_LABELS else None


def submit_items(items, source, token, call_covey):
    """File every item as a meta task; never raises; one failure never stops
    the rest.

    The idempotency key is item_key(content) — content ONLY: the same work
    is the same task even if the wording drifts or a later run would label
    it differently. ``meta submit`` has no priority field, so urgency and a
    stated deadline are structured lines inside the prompt text — Urgency
    then Due, each emitted only when it carries a valid value — between the
    item and the Source provenance footer; a replay of a filed key keeps its
    original text — urgency and due annotate first filing, they never
    rewrite a filed task. A submit answered ``ok:false`` with
    ``code == "conflict"`` is NOT a failure: the key is already filed (the
    usual cause is a concurrent run), so the row is reported under
    ``existing`` instead; every other refusal stays a failure."""
    submitted, failed, existing = [], [], []
    for item in items:
        content = item["content"] if isinstance(item, dict) else item
        urgency = item.get("urgency") if isinstance(item, dict) else None
        due = item.get("due") if isinstance(item, dict) else None
        key = item_key(content)
        lines = [content, ""]
        if isinstance(urgency, str) and urgency in URGENCY_LABELS:
            lines += [f"Urgency: {urgency}", ""]
        if isinstance(due, str) and due:
            lines += [f"Due: {due}", ""]
        lines += [f"Source: {source}"]
        argv = ["meta", "submit",
                "--session-token", token,
                "--prompt-text", "\n".join(lines),
                "--idempotency-key", key]
        try:
            payload = call_covey(argv)
        except Exception:
            payload = None
        if isinstance(payload, dict) and payload.get("ok") is True:
            data = payload.get("data")
            meta_task_id = data.get("meta_task_id") if isinstance(data, dict) else None
            if isinstance(meta_task_id, str) and meta_task_id:
                submitted.append({"content": content, "key": key,
                                  "urgency": urgency, "due": due,
                                  "meta_task_id": meta_task_id})
                continue
        code = payload.get("code") if isinstance(payload, dict) else None
        if code == "conflict":
            # already filed under this key: the work exists in the store,
            # a concurrent run won the race — never report it as failed
            existing.append({"content": content, "key": key,
                             "urgency": urgency, "due": due})
            continue
        failed.append({"content": content, "urgency": urgency, "due": due,
                       "code": code if isinstance(code, str) and code else "error"})
    return {"submitted": submitted, "failed": failed, "existing": existing}


def ingest(text, *, source, endpoint, model, api_key, timeout, max_chars,
           max_items, call_covey, extractor=None, classifier=None,
           classifier_endpoint="", classifier_timeout=1.5, grounding_max=100):
    """The composition: clip -> meta-list read -> extract (grounded against
    what is filed) -> obtain_session -> partition -> urgency (survivors
    only) -> submit.

    Returns ``{"status", "submitted", "existing", "failed", "reason"}`` and
    never raises. status: ``ok`` (nothing failed — an empty quiet extraction
    is ok), ``not_configured``, ``model_unavailable`` (transport/http/
    malformed), ``store_unavailable`` (no session could be obtained),
    ``partial`` (at least one submit failed; the succeeded ones stay
    submitted). ``existing`` lists items already filed — matched by
    normalized first line, or claimed at submit time through an
    idempotency-key conflict (a concurrent run filed the same work) — same
    work, one task; every row carries ``matched_meta_task_id`` from the
    store read when the match is known, None when it is not. Ordering is
    deliberate: the store is read BEFORE extraction so the model can be
    told not to restate filed work (paraphrase-proofing the dedupe), and
    urgency runs ONLY on survivors, so a replay costs one grounded
    extraction call and nothing else — no submits, no classifier calls.
    ``grounding_max`` caps only the do-not-restate list SHOWN to the
    extractor (newest first by ``created``, undated rows last); 0 sends no
    list at all — the dedupe set itself is always the full paged read.
    ``extractor`` and ``classifier`` inject the model calls for tests."""
    clipped = clip(text, max_chars)
    existing = existing_contents(call_covey)
    grounded = []
    if grounding_max > 0 and existing:
        # a prompt-budget view only: newest filed lines first, undated last;
        # dedupe still consults the complete paged read
        ordered = sorted(existing.values(),
                         key=lambda row: (row.get("created") is not None,
                                          row.get("created") or 0),
                         reverse=True)
        grounded = [row["first"] for row in ordered[:grounding_max]]
    if extractor is not None:
        items, reason = extractor(clipped)
    else:
        items, reason = extract(clipped, endpoint=endpoint,
                                model=model, api_key=api_key, timeout=timeout,
                                max_items=max_items, existing=grounded)
    if reason == "not_configured":
        return {"status": "not_configured", "submitted": [], "existing": [],
                "failed": [], "reason": reason}
    if reason is not None:
        return {"status": "model_unavailable", "submitted": [], "existing": [],
                "failed": [], "reason": reason}
    if not items:
        return {"status": "ok", "submitted": [], "existing": [],
                "failed": [], "reason": None}
    token, session_reason = obtain_session(call_covey)
    if token is None:
        return {"status": "store_unavailable", "submitted": [], "existing": [],
                "failed": [], "reason": session_reason}
    fresh, dupes = [], []
    for item in items:
        content = item["content"] if isinstance(item, dict) else item
        urgency = item.get("urgency") if isinstance(item, dict) else None
        due = item.get("due") if isinstance(item, dict) else None
        matched = existing.get(normalize_item(content))
        if matched is not None:
            dupes.append({"content": content, "key": item_key(content),
                          "urgency": urgency, "due": due,
                          "matched_meta_task_id": matched.get("meta_task_id")})
        else:
            fresh.append(item)
    annotate_urgency(fresh, classifier_endpoint=classifier_endpoint or endpoint,
                     model=model, api_key=api_key, timeout=classifier_timeout,
                     classifier=classifier)
    outcome = submit_items(fresh, source, token, call_covey)
    # a store-side key conflict (a concurrent run filed the same work) is
    # existing work, not a failure — same row shape as the first-line matches
    dupes += [{"content": row["content"], "key": row["key"],
               "urgency": row["urgency"], "due": row["due"],
               "matched_meta_task_id": None} for row in outcome["existing"]]
    status = "partial" if outcome["failed"] else "ok"
    return {"status": status, "submitted": outcome["submitted"],
            "existing": dupes, "failed": outcome["failed"], "reason": None}
