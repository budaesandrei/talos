"""🔁 In-session resume: the switch swaps state between turns, losslessly."""

from langchain_core.messages import AIMessage, HumanMessage

import talos.agent.runtime as rtmod
from talos.agent.runtime import Runtime


class FakeRT:
    """Just enough Runtime to exercise the bound methods."""

    switch_session = Runtime.switch_session
    _close_dangling_tool_calls = Runtime._close_dangling_tool_calls
    _make_resume_tool = Runtime._make_resume_tool

    def __init__(self):
        self.session_id = "current"
        self.messages = [HumanMessage(content="old")]
        self.title = ""
        self.usage = {"input": 9, "output": 9, "total": 18, "turns": 3}
        self.context_tokens = 1234
        self._pending_resume = None


def test_switch_session_swaps_state(monkeypatch):
    saved = [HumanMessage(content="about graphs"), AIMessage(content="yes!")]
    monkeypatch.setattr(rtmod, "_resolve_resume", lambda q: "20990101-0000")
    monkeypatch.setattr(rtmod, "load_session", lambda sid: list(saved))
    monkeypatch.setattr(
        rtmod, "get_session_meta",
        lambda sid: {"title": "graph talk",
                     "usage": {"input": 1, "output": 2, "total": 3, "turns": 1}},
    )
    rt = FakeRT()
    sid = rt.switch_session("graph implementation chat")
    assert sid == "20990101-0000"
    assert rt.session_id == sid
    assert [m.content for m in rt.messages] == ["about graphs", "yes!"]
    assert rt.title == "graph talk" and rt.usage["turns"] == 1
    assert rt.context_tokens == 0  # re-measured next reply


def test_resume_tool_parks_the_switch_for_after_the_turn():
    rt = FakeRT()
    tool = rt._make_resume_tool()
    assert tool.name == "resume_session"
    out = tool.invoke({"session": "20990101-0000"})
    # nothing swapped mid-turn — only parked for the repl to act on
    assert rt._pending_resume == "20990101-0000"
    assert rt.session_id == "current"
    assert "queued" in out.lower()


def test_slash_resume_dispatch():
    from talos.ui.commands import dispatch

    assert dispatch("/resume 20990101-0000") == ("resume", "20990101-0000")
    assert dispatch("/resume the graph chat") == ("resume", "the graph chat")
    assert dispatch("/resume") == ("resume", "")
