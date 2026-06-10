"""Tests for the permission gate."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from talos.graph.builder import build_agent_graph
from talos.permissions import PermissionGate

from .fakes import FakeToolCallingModel


@tool
def boom(x: str) -> str:
    """A pretend-dangerous tool."""
    return f"executed {x}"


def test_read_only_tools_pass_without_asking():
    gate = PermissionGate(approver=None)
    allowed, _ = gate.check("read_file", {})
    assert allowed


def test_non_interactive_denies_mutations():
    gate = PermissionGate(approver=None)
    allowed, reason = gate.check("shell", {"command": "rm -rf /"})
    assert not allowed and "--yolo" in reason


def test_yolo_allows_everything():
    gate = PermissionGate(approver=None, yolo=True)
    assert gate.check("shell", {})[0]


def test_always_answer_caches_for_session():
    answers = iter(["a"])
    gate = PermissionGate(approver=lambda n, a: next(answers))
    assert gate.check("boom", {})[0]      # consumes the "a"
    assert gate.check("boom", {})[0]      # cached — approver not called again


async def test_denied_tool_becomes_tool_message():
    llm = FakeToolCallingModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "boom", "args": {"x": "1"}, "id": "1"}]),
            AIMessage(content="understood"),
        ]
    )
    gate = PermissionGate(approver=lambda n, a: "n")
    graph = build_agent_graph(llm, tools=[boom], system_prompt="x", gate=gate)

    result = await graph.ainvoke({"messages": [HumanMessage(content="go")]})
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]

    assert "denied" in tool_msgs[0].content.lower()
    assert result["messages"][-1].content == "understood"
