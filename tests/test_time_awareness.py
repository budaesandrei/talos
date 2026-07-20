"""Tests for time-awareness (M58).

The agent's clock is injected — every test passes an explicit ``now``
rather than relying on ``datetime.now()`` — so assertions are
deterministic. Per-message stamping is exercised through ``stamp()``
directly and through a Runtime round-trip via the compaction module
where the time-span propagates into the summarize callback.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from talos.agent import time_awareness as ta


# ── ✏️ stamp / timestamp_of / last_active ─────────────────────────────


def test_stamp_writes_iso_seconds_into_additional_kwargs():
    m = HumanMessage(content="hello")
    when = datetime(2026, 6, 18, 9, 30, 0)
    ta.stamp(m, when=when)
    assert m.additional_kwargs["created_at"] == "2026-06-18T09:30:00"


def test_stamp_is_idempotent():
    """A message that's already stamped must keep its original time, so
    the real creation time survives save/load/compact round-trips."""
    m = HumanMessage(content="hello")
    ta.stamp(m, when=datetime(2026, 6, 18, 9, 0))
    ta.stamp(m, when=datetime(2026, 6, 20, 14, 0))  # would-be overwrite
    assert m.additional_kwargs["created_at"] == "2026-06-18T09:00:00"


def test_stamp_initializes_missing_additional_kwargs():
    """LangChain message subtypes sometimes default additional_kwargs to
    None; ``stamp`` must handle that without crashing."""
    m = HumanMessage(content="hi")
    m.additional_kwargs = None  # type: ignore[assignment]
    ta.stamp(m, when=datetime(2026, 6, 18, 9, 0))
    assert m.additional_kwargs["created_at"] == "2026-06-18T09:00:00"


def test_timestamp_of_returns_none_for_missing_or_bad_data():
    assert ta.timestamp_of(HumanMessage(content="no stamp")) is None
    m = HumanMessage(content="bad stamp")
    m.additional_kwargs = {"created_at": "garbage"}
    assert ta.timestamp_of(m) is None


def test_last_active_picks_latest_across_messages():
    msgs = [
        ta.stamp(HumanMessage(content="a"), when=datetime(2026, 6, 18, 9)),
        ta.stamp(AIMessage(content="b"), when=datetime(2026, 6, 18, 10)),
        ta.stamp(HumanMessage(content="c"), when=datetime(2026, 6, 18, 9, 30)),
    ]
    assert ta.last_active(msgs) == datetime(2026, 6, 18, 10)


def test_last_active_returns_none_when_nothing_stamped():
    assert ta.last_active([HumanMessage(content="x"),
                            AIMessage(content="y")]) is None


def test_stamp_all_counts_only_new_stamps():
    msgs = [
        HumanMessage(content="a"),
        ta.stamp(HumanMessage(content="b"), when=datetime(2026, 6, 18, 9)),
        HumanMessage(content="c"),
    ]
    n = ta.stamp_all(msgs, when=datetime(2026, 6, 20, 10))
    assert n == 2
    # The pre-stamped one kept its original time
    assert msgs[1].additional_kwargs["created_at"] == "2026-06-18T09:00:00"


# ── ⏰ detect_gap ──────────────────────────────────────────────────────


def test_detect_gap_returns_none_for_fresh_messages():
    msgs = [ta.stamp(HumanMessage(content="x"),
                      when=datetime(2026, 6, 18, 10, 0))]
    gap = ta.detect_gap(msgs, now=datetime(2026, 6, 18, 10, 5),
                         threshold_minutes=30)
    assert gap is None  # 5 < 30


def test_detect_gap_returns_timedelta_when_threshold_exceeded():
    msgs = [ta.stamp(HumanMessage(content="x"),
                      when=datetime(2026, 6, 18, 10, 0))]
    gap = ta.detect_gap(msgs, now=datetime(2026, 6, 19, 0, 23),
                         threshold_minutes=30)
    assert gap == timedelta(hours=14, minutes=23)


def test_detect_gap_uses_latest_stamped_message():
    """Even with older messages in front, the gap is measured from the
    NEWEST stamped message."""
    msgs = [
        ta.stamp(HumanMessage(content="old"),
                  when=datetime(2026, 6, 10, 9, 0)),
        ta.stamp(AIMessage(content="newer"),
                  when=datetime(2026, 6, 18, 10, 0)),
    ]
    gap = ta.detect_gap(msgs, now=datetime(2026, 6, 18, 11, 0),
                         threshold_minutes=30)
    assert gap == timedelta(hours=1)


def test_detect_gap_zero_threshold_disables():
    msgs = [ta.stamp(HumanMessage(content="x"),
                      when=datetime(2026, 6, 18, 10, 0))]
    assert ta.detect_gap(msgs, now=datetime(2026, 6, 19, 10),
                          threshold_minutes=0) is None


def test_detect_gap_returns_none_for_empty_or_unstamped():
    assert ta.detect_gap([], now=datetime.now(), threshold_minutes=30) is None
    msgs = [HumanMessage(content="no stamp")]
    assert ta.detect_gap(msgs, now=datetime.now(),
                          threshold_minutes=30) is None


# ── ⏱ format_gap / format_last_active / time_span ─────────────────────


def test_format_gap_renders_human_friendly():
    assert ta.format_gap(timedelta(minutes=14)) == "14m"
    assert ta.format_gap(timedelta(hours=2, minutes=15)) == "2h 15m"
    assert ta.format_gap(timedelta(days=1, hours=4, minutes=30)) == "1d 4h 30m"
    # Tiny gaps round to 0m, not empty
    assert ta.format_gap(timedelta(seconds=4)) == "0m"
    # Days with no other components
    assert ta.format_gap(timedelta(days=3)) == "3d"


def test_format_last_active_returns_just_now_for_tiny_gaps():
    msgs = [ta.stamp(HumanMessage(content="x"),
                      when=datetime(2026, 6, 18, 10, 0, 0))]
    out = ta.format_last_active(msgs, now=datetime(2026, 6, 18, 10, 0, 15))
    assert out == "last active just now"


def test_format_last_active_for_real_gap():
    msgs = [ta.stamp(HumanMessage(content="x"),
                      when=datetime(2026, 6, 18, 10, 0))]
    out = ta.format_last_active(msgs, now=datetime(2026, 6, 19, 0, 23))
    assert out == "last active 14h 23m ago"


def test_format_last_active_returns_none_with_no_stamps():
    assert ta.format_last_active([HumanMessage(content="x")]) is None


def test_time_span_returns_earliest_and_latest():
    msgs = [
        ta.stamp(HumanMessage(content="b"), when=datetime(2026, 6, 18, 10)),
        ta.stamp(AIMessage(content="a"), when=datetime(2026, 6, 17, 9)),
        ta.stamp(ToolMessage(content="c", tool_call_id="t"),
                  when=datetime(2026, 6, 19, 11)),
    ]
    span = ta.time_span(msgs)
    assert span is not None
    start, end = span
    assert start == datetime(2026, 6, 17, 9)
    assert end == datetime(2026, 6, 19, 11)


def test_format_span_long_includes_duration():
    out = ta.format_span(
        (datetime(2026, 6, 17, 9), datetime(2026, 6, 19, 11))
    )
    assert "2026-06-17" in out and "2026-06-19" in out
    assert "spanning" in out and "2d" in out


def test_format_span_short_is_concise():
    out = ta.format_span(
        (datetime(2026, 6, 17, 9, 0), datetime(2026, 6, 17, 9, 45))
    )
    # Short conversation: dates → times, no "spanning"
    assert "2026-06-17" in out and "spanning" not in out


def test_format_span_returns_empty_string_for_none():
    assert ta.format_span(None) == ""


# ── 📣 gap_notice (SystemMessage) ─────────────────────────────────────


def test_gap_notice_produces_taggable_system_message():
    notice = ta.gap_notice(timedelta(hours=14, minutes=23),
                            when=datetime(2026, 6, 19, 0, 23))
    assert isinstance(notice, SystemMessage)
    assert "14h 23m" in notice.content
    assert ta.is_gap_notice(notice)
    # And it's stamped itself
    assert notice.additional_kwargs["created_at"] == "2026-06-19T00:23:00"


def test_is_gap_notice_rejects_other_system_messages():
    assert not ta.is_gap_notice(SystemMessage(content="random"))
    assert not ta.is_gap_notice(HumanMessage(content="user"))


# ── 🔁 round-trip through LangChain serialization ─────────────────────


def test_stamp_survives_messages_to_dict_round_trip():
    """A stamped message must come back stamped after LangChain's
    serialization (which is what memory/sessions.py uses on save/load)."""
    from langchain_core.messages import messages_from_dict, messages_to_dict

    m = ta.stamp(HumanMessage(content="hi"),
                  when=datetime(2026, 6, 18, 9, 30))
    out = messages_from_dict(messages_to_dict([m]))
    assert ta.timestamp_of(out[0]) == datetime(2026, 6, 18, 9, 30)


# ── 🗜️ compaction integration ────────────────────────────────────────


@pytest.mark.asyncio
async def test_compaction_passes_span_to_summarizer():
    """When messages are stamped, the compaction prompt should include
    the time span so the digest can mention 'these turns spanned ...'."""
    from talos.memory.compaction import compact

    msgs = []
    base = datetime(2026, 6, 18, 9, 0)
    for i in range(10):
        m = HumanMessage(content=f"msg {i}") if i % 2 == 0 \
            else AIMessage(content=f"reply {i}")
        ta.stamp(m, when=base + timedelta(hours=i * 6))
        msgs.append(m)

    captured = {}
    async def fake_summarize(prior: str, transcript: str, span: str = "") -> str:
        captured["prior"] = prior
        captured["transcript"] = transcript
        captured["span"] = span
        return "digest"

    new, did = await compact(msgs, fake_summarize, keep_recent=4)
    assert did is True
    assert captured["span"]  # non-empty
    assert "2026-06-18" in captured["span"]


@pytest.mark.asyncio
async def test_compaction_works_with_two_arg_summarizer():
    """Backward compat: a pre-M58 summarize callable that takes only
    (prior, transcript) must still work. compact() should fall back
    via TypeError catch."""
    from talos.memory.compaction import compact

    msgs = [
        ta.stamp(HumanMessage(content="a"), when=datetime(2026, 6, 18, 9)),
        AIMessage(content="b"),
        HumanMessage(content="c"),
        AIMessage(content="d"),
        HumanMessage(content="e"),
        AIMessage(content="f"),
        HumanMessage(content="g"),
        AIMessage(content="h"),
    ]
    async def two_arg(prior: str, transcript: str) -> str:
        return "old-style digest"

    new, did = await compact(msgs, two_arg, keep_recent=4)
    assert did is True
    # The summary made it in
    assert any("old-style digest" in str(m.content) for m in new)
