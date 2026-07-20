"""Tests for concurrent interjections (M20): stop flags + intent routing."""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from talos.graph.builder import STOP_NOTICE, build_agent_graph
from talos.runtime import runner

from .fakes import FakeToolCallingModel

EXECUTED = []


@tool
def dangerous(x: str) -> str:
    """Pretend work."""
    EXECUTED.append(x)
    return f"did {x}"


async def test_stop_flag_refuses_tools_at_the_safe_boundary():
    """Graceful stop: pending tool calls are refused, never half-executed,
    and the model gets the notice so it can wrap up."""
    EXECUTED.clear()
    flag = asyncio.Event()
    flag.set()  # user already asked to stop
    llm = FakeToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "dangerous", "args": {"x": "1"}, "id": "1"},
                    {"name": "dangerous", "args": {"x": "2"}, "id": "2"},
                ],
            ),
            AIMessage(content="okay, stopping — here's where things stand"),
        ]
    )
    graph = build_agent_graph(llm, tools=[dangerous], system_prompt="x", stop_flag=flag)
    result = await graph.ainvoke({"messages": [HumanMessage(content="go")]})

    assert EXECUTED == []  # nothing ran
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert all(m.content == STOP_NOTICE for m in tool_msgs)
    assert "stopping" in result["messages"][-1].content


async def test_intent_heuristics():
    assert await runner.classify_intent("can you stop this, it's not working") == "stop"
    assert await runner.classify_intent("STOP NOW!!") == "stopnow"
    assert await runner.classify_intent("abort immediately") == "stopnow"
    assert await runner.classify_intent("what are you doing right now?") == "status"
    assert await runner.classify_intent("how's it going?") == "status"


async def test_ambiguous_intent_falls_back_to_llm(monkeypatch):
    monkeypatch.setattr(
        runner, "build_llm",
        lambda model=None: FakeToolCallingModel(responses=[AIMessage(content="QUEUE")]),
    )
    assert await runner.classify_intent("also add dark mode please") == "queue"


async def test_llm_failure_defaults_to_queue(monkeypatch):
    def boom(model=None):
        raise RuntimeError("no network")

    monkeypatch.setattr(runner, "build_llm", boom)
    assert await runner.classify_intent("also add dark mode please") == "queue"


async def test_approval_future_takes_priority_over_intent(tmp_path, monkeypatch):
    """A line typed while an approval prompt waits answers the prompt."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        runner, "build_llm",
        lambda model=None: FakeToolCallingModel(responses=[AIMessage(content="hi")]),
    )
    rt = runner.Runtime(interactive=False)
    fut = asyncio.get_running_loop().create_future()
    rt._line_request = fut
    fake_task = asyncio.create_task(asyncio.sleep(0))
    await rt.interject("y", fake_task)
    assert fut.result() == "y"  # routed to the approval, not classified
