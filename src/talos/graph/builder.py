from langgraph.graph import END, START, StateGraph

from talos.graph.state import AgentState


def assistant_node(state: AgentState) -> dict[str, str]:
    return {"output": f"Talos received: {state.user_input}"}


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("assistant", assistant_node)
    graph.add_edge(START, "assistant")
    graph.add_edge("assistant", END)

    return graph.compile()