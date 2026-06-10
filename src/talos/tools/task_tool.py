"""🤝 Task tool — how the main agent delegates to a subagent.

Each call builds a **fresh graph**: own system prompt, own (scoped) tools,
own empty message history. Only the final answer travels back — that's
the whole point: the subagent's noisy exploration never pollutes the main
conversation.

Safety: subagents run without a human in the loop, so they get a
non-interactive permission gate — read-only tools work, mutating tools
are denied (unless TALOS_YOLO). And they never receive the ``task`` tool
itself, so no infinite delegation chains.
"""

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from talos.agents import discover_agents
from talos.config import settings
from talos.context import environment_info
from talos.graph.builder import build_agent_graph
from talos.llm import build_llm
from talos.permissions import PermissionGate

# Tools a subagent may use when its definition doesn't list any.
DEFAULT_SUBAGENT_TOOLS = ["read_file", "list_dir", "glob_files", "grep", "web_fetch", "load_skill"]


def _resolve_tools(names: list[str]):
    from talos.tools import get_tools  # late import to avoid a cycle

    wanted = set(names or DEFAULT_SUBAGENT_TOOLS) - {"task"}  # 🚫 no recursion
    return [t for t in get_tools() if t.name in wanted]


@tool
async def task(agent: str, prompt: str) -> str:
    """Delegate a bounded task to a subagent from the roster in your system
    prompt. Give it a complete, self-contained brief — it cannot see this
    conversation. It returns a single final report."""
    defs = {d.name: d for d in discover_agents()}
    spec = defs.get(agent)
    if spec is None:
        roster = ", ".join(defs) or "(none defined)"
        return f"Error: no subagent '{agent}'. Available: {roster}"

    graph = build_agent_graph(
        llm=build_llm(spec.model),
        tools=_resolve_tools(spec.tools),
        system_prompt=spec.system_prompt + "\n\n" + environment_info(),
        gate=PermissionGate(approver=None, yolo=settings.yolo),
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=prompt)]},
        config={"recursion_limit": settings.max_iterations},
    )

    for msg in reversed(result["messages"]):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            content = msg.content
            return content if isinstance(content, str) else str(content)
    return "(subagent produced no final answer)"
