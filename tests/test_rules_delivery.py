"""Behavioural coverage for the rules plugin: glob semantics, priority order and
one-by-one reminder delivery: every matching call delivers ALL the specific
rules that triggered on it plus exactly ONE extension-wide rule (highest
priority first) until that library is drained; nothing is capped, held back or
silently dropped, and every rule still arrives at most once per session.

Everything is driven through the real plugin: rule files on disk, the native
tool_execution middleware, the rendered system-prompt sections and rules_explain.
Rule bodies are padded to exact sizes so a delivery could never hide behind a
per-call character budget.
"""
from __future__ import annotations

import json
import re
from typing import Final

import pytest

# (name, priority) already listed in the expected delivery/index order; the
# alphabet disagrees with it and "c-med"/"c-medium"/"c-unknown" tie as MEDIUM.
PRIORITY_ORDER = [("e-crit", "CRITICAL"), ("f-crit", "critical"), ("d-high", "HIGH"),
                  ("c-med", None), ("c-medium", "MEDIUM"), ("c-unknown", "URGENT"),
                  ("b-low", "LOW"), ("a-ref", "REFERENCE")]
EXPECTED = [name for name, _ in PRIORITY_ORDER]
TOOL = "tools: [terminal]"
# A terminal call naming a .rs file: matches `tools: [terminal]` rules and
# extension-wide `**/*.rs` / `*.rs` rules alike.
RS_ARGS: Final = {"command": "cat src/x.rs", "path": "src/x.rs"}


def _meta(cwd, session_id="sess-1", profile="main"):
    return {"session_id": session_id, "model": "m", "provider": "p",
            "platform": "cli", "profile_name": profile, "cwd": str(cwd)}


def _write_rule(path, front_matter="", body="BODY"):
    text = (f"---\n{front_matter}\n---\n{body}\n") if front_matter else f"{body}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fm(*lines, prio=None):
    extra = [f"constraints:\n  priority: {prio}"] if prio else []
    return "\n".join([*lines, *extra])


def _ext_fm(prio=None, glob="**/*.rs"):
    """Front matter of an extension-wide rule: no tools, no condition, one glob."""
    return _fm(f"globs: {json.dumps([glob])}", prio=prio)


def _cond(entries):
    return f"condition: {json.dumps(entries)}"


def _make(tmp_path, specs):
    """Write (name, front matter, body size) rules; return {name: exact body}."""
    bodies = {}
    for name, front, size in specs:
        bodies[name] = f"BODY-{name} ".ljust(size, "x")
        _write_rule(tmp_path / "global" / f"{name}.md", front, bodies[name])
    return bodies


def _harness(load_plugin, tmp_path, monkeypatch, cwd=None):
    monkeypatch.chdir(tmp_path)
    h = load_plugin("rules", settings={
        "rule_dirs": [str(tmp_path / "global")],
        "ttsr_enabled": True, "disabled_rules": [],
        "rules_explain_enabled": True})
    h.manager.render_system_prompt_sections(_meta(cwd or tmp_path))
    return h


def _run(h, tool="terminal", args=None, next_call=None, session_id="sess-1"):
    return h.middleware("tool_execution", tool_name=tool,
                        args=args or {"command": "ls"},
                        next_call=next_call or (lambda a=None: "EXECUTED"),
                        session_id=session_id, task_id="t1", tool_call_id="c1",
                        turn_id="turn-1", api_request_id="api-1")


def _json_ok(a=None):
    return json.dumps({"success": True})


def _boom(a=None):
    raise RuntimeError("tool blew up")


def _structural(a=None):
    return {"ok": True}


def _rs_call(h, next_call=None, session_id="sess-1"):
    """A matching call: terminal + a `.rs` path — hits tool rules and ext rules."""
    return _run(h, args=RS_ARGS, next_call=next_call, session_id=session_id)


