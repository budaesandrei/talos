"""Tests for context compaction (M33)."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from talos.compaction import compact, fuel_gauge, is_summary, SUMMARY_MARKER


async def _fake_summarize(prior, transcript):
    return f"digest of {transcript.count(chr(10)) + 1} lines"


async def test_compaction_folds_old_keeps_recent():
    msgs = []
    for i in range(10):
        msgs.append(HumanMessage(content=f"q{i}"))
        msgs.append(AIMessage(content=f"a{i}"))
    new, did = await compact(msgs, _fake_summarize, keep_recent=4)
    assert did
    assert is_summary(new[0])               # first message is the summary
    assert len(new) == 5                     # summary + 4 recent
    assert new[-1].content == "a9"           # most recent kept verbatim


async def test_no_compaction_when_short():
    msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
    new, did = await compact(msgs, _fake_summarize, keep_recent=6)
    assert not did and new == msgs


async def test_existing_summary_is_carried_and_merged():
    msgs = [SystemMessage(content=f"{SUMMARY_MARKER}\nold stuff")]
    for i in range(8):
        msgs.append(HumanMessage(content=f"q{i}"))
        msgs.append(AIMessage(content=f"a{i}"))
    new, did = await compact(msgs, _fake_summarize, keep_recent=4)
    assert did
    assert sum(1 for m in new if is_summary(m)) == 1  # merged, not duplicated


async def test_tool_message_not_split_from_its_call():
    msgs = [
        HumanMessage(content="go"),
        AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}]),
        ToolMessage(content="result", tool_call_id="1", name="x"),
        AIMessage(content="done"),
        HumanMessage(content="more"),
        AIMessage(content="ok"),
    ]
    # keep_recent lands mid tool-pair; the cut must skip the orphan ToolMessage
    new, did = await compact(msgs, _fake_summarize, keep_recent=4)
    kept = [m for m in new if not is_summary(m)]
    assert not (isinstance(kept[0], ToolMessage))  # never starts with an orphan


def test_fuel_gauge():
    assert fuel_gauge(0, None) == ""
    g = fuel_gauge(70, 100, width=10)
    assert "70%" in g and g.count("▰") == 7
