"""🗂 Knowledge tools — agent surface for user-set KBs.

Reads (search + list) are available to the agent. Writes (add /
remove / update / clear) ARE also available — per the M66 design
direction "all non-safety CLI verbs are tools". The KB content
itself isn't sensitive (kiro's model), so write opacity isn't needed
the way it is for vault.
"""

import json

from langchain_core.tools import tool


@tool
def recall_knowledge(query: str, k: int = 5,
                      kb: str | None = None) -> str:
    """Semantic search across user-set knowledge bases.

    query: natural-language query
    k: max hits to return (default 5)
    kb: optional KB id or name to scope to a single base; None = all

    Returns ranked hits as JSON. Lower score = better match. Use this
    when the user asks anything that might be answered by docs/files
    they previously added with `talos knowledge add` or `/knowledge add`.
    """
    from talos.lifecycle.knowledge_cli import search_user_kbs

    if not query.strip():
        return "Error: empty query"
    try:
        hits = search_user_kbs(query, k=k, kb_id_or_name=kb)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    if not hits:
        return "(no matching content in any knowledge base)"
    return json.dumps(hits, indent=2)


@tool
def list_kbs_tool() -> str:
    """List user-set knowledge bases. Returns JSON [{kb_id, name, kind,
    created_at}]. Use to remind yourself what KBs exist before searching
    or adding."""
    from talos.lifecycle.knowledge_cli import list_user_kbs

    try:
        metas = list_user_kbs()
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    if not metas:
        return "(no knowledge bases yet)"
    return json.dumps([
        {"kb_id": m.kb_id, "name": m.name, "kind": m.kind,
         "created_at": m.created_at}
        for m in metas
    ], indent=2)


@tool
def add_kb_tool(name: str, path: str,
                 include: str = "", exclude: str = "") -> str:
    """Add a local file or directory to a knowledge base (creates the
    KB if it doesn't exist).

    name: KB name (descriptive, e.g. 'api-docs')
    path: absolute or relative filesystem path
    include: comma-separated glob patterns to include (e.g. '**/*.md')
    exclude: comma-separated glob patterns to exclude (e.g. 'node_modules/**')

    Returns a summary string. URLs are not supported here — use the
    CLI `talos knowledge add` for URL sources (M64).
    """
    from talos.lifecycle.knowledge_cli import add_kb

    inc = [p.strip() for p in include.split(",") if p.strip()]
    exc = [p.strip() for p in exclude.split(",") if p.strip()]
    try:
        kb, n = add_kb(name=name, path=path, include=inc, exclude=exc)
    except Exception as exc_:  # noqa: BLE001
        return f"Error: {type(exc_).__name__}: {exc_}"
    return f"added {n} chunk(s) to KB {kb.meta.name!r} ({kb.meta.kb_id})"


@tool
def update_kb_tool(kb_id_or_name: str) -> str:
    """Re-ingest every source listed in a KB's manifest. Use when the
    underlying files have changed."""
    from talos.lifecycle.knowledge_cli import update_kb

    try:
        res = update_kb(kb_id_or_name=kb_id_or_name)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    return f"re-indexed {res.get('sources', 0)} source(s), {res.get('chunks', 0)} chunk(s)"


@tool
def remove_kb_tool(kb_id_or_name: str) -> str:
    """Delete a knowledge base by id or name."""
    from talos.lifecycle.knowledge_cli import remove_kb

    try:
        ok = remove_kb(kb_id_or_name=kb_id_or_name)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    return f"removed {kb_id_or_name!r}" if ok else f"no KB named {kb_id_or_name!r}"