def _sequence(h, count, next_call=None, session_id="sess-1"):
    """`count` successive matching calls, e.g. to watch a library drain."""
    return [_rs_call(h, next_call, session_id) for _ in range(count)]


def _explain(h):
    return json.loads(h.tool("rules_explain", {"paths": ["a.py"], "tool_name": "read_file"},
                             session_id="sess-1", task_id="t1"))


def _section(h, tmp_path, section_id):
    return {i.id: i.content
            for i in h.manager.render_system_prompt_sections(_meta(tmp_path))}[section_id]


def _rule_file(tmp_path, name):
    return str(tmp_path / "global" / f"{name}.md")


def _chunk(tmp_path, name, bodies):
    return f"{bodies[name]}\n[rule file: {_rule_file(tmp_path, name)}]"


def _joined(tmp_path, names, bodies):
    return "\n\n".join(_chunk(tmp_path, n, bodies) for n in names)


def _delivered(text, bodies):
    hit = [n for n, b in bodies.items() if b in text]
    return sorted(hit, key=lambda n: text.index(bodies[n]))


def _assert_no_held_marker(text):
    # Contract D has no held-back lane in free text or in the JSON dict.
    assert "held back" not in text and "rule_guidance_held_back" not in text


def _assert_delivers(tmp_path, bodies, out, expected):
    """The output carries exactly the `expected` rule bodies, joined in order."""
    assert _joined(tmp_path, expected, bodies) in out
    assert _delivered(out, bodies) == list(expected)
    _assert_no_held_marker(out)


def _indexed(book):
    return re.findall(r"^- \*\*(\S+?)\*\* — ", book, re.M)


# --- Contract E: the index lists only the rules nothing else delivers ----------

RANK: Final = {"CRITICAL": 0, "HIGH": 1, None: 2, "LOW": 3, "REFERENCE": 4}
BOOK_BUDGET: Final = 3600  # the rulebook body budget; the omission line rides after it
ALWAYS_ORDER: Final = [("z-crit", "CRITICAL"), ("y-high", "HIGH"), ("v-med", None),
                       ("w-med", "MEDIUM"), ("t-low", "LOW"), ("u-low", "LOW"),
                       ("a-ref", "REFERENCE"), ("b-ref", "REFERENCE"), ("c-ref", "REFERENCE")]
# Every scope that hands a rule to another lane; each is paired with a description so
# only the scope keeps it out of the index.
SCOPED_KINDS: Final = {
    "tools": TOOL,
    "condition": _cond(["alpha"]),
    "globs": 'globs: ["src/*.rs"]',
    "always+globs": 'alwaysApply: true\nglobs: ["*.rs"]',
    "always": "alwaysApply: true",
    "all-scopes": f'{TOOL}\n{_cond(["alpha"])}\nglobs: ["src/*.rs"]',
}


def _names(names):
    """At most five names, then `, +k more` for the rest."""
    head = ", ".join(names[:5])
    return head + (f", +{len(names) - 5} more" if len(names) > 5 else "")


def _book_omission(names):
    return (f"- {len(names)} lower-priority rule(s) over the rulebook budget "
            f"(read the rule files or call rules_explain): {_names(names)}")


def _book_line(tmp_path, name, desc):
    return f"- **{name}** — {desc} [{_rule_file(tmp_path, name)}]"


def _index_specs(count, prios=(None,)):
    """`count` index-only rules r00.. whose lines all have the same width."""
    return [(f"r{i:02d}", prios[i % len(prios)], f"r{i:02d} ".ljust(200, "d"))
            for i in range(count)]


def _write_index(tmp_path, specs):
    """Write the index-only rules; return their (name, expected line) in index order."""
    _make(tmp_path, [(n, _fm(f"description: {json.dumps(d)}", prio=p), 40)
                     for n, p, d in specs])
    ranked = sorted(specs, key=lambda s: (RANK[s[1]], s[0]))
    return [(n, _book_line(tmp_path, n, d)) for n, _, d in ranked]


