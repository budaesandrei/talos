from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langgraph.graph import END, START, StateGraph

from talos.config import settings
from talos.graph.state import AgentState


llm = ChatAnthropic(
    model=settings.model,
    anthropic_api_key=settings.anthropic_api_key,
)


def assistant_node(state: AgentState) -> dict[str, str]:
    response = llm.invoke(
        [
            SystemMessage(content=settings.system_prompt),
            *state.messages,
        ]
    )
    return {"messages": [response]}


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("assistant", assistant_node)
    graph.add_edge(START, "assistant")
    graph.add_edge("assistant", END)

    return graph.compile()
