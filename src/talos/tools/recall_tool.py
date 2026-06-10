"""🔎 Recall tool — query long-term graph memory (M34)."""

from langchain_core.tools import tool

# the active session id, set by the runtime so the tool knows which graph
_SESSION_ID: str | None = None


def set_session(session_id: str) -> None:
    global _SESSION_ID
    _SESSION_ID = session_id


@tool
def recall_memory(query: str) -> str:
    """Search long-term memory from earlier in this (possibly very long)
    conversation — topics, decisions and facts that were compacted out of
    the live context. Use when the user refers to something from far back."""
    from talos.graph_memory import load_graph

    if _SESSION_ID is None:
        return "no active session"
    graph = load_graph(_SESSION_ID)
    hit = graph.recall(query)
    return hit or "nothing relevant found in long-term memory"
