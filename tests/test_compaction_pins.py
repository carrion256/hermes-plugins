"""Configured tool pins (``pinned_tools``) hold on every reduction path.

Each test compares a pinned pair against an UNPINNED TWIN at the same position with the
same payload, so the assertion can only pass if the pin — not the position, not the
content — is what saved it. The mirror test runs the same fixture with an empty pin set
and requires the old behaviour byte for byte.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAIN = ROOT / "plugins" / "chain-compaction"
FAST = ROOT / "plugins" / "fast-compaction"
PINNED = "prewalk_ready"          # the shipped default; named literally on purpose
BIG = "R" * 9000                  # over the clip trigger, under the state budget


def _load(directory: Path, name: str):
    target = directory / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"pins_{directory.name}_{name}", target)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {directory.name}/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stages = _load(CHAIN, "stages")
pruner = _load(FAST, "pruner")


def _load_engine():
    spec = importlib.util.spec_from_file_location(
        "pins_chain_engine", CHAIN / "__init__.py", submodule_search_locations=[str(CHAIN)])
    if spec is None or spec.loader is None:
        raise ImportError("cannot load the chain-compaction engine")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pins_chain_engine"] = module    # required for `from . import stages`
    spec.loader.exec_module(module)
    return module


def tokens(text):
    return max(1, len(text) // 4)


def cfg(**over):
    """Stage config with NO pins: every test asks for a pin explicitly, so the default
    never hides a broken pin path."""
    base = dict(stages.DEFAULTS)
    base["pinned_tools"] = ()
    base["preserve_recent_messages"] = 2          # the pairs below all sit outside it
    base.update(over)
    return base


def asst(call_id: str, name: str) -> dict:
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                           "function": {"name": name, "arguments": "{}"}}]}


def result(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": BIG}


def transcript(pairs) -> list:
    """system + user, then each ``(call_id, tool_name)`` pair, then two recent turns.
    The pairs land outside the protected window, so nothing pins them positionally."""
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "do the thing"}]
    for call_id, name in pairs:
        msgs.append(asst(call_id, name))
        msgs.append(result(call_id))
    msgs.append({"role": "user", "content": "keep going"})
    msgs.append({"role": "user", "content": "and again"})
    return msgs


PAIRS = [("p0", PINNED), ("p1", "terminal"), ("p2", "terminal"), ("p3", "terminal")]


def content_of(out, call_id: str) -> str:
    return next(m["content"] for m in out if isinstance(m, dict)
                and m.get("tool_call_id") == call_id)


def surviving_ids(out) -> set:
    return {tc["id"] for m in out if isinstance(m, dict) and m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)}


# ------------------------------------------------------------------ clip

def test_clip_spares_the_pinned_result_and_cuts_its_identical_twin():
    msgs = transcript(PAIRS)
    out, stats = stages.stage_clip(msgs, cfg(pinned_tools=[PINNED]), tokens)
    # clip is position-independent: EVERY unpinned result over the trigger is cut, so the
    # pin is proved by the one that is not, not by a reduced count.
    assert stats["stage"] == "clip" and stats["touched"] == len(PAIRS) - 1
    assert [cid for cid, _ in PAIRS if "omitted" in content_of(out, cid)] == [
        "p1", "p2", "p3"]
    pinned = next(i for i, m in enumerate(msgs) if m.get("tool_call_id") == "p0")
    assert out[pinned] is msgs[pinned] and out[pinned]["content"] == BIG   # not even copied
    twin = next(m for m in out if m.get("tool_call_id") == "p1")
    assert "omitted" in twin["content"] and len(twin["content"]) < len(BIG)


def test_clip_without_pins_cuts_every_over_trigger_result():
    msgs = transcript(PAIRS)
    out, stats = stages.stage_clip(msgs, cfg(), tokens)
    assert stats["touched"] == len(PAIRS)                   # the pre-setting behaviour
    assert all("omitted" in content_of(out, cid) for cid, _ in PAIRS)


# ------------------------------------------------------------------ drop_stale

def test_drop_stale_keeps_the_pinned_pair_and_drops_its_identical_twin():
    msgs = transcript(PAIRS)
    out, stats = stages.stage_drop_stale(msgs, cfg(pinned_tools=[PINNED],
                                                   drop_stale_keep_pairs=1), tokens)
    # Candidates exclude the pin, so the keep window counts down p1,p2,p3: the two older
    # unpinned pairs go, p3 stays by the window, p0 stays by the pin.
    assert surviving_ids(out) == {"p0", "p3"} and stats["dropped_pairs"] == 2
    assert content_of(out, "p0") == BIG
    orphaned = [m["tool_call_id"] for m in out if isinstance(m, dict)
                and m.get("role") == "tool" and m["tool_call_id"] not in surviving_ids(out)]
    assert orphaned == []


def test_drop_stale_without_pins_drops_the_pair_the_pin_would_save():
    msgs = transcript(PAIRS)
    out, stats = stages.stage_drop_stale(msgs, cfg(drop_stale_keep_pairs=1), tokens)
    assert surviving_ids(out) == {"p3"} and stats["dropped_pairs"] == 3   # p0 is gone


# ------------------------------------------------------------------ prune (classifier)

class AllDropClient:
    """The classifier answering drop for every question it is asked."""

    def __init__(self):
        self.requests: list[dict[str, object]] = []

    def ask(self, state: str, questions: dict[str, object], *, caller: str,
            timeout: float | None = None) -> dict[str, object]:
        self.requests.append(questions)
        return {"answers": {name: "drop" for name in questions}}


def _prune_engine(pinned):
    settings = cfg(pinned_tools=pinned)
    client = AllDropClient()
    engine = _load_engine()._engine_class()(settings, pruner, client)
    return engine, settings, client


def test_prune_keeps_the_pinned_pair_and_drops_its_identical_twin():
    engine, settings, client = _prune_engine([PINNED])
    msgs = transcript(PAIRS)
    pruned, stats = engine._stage_prune(msgs, settings)
    assert surviving_ids(pruned) == {"p0"} and stats["dropped"] == 3
    assert content_of(pruned, "p0") == BIG
    # a pinned pair never even reaches the classifier: 3 calls x 2 questions
    assert len(client.requests) == 1 and len(client.requests[0]) == 6
    users = [m for m in pruned if m.get("role") == "user"]
    assert len(users) == 3 and all(any(u is original for original in msgs) for u in users)


def test_prune_without_pins_drops_the_pair_the_pin_would_save():
    engine, settings, client = _prune_engine([])
    pruned, stats = engine._stage_prune(transcript(PAIRS), settings)
    assert surviving_ids(pruned) == set() and stats["dropped"] == 4
    assert len(client.requests[0]) == 8                     # all four were asked about


# ------------------------------------------------------------------ byte-identical default

def _rewritten(output: list, source: list) -> set:
    """Indices where the stage produced a different object (a stage may only ever copy a
    message it actually rewrites)."""
    assert len(output) == len(source), "the stage removed messages it was not meant to"
    return {i for i, (o, m) in enumerate(zip(output, source)) if o is not m}


def test_empty_pin_set_is_byte_identical_to_having_no_setting_at_all():
    """The feature must not perturb an unconfigured install: not one object, not one stat."""
    absent = dict(stages.DEFAULTS, preserve_recent_messages=2)
    absent.pop("pinned_tools")
    for stage, extra in (("stage_clip", {"clip_trigger_chars": 8000, "clip_head_chars": 100}),
                         ("stage_drop_stale", {"drop_stale_keep_pairs": 1})):
        runner = getattr(stages, stage)
        msgs = transcript(PAIRS)
        base = dict(absent, **extra)
        first, first_stats = runner(msgs, base, tokens)
        second, second_stats = runner(msgs, dict(base, pinned_tools=()), tokens)
        assert second == first and second_stats == first_stats, f"{stage} reads the key"
        if stage == "stage_drop_stale":
            # a positional removal rewrites nothing: every survivor IS the original object
            assert all(any(o is m for m in msgs) for o in first), \
                f"{stage} copied an untouched message"
            continue
        copied = _rewritten(first, msgs)
        assert copied == _rewritten(second, msgs), f"{stage} moved the rewrite line"
        assert copied, f"{stage} clipped nothing; the fixture does not exercise it"
        # a copy that equals its source means a message was copied without being changed
        assert all(first[i] != msgs[i] for i in copied), \
            f"{stage} copied an untouched message"


def test_pinned_names_parses_config_shapes_and_ignores_noise():
    assert stages.pinned_names({"pinned_tools": ["a", " b ", ""]}) == frozenset({"a", "b"})
    assert stages.pinned_names({"pinned_tools": None}) == frozenset()
    assert stages.pinned_names({}) == frozenset()
    assert stages.pinned_names(None) == frozenset()


# ------------------------------------------------------------------ fast-compaction pruner

def test_fast_compaction_pruner_pins_by_tool_name_and_eligibility_follows():
    msgs = transcript(PAIRS)
    unpinned = pruner.collect_tool_calls(msgs, 2)
    assert {cid for cid, record in unpinned.items() if not record["pinned"]} == {
        "p0", "p1", "p2", "p3"}
    pinned = pruner.collect_tool_calls(msgs, 2, pinned_tools=frozenset({PINNED}))
    assert pinned["p0"]["pinned"] is True
    assert [r["call_id"] for r in pruner.eligible_calls(pinned)] == ["p1", "p2", "p3"]
    # apply_decisions honours it: a drop verdict for the pinned call changes nothing
    decisions = {cid: {"call": False, "result": False} for cid in pinned}
    out, stats = pruner.apply_decisions(msgs, pinned, decisions, 300)
    assert surviving_ids(out) == {"p0"} and stats["dropped"] == 3


def test_pruner_pin_parser_rejects_a_config_that_is_not_a_name_list():
    assert pruner.parse_pinned_tools(None) == frozenset()
    assert pruner.parse_pinned_tools(["prewalk_ready", " x "]) == frozenset(
        {"prewalk_ready", "x"})
    # the SHAPE of the value is a type error ...
    for bad in ("prewalk_ready", 7, {"a": 1}, [3], ["ok", 3]):
        try:
            pruner.parse_pinned_tools(bad)
        except TypeError:
            continue
        raise AssertionError(f"parse_pinned_tools accepted {bad!r} as a value")
    # ... while a name that is blank is a value error
    for bad in (["", "   "], ["ok", "  \t"]):
        try:
            pruner.parse_pinned_tools(bad)
        except ValueError:
            continue
        raise AssertionError(f"parse_pinned_tools accepted a blank name {bad!r}")
