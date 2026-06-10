from langchain_core.messages import SystemMessage
from langgraph.graph import END, START, StateGraph

from talos.config import PACKAGE_ROOT
from talos.graph.state import AgentState
from talos.llm import build_llm

SYSTEM_PROMPT = (PACKAGE_ROOT / "prompts" / "system.md").read_text(encoding="utf-8")


def assistant_node(state: AgentState) -> dict:
    llm = build_llm()
    response = llm.invoke([SystemMessage(content=SYSTEM_PROMPT), *state.messages])
    return {"messages": [response]}


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("assistant", assistant_node)
    graph.add_edge(START, "assistant")
    graph.add_edge("assistant", END)

    return graph.compile()
