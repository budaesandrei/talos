from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from talos.graph.builder import build_graph


def get_message_text(message: BaseMessage) -> str:
    content = message.content

    if isinstance(content, str):
        return content

    return str(content)


def run_agent(user_input: str) -> str:
    graph = build_graph()

    result = graph.invoke(
        {
            "messages": [HumanMessage(content=user_input)],
        }
    )

    last_message = result["messages"][-1]

    if not isinstance(last_message, AIMessage):
        return get_message_text(last_message)

    return get_message_text(last_message)


def run_chat_turn(messages: list[BaseMessage], user_input: str) -> list[BaseMessage]:
    graph = build_graph()

    result = graph.invoke(
        {
            "messages": [
                *messages,
                HumanMessage(content=user_input),
            ],
        }
    )

    return result["messages"]
