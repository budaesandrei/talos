from talos.graph.builder import build_graph


def run_agent(user_input: str) -> str:
    graph = build_graph()

    result = graph.invoke(
        {
            "user_input": user_input,
            "output": "",
        }
    )

    return result["output"]
