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

import asyncio
from collections.abc import Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from talos.agent.graph.state import AgentState
from talos.infra.permissions import PermissionGate
from talos.infra.policy import check_action
from talos.infra.tracing import set_span_attrs, span


STOP_NOTICE = (
    "⚠️ USER REQUESTED STOP — do not start new actions. Wrap up now: "
    "summarize what was done and what remains."
)


def build_agent_graph(
    llm: BaseChatModel,
    tools: Sequence[BaseTool],
    system_prompt: str,
    gate: PermissionGate | None = None,
    stop_flag: "asyncio.Event | None" = None,
):
    # bind_tools() attaches the tools' JSON schemas to every request, which
    # is how the model knows what it *can* call.
    llm_with_tools = llm.bind_tools(list(tools)) if tools else llm
    tools_by_name = {t.name: t for t in tools}

    async def agent_node(state: AgentState) -> dict:
        """🧠 Think: one LLM call over the full conversation."""
        messages = [SystemMessage(content=system_prompt), *state.messages]
        # 💾 Anthropic-style models cache nothing unless we mark
        # breakpoints — and the ReAct loop re-bills the whole prefix on
        # every think→act step, so this is the biggest cost lever we
        # have. No-op for OpenAI-family (automatic server-side caching).
        from talos.agent.caching import add_cache_breakpoints, cache_enabled

        if cache_enabled(getattr(llm, "model_name", "") or ""):
            messages = add_cache_breakpoints(messages)
        with span("gen_ai.chat", **{"gen_ai.operation.name": "chat"}) as s:
            response = await llm_with_tools.ainvoke(messages)
            um = getattr(response, "usage_metadata", None) or {}
            set_span_attrs(
                s,
                **{
                    "gen_ai.usage.input_tokens": um.get("input_tokens"),
                    "gen_ai.usage.output_tokens": um.get("output_tokens"),
                    "gen_ai.response.tool_calls": len(response.tool_calls or []),
                },
            )
        return {"messages": [response]}

    async def tools_node(state: AgentState) -> dict:
        """🔧 Act: execute every tool call the model just requested."""
        last = state.messages[-1]
        results: list[ToolMessage] = []

        for call in last.tool_calls:
            tool = tools_by_name.get(call["name"])
            # 🛑 graceful stop: between tool calls is the safe boundary —
            # never mid-write. Remaining calls are refused; the model reads
            # the notice and wraps up instead of acting.
            if stop_flag is not None and stop_flag.is_set():
                results.append(
                    ToolMessage(
                        content=STOP_NOTICE,
                        tool_call_id=call["id"],
                        name=call["name"],
                    )
                )
                continue
            # 🚧 deterministic policy runs BEFORE the gate — a denied action
            # never even becomes a prompt the human could approve
            denial = check_action(call["name"], call["args"])
            if denial is not None:
                results.append(ToolMessage(
                    content=denial, tool_call_id=call["id"], name=call["name"]))
                continue
            allowed, reason = (True, "") if gate is None else await gate.check(
                call["name"], call["args"]
            )
            if tool is None:
                output = f"Error: unknown tool '{call['name']}'"
            elif not allowed:
                # 🛡️ Denial is information: the model sees it and can adapt.
                output = reason
            else:
                with span("execute_tool",
                          **{"gen_ai.operation.name": "execute_tool",
                             "gen_ai.tool.name": call["name"]}):
                    try:
                        output = await tool.ainvoke(call["args"])
                    except Exception as exc:  # errors go to the model, not the user
                        output = f"Error: {type(exc).__name__}: {exc}"

            # 🔐 vault scrub: redact any revealed secret values that
            # might have leaked into the tool output (e.g., `cat .env`).
            # Honest-leak defense only; the docs are explicit that an
            # adversarial model can bypass via encoding tricks.
            from talos.infra.vault import RevealedSecrets

            scrubbed = RevealedSecrets.scrub(str(output))
            results.append(
                ToolMessage(
                    content=scrubbed,
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
