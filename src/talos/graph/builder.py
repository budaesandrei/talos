"""🕸️ Graph builder — the agent's "think → act → think" loop (ReAct).

The compiled graph looks like this:

    START ──▶ agent ──(tool_calls?)──▶ tools ──▶ agent ──▶ …
                │
                └──(no tool calls)──▶ END

- **agent** node: one LLM call. The model sees the system prompt + history
  and either answers in plain text or requests tool calls.
- **tools** node: executes every requested tool call and feeds the results
  back as ``ToolMessage``s, so the next agent step can read them.
- **should_continue** edge: routes to ``tools`` while the model keeps
  requesting actions, to ``END`` once it produces a final answer.
"""

from collections.abc import Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from talos.graph.state import AgentState
from talos.permissions import PermissionGate


def build_agent_graph(
    llm: BaseChatModel,
    tools: Sequence[BaseTool],
    system_prompt: str,
    gate: PermissionGate | None = None,
):
    # bind_tools() attaches the tools' JSON schemas to every request, which
    # is how the model knows what it *can* call.
    llm_with_tools = llm.bind_tools(list(tools)) if tools else llm
    tools_by_name = {t.name: t for t in tools}

    async def agent_node(state: AgentState) -> dict:
        """🧠 Think: one LLM call over the full conversation."""
        response = await llm_with_tools.ainvoke(
            [SystemMessage(content=system_prompt), *state.messages]
        )
        return {"messages": [response]}

    async def tools_node(state: AgentState) -> dict:
        """🔧 Act: execute every tool call the model just requested."""
        last = state.messages[-1]
        results: list[ToolMessage] = []

        for call in last.tool_calls:
            tool = tools_by_name.get(call["name"])
            allowed, reason = (True, "") if gate is None else gate.check(
                call["name"], call["args"]
            )
            if tool is None:
                output = f"Error: unknown tool '{call['name']}'"
            elif not allowed:
                # 🛡️ Denial is information: the model sees it and can adapt.
                output = reason
            else:
                try:
                    output = await tool.ainvoke(call["args"])
                except Exception as exc:  # errors go back to the model, not the user
                    output = f"Error: {type(exc).__name__}: {exc}"

            results.append(
                ToolMessage(
                    content=str(output),
                    tool_call_id=call["id"],
                    name=call["name"],
                )
            )
        return {"messages": results}

    def should_continue(state: AgentState) -> str:
        last = state.messages[-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")  # results always go back to the model

    return graph.compile()
