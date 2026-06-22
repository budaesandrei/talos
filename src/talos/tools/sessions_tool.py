"""💬 Session tools — agent-facing search and discovery of past chats.

Read-only by design: the agent can find conversations and read their
metadata, but resume/delete stay user-actions (CLI). Matches the
read-only-tools / user-only-writes pattern from the vault.
"""

import json

from langchain_core.tools import tool


@tool
def list_sessions_tool(scope: str = "here") -> str:
    """List saved chat sessions, optionally filtered by project.

    scope: "here" (default — current project's sessions only),
           "all"  (every session across every project), or
           an absolute project path string.

    Returns a JSON array of {id, title, messages, project_path}.
    """
    from talos.memory.sessions import list_sessions

    try:
        rows = list_sessions(project=scope)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    return json.dumps(rows, indent=2)


@tool
def search_sessions_tool(query: str, k: int = 5,
                          scope: str = "here") -> str:
    """Semantic search across past chat sessions.

    query: natural-language description of what you're looking for
    k: max number of session hits to return (default 5)
    scope: "here" (current project) or "all" (every project)

    Returns ranked sessions as JSON: [{session_id, score, snippet,
    role, project_path}]. Lower score = better match (L2 distance).

    Use this when the user asks to find or resume a past conversation
    described in natural language. Report the candidates with their ids
    and snippets; ask the user which to resume rather than guessing.
    """
    from talos.memory import sessions, sessions_kb

    if not query.strip():
        return "Error: empty query"
    project_path = None
    if scope == "here":
        project_path = sessions.current_project_path()
    elif scope != "all":
        project_path = scope
    try:
        kb = sessions_kb.open_sessions_kb()
        hits = sessions_kb.search_sessions(
            query, k=k, kb=kb, project_path=project_path,
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    if not hits:
        return "(no matching sessions found)"
    rolled = sessions_kb.aggregate_to_sessions(hits)
    # Enrich each hit with the title from the index
    for r in rolled:
        meta = sessions.get_session_meta(r["session_id"]) or {}
        r["title"] = meta.get("title", "")
    return json.dumps(rolled, indent=2)
