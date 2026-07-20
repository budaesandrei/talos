"""Tests for the core agent loop (think → act → think)."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from talos.agent.graph.builder import build_agent_graph

from .fakes import FakeToolCallingModel


@tool
def echo(text: str) -> str:
    """Echo the input back."""
    return f"echo: {text}"


async def test_plain_answer_ends_the_loop():
    llm = FakeToolCallingModel(responses=[AIMessage(content="hi there")])
    graph = build_agent_graph(llm, tools=[], system_prompt="be nice")

    result = await graph.ainvoke({"messages": [HumanMessage(content="hello")]})

    assert result["messages"][-1].content == "hi there"


async def test_tool_call_round_trip():
    llm = FakeToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "echo", "args": {"text": "ping"}, "id": "1"}],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = build_agent_graph(llm, tools=[echo], system_prompt="be nice")

    result = await graph.ainvoke({"messages": [HumanMessage(content="run echo")]})
    messages = result["messages"]

    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == "echo: ping"
    assert messages[-1].content == "done"


async def test_unknown_tool_reports_error_to_model():
    llm = FakeToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "nope", "args": {}, "id": "1"}],
            ),
            AIMessage(content="ok"),
        ]
    )
    graph = build_agent_graph(llm, tools=[echo], system_prompt="x")

    result = await graph.ainvoke({"messages": [HumanMessage(content="go")]})
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]

    assert "unknown tool" in tool_msgs[0].content
