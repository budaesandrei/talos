"""🔎 Recall tool — this conversation's long-term memory (M34).

Two depths, one act (matches how humans actually recall):

- **default** = the "feeling/gist" pass: topic clusters and community
  summaries from the graph-memory that was built when older turns got
  compacted. Fast, cheap, small answer. Good for "what did we discuss /
  decide / how are these things related?"

- **detailed=True** = the "let me actually remember" pass: embedding
  search across the raw message chunks of THIS session, verbatim.
  Slower, larger answer. Good for specific IDs, URLs, quotes, config
  values, error strings — anything a summary would paraphrase away.
"""

import json

from langchain_core.tools import tool

# the active session id, set by the runtime so the tool knows which graph
_SESSION_ID: str | None = None


def set_session(session_id: str) -> None:
    global _SESSION_ID
    _SESSION_ID = session_id


@tool
def recall_memory(query: str, detailed: bool = False) -> str:
    """Recall from earlier in THIS (possibly very long) conversation.

    Two modes, mirroring how a person actually remembers:

    ``detailed=False`` (default) — the gist. Searches topic clusters
    and community summaries built during compaction. Best for "what did
    we discuss / decide / how did these things relate?" Returns a short
    abstracted answer that likely doesn't contain specific IDs, URLs,
    or exact strings.

    ``detailed=True`` — actually remember. Searches the RAW message
    chunks with embeddings and returns verbatim snippets. Use this when
    you need a specific fact the user typed earlier: a route ID, a URL,
    a file path, an error message, an exact command, a ticket number,
    a config value. Also the right retry when a default recall came
    back with paraphrased context but no specifics.

    Typical flow:
      1. `recall_memory(query)` for context (feeling/gist)
      2. `recall_memory(query, detailed=True)` if you need specifics
      3. live lookup (Kong, git log, filesystem) only if both are empty

    (For finding a DIFFERENT past conversation to resume, use
    ``search_sessions_tool`` instead — that searches across other
    sessions; this one searches the one you're in.)"""
    if _SESSION_ID is None:
        return "no active session"

    if detailed:
        return _recall_detailed(query)

    # gist mode: graph memory
    from talos.memory.graph_memory import load_graph

    graph = load_graph(_SESSION_ID)
    hit = graph.recall(query)
    if hit:
        return hit
    return (
        "nothing matched in graph-memory (summaries). "
        "If you're looking for something specific (an id, URL, quote, "
        "error string, config value, exact command), retry with "
        "detailed=True — that searches the raw message chunks."
    )


def _recall_detailed(query: str) -> str:
    """Embedding search over THIS session's raw message chunks."""
    from talos.memory import sessions_kb

    try:
        kb = sessions_kb.open_sessions_kb()
        # over-fetch so the current-session filter has enough to draw from
        hits = sessions_kb.search_sessions(query, k=30, kb=kb)
    except Exception as exc:  # noqa: BLE001
        return f"Error opening session index: {type(exc).__name__}: {exc}"

    mine = [h for h in hits if h.chunk.source_id == _SESSION_ID][:5]
    if not mine:
        return (
            "no raw chunks matched. The message may pre-date the index, or "
            "your embedder (hash mode) may not be finding a paraphrase — "
            "try again with the exact word/id the user used."
        )
    payload = [
        {
            "role": h.chunk.metadata.get("role"),
            "msg_index": h.chunk.metadata.get("msg_index"),
            "snippet": h.chunk.text,
            "score": round(h.score, 3),
        }
        for h in mine
    ]
    return json.dumps(payload, indent=2)
