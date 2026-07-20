"""Tests for think-mode stream splitting (M45)."""

from talos.agent.thinking import ThinkSplitter


def _collect(fragments):
    s = ThinkSplitter()
    events = []
    for f in fragments:
        events += list(s.feed(f))
    events += list(s.flush())
    return events


def _joined(events, channel):
    return "".join(p for c, p in events if c == channel)


def test_split_across_chunks():
    ev = _collect(["<think", "ing>reason here</think", "ing>the answer"])
    assert _joined(ev, "think") == "reason here"
    assert _joined(ev, "answer") == "the answer"


def test_no_thinking_is_all_answer():
    ev = _collect(["just ", "a normal ", "answer"])
    assert _joined(ev, "answer") == "just a normal answer"
    assert _joined(ev, "think") == ""


def test_thinking_then_answer_inline():
    ev = _collect(["<thinking>plan</thinking>done"])
    assert _joined(ev, "think") == "plan"
    assert _joined(ev, "answer") == "done"


def test_strip_removes_block():
    assert ThinkSplitter.strip("<thinking>secret\nmulti</thinking>\nVisible") == "Visible"
    assert ThinkSplitter.strip("no tags here") == "no tags here"


def test_carry_never_loses_text():
    # a chunk ending mid-word that isn't a tag must still be emitted
    ev = _collect(["hello wor", "ld"])
    assert _joined(ev, "answer") == "hello world"