def _fitting(rows):
    """The longest prefix of (name, line) rows whose joined text stays in budget."""
    shown = []
    for row in rows:
        if len("\n".join([*(line for _, line in shown), row[1]])) > BOOK_BUDGET:
            break
        shown.append(row)
    return shown


# --- C1: glob semantics ---------------------------------------------------------

GLOB_CASES = [
    ("**/*.py", "x.py", True), ("**/*.py", "src/x.py", True), ("**/*.py", "a/b/x.py", True),
    ("**/*.py", "x.pyc", False), ("**/*.py", "src/x.rs", False), ("**/*.py", "src/X.PY", False),
    ("src/*.py", "src/x.py", True), ("src/*.py", "src/a/x.py", False),
    ("src/*.py", "x.py", False), ("src/*.py", "a/src/x.py", False),
    ("src/?.py", "src/x.py", True), ("src/?.py", "src/xy.py", False),
    ("**/covey*/**", "covey/a.rs", True), ("**/covey*/**", "x/covey-core/y/z.rs", True),
    ("**/covey*/**", "x/other/z.rs", False), ("**/covey*/**", "covey.rs", False),
    ("docs/**", "docs/a.md", True), ("docs/**", "docs/a/b.md", True),
    ("docs/**", "docs", False), ("docs/**", "other/a.md", False),
    ("*.py", "src/deep/x.py", True), ("*.py", "x.pyc", False),
]


@pytest.mark.parametrize(("glob", "rel", "expected"), GLOB_CASES,
                         ids=[f"{g}|{r}" for g, r, _ in GLOB_CASES])
