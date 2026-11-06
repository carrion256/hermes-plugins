"""The ``drop_reasoning`` stage: pure chain-of-thought deletion from stale turns.

The contract under test is that a *pure deletion* happened and nothing else: visible
text, ``tool_calls``, user and tool messages come back as the same bytes and the same
objects, the newest turns are not touched at all, and a transcript with no reasoning is
an ordinary stage miss (``StageError``), never a silent no-op.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "chain-compaction"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"dropr_{name}", PLUGIN / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load chain-compaction/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stages = _load("stages")

OLD_THINKING = "A" * 50
# chars_saved counts the readable strings of what was deleted: every key and every value
# string of the removed payload (structure itself is free). Spelled out per payload so a
# counter that charged kept content, or skipped a field, fails the test.
DETAILS = [{"type": "text", "text": "opaque"}]                       # 4+4+4+6 = 18 chars
DETAILS_CHARS = 18
THINKING_BLOCK = {"type": "thinking", "thinking": "deep thought"}    # 4+8+8+12 = 32 chars
REDACTED_BLOCK = {"type": "redacted_thinking", "data": "base64blob"}  # 4+17+4+10 = 35
TOOL_CALLS = [{"id": "c0", "type": "function",
               "function": {"name": "terminal", "arguments": "{}"}}]


def tokens(text):
    return max(1, len(text) // 4)


def cfg(**over):
    base = dict(stages.DEFAULTS)
    base["preserve_recent_messages"] = 2      # only the last two rows are protected
    base.update(over)
    return base


def old_assistant(**over):
    """A stale assistant turn carrying every reasoning carrier the stage knows."""
    message = {"role": "assistant", "content": "the visible answer",
               "reasoning": OLD_THINKING,
               "reasoning_content": OLD_THINKING,
               "reasoning_details": DETAILS,
               "api_content": "pre-strip bytes that must not replay",
               "tool_calls": TOOL_CALLS}
    message.update(over)
    return message


def transcript() -> list:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "fix the loader test"},
        old_assistant(),
        {"role": "tool", "tool_call_id": "c0", "content": "output"},
        # newest window: must survive completely untouched
        {"role": "assistant", "content": "working on it", "reasoning": "fresh thought"},
        {"role": "user", "content": "keep going"},
    ]


def test_stale_reasoning_is_deleted_and_everything_else_is_byte_identical():
    msgs = transcript()
    out, stats = stages.stage_drop_reasoning(msgs, cfg(), tokens)
    assert stats == {"stage": "drop_reasoning", "touched": 1,
                     "chars_saved": 2 * len(OLD_THINKING) + DETAILS_CHARS}
    stripped = out[2]
    for field in stages.REASONING_FIELDS:
        assert field not in stripped, f"{field} survived the stage"
    assert "api_content" not in stripped, "a sidecar would replay the pre-strip bytes"
    assert stripped["content"] == "the visible answer"          # visible text, byte for byte
    assert stripped["tool_calls"] is TOOL_CALLS                 # not even copied
    # untouched rows are the original objects, in the original order
    assert [i for i, m in enumerate(out) if m is not msgs[i]] == [2]
    assert out[4] is msgs[4] and out[4]["reasoning"] == "fresh thought"   # window intact
    assert out[1] is msgs[1] and out[3] is msgs[3]                        # user/tool intact


def test_thinking_blocks_go_and_visible_text_blocks_stay():
    msgs = transcript()
    msgs[2]["content"] = [THINKING_BLOCK, REDACTED_BLOCK,
                          {"type": "text", "text": "the visible answer"}]
    out, stats = stages.stage_drop_reasoning(msgs, cfg(), tokens)
    assert stats["touched"] == 1
    assert out[2]["content"] == [{"type": "text", "text": "the visible answer"}]
    assert stats["chars_saved"] == (2 * len(OLD_THINKING) + DETAILS_CHARS + 32 + 35)


def test_a_turn_with_no_reasoning_is_untouched_and_an_empty_stage_raises():
    msgs = transcript()
    for message in msgs:
        for field in stages.REASONING_FIELDS:
            message.pop(field, None)
    with pytest.raises(stages.StageError, match="no_reasoning"):
        stages.stage_drop_reasoning(msgs, cfg(), tokens)
    # and when it raises, nothing was rewritten: the input list is unchanged
    assert all(isinstance(m, dict) for m in msgs)


def test_protected_window_size_is_the_shared_preserve_recent_setting():
    msgs = transcript()
    # A window covering the whole transcript leaves no stale turn: ordinary stage miss.
    with pytest.raises(stages.StageError, match="no_reasoning"):
        stages.stage_drop_reasoning(msgs, cfg(preserve_recent_messages=6), tokens)
    # A window of one row also exposes the newest assistant turn, so both go.
    out, stats = stages.stage_drop_reasoning(msgs, cfg(preserve_recent_messages=1), tokens)
    assert stats["touched"] == 2
    assert "reasoning" not in out[4] and out[4] is not msgs[4]
    assert out[4]["content"] == "working on it"             # visible text still intact


def test_the_stage_is_registered_and_first_in_the_default_pipeline():
    assert stages.STAGES["drop_reasoning"] is stages.stage_drop_reasoning
    assert stages.DEFAULTS["pipeline"] == "drop_reasoning,prune,clip,drop_stale"
    assert stages.parse_pipeline(stages.DEFAULTS["pipeline"], extra=("prune",)) == [
        "drop_reasoning", "prune", "clip", "drop_stale"]
    dispatched, stats = stages.run_stage("drop_reasoning", transcript(), cfg(), tokens)
    assert stats["stage"] == "drop_reasoning" and "reasoning" not in dispatched[2]


def test_user_and_tool_messages_never_lose_anything_even_when_mislabelled():
    """The stage keys on role == assistant: a user or tool row that happens to carry a
    reasoning field keeps it, because deleting from them is not this stage's contract."""
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "hi", "reasoning": "user side note"},
            {"role": "tool", "tool_call_id": "c0", "content": "out",
             "reasoning_content": "tool side note"},
            old_assistant(),
            {"role": "user", "content": "next"},
            {"role": "user", "content": "and next"}]
    out, stats = stages.stage_drop_reasoning(msgs, cfg(), tokens)
    assert stats["touched"] == 1
    assert out[1] is msgs[1] and out[2] is msgs[2]
