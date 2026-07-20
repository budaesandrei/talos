"""Tests for the permission gate."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from talos.agent.graph.builder import build_agent_graph
from talos.infra.permissions import PermissionGate

from .fakes import FakeToolCallingModel


@tool
def boom(x: str) -> str:
    """A pretend-dangerous tool."""
    return f"executed {x}"


async def test_read_only_tools_pass_without_asking():
    gate = PermissionGate(approver=None)
    allowed, _ = await gate.check("read_file", {})
    assert allowed


async def test_non_interactive_denies_mutations():
    gate = PermissionGate(approver=None)
    allowed, reason = await gate.check("shell", {"command": "rm -rf /"})
    assert not allowed and "--yolo" in reason


async def test_yolo_allows_everything():
    gate = PermissionGate(approver=None, yolo=True)
    assert (await gate.check("shell", {}))[0]


async def test_sync_and_async_approvers_both_work():
    answers = iter(["a"])
    gate = PermissionGate(approver=lambda n, a: next(answers))
    assert (await gate.check("boom", {}))[0]   # consumes the "a"
    assert (await gate.check("boom", {}))[0]   # cached — not called again

    async def async_approver(n, a):
        return "y"

    gate2 = PermissionGate(approver=async_approver)
    assert (await gate2.check("boom", {}))[0]


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