def test_glob_semantics(load_plugin, tmp_path, monkeypatch, glob, rel, expected):
    bodies = _make(tmp_path, [("g", f"globs: {json.dumps([glob])}", 40)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    out = _run(h, "read_file", {"path": rel})
    assert (bodies["g"] in out) is expected


@pytest.mark.parametrize("glob", ["**/*.py", "*.py"])
def test_path_outside_session_cwd_never_matches(load_plugin, tmp_path, monkeypatch, glob):
    proj = tmp_path / "proj"
    proj.mkdir()
    bodies = _make(tmp_path, [("g", f"globs: {json.dumps([glob])}", 40)])
    h = _harness(load_plugin, tmp_path, monkeypatch, cwd=proj)
    assert _run(h, "read_file", {"path": str(tmp_path / "other" / "x.py")}) == "EXECUTED"
    assert bodies["g"] in _run(h, "read_file", {"path": str(proj / "x.py")})  # control


def test_top_level_priority_key_still_rejects_the_file(load_plugin, tmp_path, monkeypatch):
    _make(tmp_path, [("bad", f"{TOOL}\npriority: CRITICAL", 40)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    assert _run(h) == "EXECUTED"


# --- C3: one extension-wide rule per call, nothing capped or dropped ------------

def test_every_matching_call_delivers_one_extension_wide_rule_until_drained(load_plugin, tmp_path, monkeypatch):
    # ~500-char bodies: any per-call budget would cut this set short or name
    # held-back rules; the contract promises neither.
    bodies = _make(tmp_path, [(n, _ext_fm(p), 500) for n, p in reversed(PRIORITY_ORDER)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    for name, out in zip(EXPECTED, _sequence(h, len(EXPECTED))):
        _assert_delivers(tmp_path, bodies, out, [name])  # exactly ONE rule per call
    assert _rs_call(h) == "EXECUTED"  # drained: nothing queued, nothing lost
    h.manager.render_system_prompt_sections(_meta(tmp_path, "sess-2"))  # claims are per-session
    _assert_delivers(tmp_path, bodies, _rs_call(h, session_id="sess-2"), [EXPECTED[0]])


def test_extension_wide_queue_survives_non_matching_calls(load_plugin, tmp_path, monkeypatch):
    bodies = _make(tmp_path, [(n, _ext_fm(prio="CRITICAL" if i == 0 else None), 500)
                              for i, n in enumerate(["x-first", "a-second", "b-third"])])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    for name in ["x-first", "a-second"]:
        _assert_delivers(tmp_path, bodies, _rs_call(h), [name])
        assert _run(h) == "EXECUTED"  # a path-free call matches nothing: claims untouched
    _assert_delivers(tmp_path, bodies, _rs_call(h), ["b-third"])
    assert _rs_call(h) == "EXECUTED"


def test_extension_wide_rule_larger_than_the_old_budget_arrives_whole(load_plugin, tmp_path, monkeypatch):
    bodies = _make(tmp_path, [("a-big", _ext_fm(), 9000), ("b-small", _ext_fm(), 100),
                              ("c-small", _ext_fm(), 100)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    big = _rs_call(h)
    # A truncated 3600-char prefix could not satisfy this; the whole ~9000-char
    # body rides the result, and it alone takes its one-rule turn.
    _assert_delivers(tmp_path, bodies, big, ["a-big"])
    _assert_delivers(tmp_path, bodies, _rs_call(h), ["b-small"])  # not starved by the big one
    _assert_delivers(tmp_path, bodies, _rs_call(h), ["c-small"])
    assert _rs_call(h) == "EXECUTED"


def test_specific_rules_are_never_capped_or_held_back(load_plugin, tmp_path, monkeypatch):
    names = [f"r{i:02d}" for i in range(25)]  # ~12.5k chars: no per-call budget survives
    bodies = _make(tmp_path, [(n, TOOL, 500) for n in names])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _assert_delivers(tmp_path, bodies, _run(h), names)  # ALL 25 in the first matching call
    assert _run(h) == "EXECUTED"  # each rule is still delivered exactly once per session


def test_json_results_carry_only_the_delivered_rules(load_plugin, tmp_path, monkeypatch):
    bodies = _make(tmp_path, [(n, TOOL, 500) for n in ["sp1", "sp2"]]
                   + [("e0-ext", _ext_fm("CRITICAL"), 500), ("e1-ext", _ext_fm(), 500)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    raw = _rs_call(h, _json_ok)  # specific rules are content/tool-triggered: both now,
    payload = json.loads(raw)    # plus the single CRITICAL extension-wide rule
    _assert_no_held_marker(raw)
    assert payload["rule_guidance"] == _joined(tmp_path, ["sp1", "sp2", "e0-ext"], bodies)
    assert sorted(r["source"] for r in payload["rule_refs"]) == sorted(
        _rule_file(tmp_path, n) for n in ["sp1", "sp2", "e0-ext"])  # refs: delivered only
    raw = _rs_call(h, _json_ok)  # next call: exactly the next extension-wide rule
    payload = json.loads(raw)
    _assert_no_held_marker(raw)
    assert payload["rule_guidance"] == _joined(tmp_path, ["e1-ext"], bodies)
    assert [r["source"] for r in payload["rule_refs"]] == [_rule_file(tmp_path, "e1-ext")]
    assert json.loads(_run(h, next_call=_json_ok)) == {"success": True}  # drained


def test_single_rule_output_is_result_plus_note_byte_for_byte(load_plugin, tmp_path, monkeypatch):
    bodies = _make(tmp_path, [("only-rule", _ext_fm(), 200)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    out = _rs_call(h)
    head = ("EXECUTED\n\n[RULE GUIDANCE — one-time, apply to this change]\n"
            + _chunk(tmp_path, "only-rule", bodies) + "\n[rule refs] ")
    assert out.startswith(head)  # the note is result + header + body + file line, unmodified
    refs = json.loads(out[len(head):])  # nothing is appended after the refs
    assert [r["source"] for r in refs] == [_rule_file(tmp_path, "only-rule")]
    _assert_no_held_marker(out)


ALL_MIXED: Final = ["sp1", "sp2", "sp3", "e0-ext", "e1-ext", "e2-ext"]


@pytest.mark.parametrize("mode", ["raise", "structural"])
@pytest.mark.parametrize("when", [0, 1], ids=["on-first-call", "after-one-delivery"])
def test_failed_or_structural_call_releases_what_it_claimed(load_plugin, tmp_path, monkeypatch,
                                                            mode, when):
    bodies = _make(tmp_path, [("sp1", TOOL, 500), ("sp2", TOOL, 500), ("sp3", TOOL, 500),
                              ("e0-ext", _ext_fm("CRITICAL"), 500),
                              ("e1-ext", _ext_fm(), 500), ("e2-ext", _ext_fm(), 500)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    if when:
        # one honest delivery first: the failing call below claims only the NEXT
        # extension-wide rule, so replaying must not re-send the specific ones.
        _assert_delivers(tmp_path, bodies, _rs_call(h), ALL_MIXED[:4])
    if mode == "raise":
        with pytest.raises(RuntimeError):
            _rs_call(h, _boom)
    else:
        assert _rs_call(h, _structural) == {"ok": True}  # structural output passes through
    # The next successful call replays, as one delivery, exactly the set the
    # failed call claimed — the specific rules plus the ONE extension rule it
    # had taken, and nothing that was delivered honestly before.
    replay = [ALL_MIXED[4]] if when else ALL_MIXED[:4]
    _assert_delivers(tmp_path, bodies, _rs_call(h), replay)
    for name in ALL_MIXED[5:] if when else ALL_MIXED[4:]:
        _assert_delivers(tmp_path, bodies, _rs_call(h), [name])  # drain resumes
    assert _rs_call(h) == "EXECUTED"


# --- C3: specificity ------------------------------------------------------------

SPECIFIC_KINDS: Final = [TOOL, 'globs: ["src/*.rs"]', "condition: 'x\\.rs'",
                         f'{TOOL}\nglobs: ["**/*.rs"]', _cond(["x\\.rs"])]


@pytest.mark.parametrize("specific", SPECIFIC_KINDS,
                         ids=["tool", "path-glob", "condition", "tool+ext-glob",
                              "list-condition"])
def test_specific_rules_come_with_exactly_one_extension_wide_rule(load_plugin, tmp_path,
                                                                  monkeypatch, specific):
    ext = [f"ext{i}ext" for i in range(6)]
    specs = [(n, _ext_fm("CRITICAL" if i == 0 else None,
                         glob=g), 500)
             for i, (n, g) in enumerate(zip(ext, ["**/*.rs", "*.rs"] * 3))]
    bodies = _make(tmp_path, [*specs, ("zzz-specific", _fm(specific, prio="REFERENCE"), 500)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    # The REFERENCE specific rule comes FIRST on the call that triggered it, beside
    # exactly the first (CRITICAL) extension-wide rule — never capped, never queued.
    _assert_delivers(tmp_path, bodies, _rs_call(h), ["zzz-specific", ext[0]])
    for name in ext[1:]:  # one extension-wide rule per later matching call
        _assert_delivers(tmp_path, bodies, _rs_call(h), [name])
    assert _rs_call(h) == "EXECUTED"  # the specific rule arrived once, not again


def test_late_matching_specific_rule_arrives_with_the_next_extension_rule(load_plugin, tmp_path, monkeypatch):
    ext = [f"ext{i}ext" for i in range(4)]
    bodies = _make(tmp_path, [(n, _ext_fm(), 500) for n in ext]
                   + [("late-spec", _fm(TOOL, _cond(["unsafe"])), 500)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _assert_delivers(tmp_path, bodies, _rs_call(h), [ext[0]])  # no specific matches yet
    args = {"command": "grep unsafe src/x.rs", "path": "src/x.rs"}
    out = _run(h, args=args)  # first call that matches the condition
    _assert_delivers(tmp_path, bodies, out, ["late-spec", ext[1]])  # it does not wait
    for name in ext[2:]:
        _assert_delivers(tmp_path, bodies, _rs_call(h), [name])
    assert _rs_call(h) == "EXECUTED"  # the late rule was delivered once, earlier, in its own call


# --- C2: priority order ---------------------------------------------------------

def test_reminders_are_delivered_by_priority_then_name(load_plugin, tmp_path, monkeypatch):
    bodies = _make(tmp_path, [(n, _fm(TOOL, prio=p), 40) for n, p in reversed(PRIORITY_ORDER)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    _assert_delivers(tmp_path, bodies, _run(h), EXPECTED)  # every specific rule, one call


def test_critical_extension_rules_drain_ahead_of_reference_ones(load_plugin, tmp_path, monkeypatch):
    names = [f"p{i:02d}" for i in range(12)]
    bodies = _make(tmp_path, [(n, _ext_fm("CRITICAL" if i >= 10 else "REFERENCE"), 500)
                              for i, n in enumerate(names)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    order = names[10:] + names[:10]  # CRITICAL first although alphabetically last
    for name, out in zip(order, _sequence(h, len(order))):
        _assert_delivers(tmp_path, bodies, out, [name])
    assert _run(h, args=RS_ARGS) == "EXECUTED"  # every REFERENCE rule arrived, none is queued


def test_rulebook_index_is_ordered_by_priority_then_name(load_plugin, tmp_path, monkeypatch):
    _make(tmp_path, [(n, _fm('description: "d"', prio=p), 40) for n, p in reversed(PRIORITY_ORDER)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    assert _indexed(_section(h, tmp_path, "rules.rulebook")) == EXPECTED


def test_rulebook_budget_keeps_critical_and_explain_mirrors_it(load_plugin, tmp_path, monkeypatch):
    names = [f"n{i:02d}" for i in range(60)]
    desc = f'description: "{"d" * 60}"'
    _make(tmp_path, [(n, _fm(desc, prio="CRITICAL" if i >= 50 else "REFERENCE"), 40)
                     for i, n in enumerate(names)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    book = _section(h, tmp_path, "rules.rulebook")
    indexed = _indexed(book)
    assert 10 < len(indexed) < 60
    assert indexed[:10] == names[50:]  # every CRITICAL rule is shown first...
    assert indexed[10:] == names[:len(indexed) - 10]  # ...REFERENCE fills what is left
    omitted = names[len(indexed) - 10:50]  # the rest, in index order
    assert book.splitlines()[-1] == _book_omission(omitted)
    assert len(book.splitlines()) == len(indexed) + 1  # exactly one omission line
    assert _explain(h)["budget_omissions"]["rulebook"] == omitted


def test_always_lane_drops_lowest_priority_and_explain_mirrors_it(load_plugin, tmp_path, monkeypatch):
    lanes = [("aaa", "REFERENCE", "A"), ("bbb", "LOW", "B"),
             ("ccc", "HIGH", "C"), ("zzz", "CRITICAL", "Z")]
    for name, prio, ch in lanes:
        _write_rule(tmp_path / "global" / f"{name}.md",
                    _fm("alwaysApply: true", prio=prio), ch * 1700)
    h = _harness(load_plugin, tmp_path, monkeypatch)
    always = _section(h, tmp_path, "rules.always-apply")
    assert sorted(ch for _, _, ch in lanes if ch * 100 in always) == ["C", "Z"]
    assert always.index("Z" * 100) < always.index("C" * 100)
    assert always.endswith(
        "\n\n[RULES] 2 always-apply rule(s) exceed the prompt budget and are omitted "
        "(lowest priority first): bbb, aaa; read the rule files directly.")
    assert _explain(h)["budget_omissions"]["always_apply"] == ["bbb", "aaa"]


# --- Contract E: index scope, omission notes, explain mirror ---------------------

@pytest.mark.parametrize("scope", SCOPED_KINDS.values(), ids=list(SCOPED_KINDS))
def test_scoped_rules_never_appear_in_the_rulebook_index(load_plugin, tmp_path, monkeypatch, scope):
    _make(tmp_path, [("scoped", _fm('description: "scoped-marker"', scope), 40),
                     ("sibling", 'description: "sibling-marker"', 40)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    book = _section(h, tmp_path, "rules.rulebook")
    # Only the index-only sibling is listed; nothing of the scoped rule leaks in.
    assert book == _book_line(tmp_path, "sibling", "sibling-marker")
    assert "scoped-marker" not in book and "(globs:" not in book


def test_rulebook_overflow_keeps_the_priority_prefix_and_names_the_rest(load_plugin, tmp_path,
                                                                        monkeypatch):
    rows = _write_index(tmp_path, _index_specs(40, ["REFERENCE", "LOW", None, "HIGH", "CRITICAL"]))
    shown = _fitting(rows)
    omitted = [name for name, _ in rows[len(shown):]]
    assert len(omitted) > 5  # more than five rules overflow, so `, +k more` is exercised
    h = _harness(load_plugin, tmp_path, monkeypatch)
    book = _section(h, tmp_path, "rules.rulebook")
    assert _indexed(book) == [name for name, _ in shown]  # highest-priority prefix, whole lines
    assert book == "\n".join([*(line for _, line in shown), _book_omission(omitted)])
    first, last = omitted[0], len(omitted)
    assert book.endswith(f"(read the rule files or call rules_explain): {first}, {omitted[1]}, "
                         f"{omitted[2]}, {omitted[3]}, {omitted[4]}, +{last - 5} more")
    assert f"- {last} lower-priority rule(s) over the rulebook budget" in book.splitlines()[-1]
    assert len(book) < 4000  # still inside the host's per-section cap


def test_rulebook_overflow_of_a_few_rules_names_them_all(load_plugin, tmp_path, monkeypatch):
    # Size the set from the real line width so exactly three rules fall off the end.
    width = len(_book_line(tmp_path, "r00", _index_specs(1)[0][2]))
    fit = (BOOK_BUDGET + 1) // (width + 1)
    rows = _write_index(tmp_path, _index_specs(fit + 3))
    h = _harness(load_plugin, tmp_path, monkeypatch)
    book = _section(h, tmp_path, "rules.rulebook")
    assert book == "\n".join([*(line for _, line in rows[:fit]),
                              "- 3 lower-priority rule(s) over the rulebook budget "
                              "(read the rule files or call rules_explain): "
                              f"r{fit:02d}, r{fit + 1:02d}, r{fit + 2:02d}"])


@pytest.mark.parametrize(("total", "listed"), [
    (5, "v-med, w-med, t-low"),
    (9, "v-med, w-med, t-low, u-low, a-ref, +2 more"),
], ids=["three-dropped", "seven-dropped"])
def test_always_note_names_dropped_rules_in_priority_order(load_plugin, tmp_path, monkeypatch,
                                                           total, listed):
    # 1700-char bodies: exactly two fit the budget, CRITICAL then HIGH.
    lanes = ALWAYS_ORDER[:total]
    bodies = _make(tmp_path, [(n, _fm("alwaysApply: true", prio=p), 1700)
                              for n, p in reversed(lanes)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    always = _section(h, tmp_path, "rules.always-apply")
    note = (f"[RULES] {total - 2} always-apply rule(s) exceed the prompt budget and are "
            f"omitted (lowest priority first): {listed}; read the rule files directly.")
    assert always == f"{bodies['z-crit']}\n\n{bodies['y-high']}\n\n{note}"
    for name, _prio in lanes[2:]:  # every dropped rule absent; CRITICAL and HIGH kept
        assert bodies[name] not in always


def test_explain_budget_omissions_equal_what_each_section_dropped(load_plugin, tmp_path, monkeypatch):
    lanes = ALWAYS_ORDER[:6]
    bodies = _make(tmp_path, [(n, _fm("alwaysApply: true", prio=p), 1700) for n, p in lanes])
    rows = _write_index(tmp_path, _index_specs(40, ["REFERENCE", "LOW", None, "HIGH", "CRITICAL"]))
    h = _harness(load_plugin, tmp_path, monkeypatch)
    always = _section(h, tmp_path, "rules.always-apply")
    book = _section(h, tmp_path, "rules.rulebook")
    missing_always = [n for n, _ in lanes if bodies[n] not in always]
    missing_book = [name for name, line in rows if line not in book]
    assert missing_always == ["v-med", "w-med", "t-low", "u-low"]  # control: the sections do drop
    assert 5 < len(missing_book) < 40
    omissions = _explain(h)["budget_omissions"]
    assert omissions["always_apply"] == missing_always
    assert omissions["rulebook"] == missing_book


# --- A-1: list-valued condition -------------------------------------------------

@pytest.mark.parametrize(("command", "fires"), [("echo alpha", True), ("echo beta", True),
                                                ("echo gamma", False)])
def test_list_condition_matches_when_any_entry_matches(load_plugin, tmp_path, monkeypatch,
                                                       command, fires):
    bodies = _make(tmp_path, [("lc", _fm(TOOL, _cond(["alpha", "beta"])), 40)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    assert (bodies["lc"] in _run(h, args={"command": command})) is fires


@pytest.mark.parametrize(("args", "fires"), [({"command": "echo alpha", "note": "-"}, True),
                                             ({"command": "ls", "note": "beta"}, True),
                                             ({"command": "ls", "note": "gamma"}, False)],
                         ids=["first-arg", "second-arg", "neither"])
def test_list_condition_searches_every_string_arg(load_plugin, tmp_path, monkeypatch, args, fires):
    bodies = _make(tmp_path, [("lc", _fm(TOOL, _cond(["alpha", "beta"])), 40)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    assert (bodies["lc"] in _run(h, args=args)) is fires


@pytest.mark.parametrize(("command", "fires"), [("echo UPPER", True), ("echo upper", True),
                                                ("echo LOWER", False), ("echo lower", True)])
def test_list_condition_inline_flags_stay_per_entry(load_plugin, tmp_path, monkeypatch,
                                                    command, fires):
    bodies = _make(tmp_path, [("lc", _fm(TOOL, _cond(["(?i)upper", "lower"])), 40)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    assert (bodies["lc"] in _run(h, args={"command": command})) is fires


@pytest.mark.parametrize(("command", "fires"), [("cat src/x.rs", True), ("cat src/xyrs", False)])
def test_plain_string_condition_is_unchanged(load_plugin, tmp_path, monkeypatch, command, fires):
    bodies = _make(tmp_path, [("sc", _fm(TOOL, "condition: 'x\\.rs'"), 40)])
    h = _harness(load_plugin, tmp_path, monkeypatch)
    assert (bodies["sc"] in _run(h, args={"command": command})) is fires


BAD_LISTS = {"empty-list": [], "non-str-entry": [1], "empty-entry": [""],
             "empty-second-entry": ["ok", ""], "invalid-regex": ["("],
             "invalid-second-regex": ["ok", "["], "33-entries": ["ok"] * 33,
             "4097-char-entry": ["k" + "x?" * 2048]}


@pytest.mark.parametrize("bad", BAD_LISTS.values(), ids=list(BAD_LISTS))
def test_malformed_list_condition_rejects_only_that_file(load_plugin, tmp_path, monkeypatch, bad):
    good = ["nope"] * 31 + ["ok"]  # 32 entries is the accepted maximum
    bodies = _make(tmp_path, [("bad", _fm(TOOL, _cond(bad)), 40),
                              ("good", _fm(TOOL, _cond(good)), 40)])
    h = _harness(load_plugin, tmp_path, monkeypatch)  # the plugin still loads
    out = _run(h, args={"command": "ok ls"})  # every "bad" list above would match this if kept
    assert bodies["good"] in out and bodies["bad"] not in out
