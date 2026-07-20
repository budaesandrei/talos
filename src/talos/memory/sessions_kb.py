"""💬→🗂 SessionsKB — auto-index sessions for natural-language search.

The first concrete user of the generic KnowledgeBase primitive (M60).
Each session becomes one source; each message becomes one chunk (or
several sub-chunks for very long messages). Source IDs are session IDs,
chunk metadata carries role + index + timestamp + project_path so the
search results can attribute back to the original conversation.

Ingestion is idempotent: ``ingest_session(id)`` first removes any prior
chunks for that source, then re-indexes. Lets us re-run cheaply when a
session grows (M61 wires this up as a save_session hook). ``ingest_all()``
walks the sessions dir and indexes everything.

Chunking strategy is deliberately simple: one message = one chunk. If a
message text exceeds CHUNK_CHAR_LIMIT (default 1500 chars ≈ 400 tokens),
we split it into overlapping windows. Keeping turn boundaries lets
search results attribute back to specific messages cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from talos.memory.embeddings import Embedder, get_embedder
from talos.memory.knowledge import Chunk, Hit, KnowledgeBase


SESSIONS_KB_NAME = "sessions"
SESSIONS_KB_KIND = "sessions"

CHUNK_CHAR_LIMIT = 1500
CHUNK_OVERLAP = 200


def kb_root(*, global_dir: Path | None = None) -> Path:
    """Where session KBs live: ``~/.talos/kb/`` by default."""
    if global_dir is None:
        from talos.infra.vault import global_dir as _gd
        global_dir = _gd()
    return global_dir / "kb"


def open_sessions_kb(*, embedder: Embedder | None = None,
                      global_dir: Path | None = None) -> KnowledgeBase:
    """Open (or create) the singleton sessions knowledge base.

    Embedder defaults to whatever ``get_embedder()`` selects
    (sentence-transformers if installed, else HashEmbedder)."""
    return KnowledgeBase.open(
        name=SESSIONS_KB_NAME,
        kind=SESSIONS_KB_KIND,
        dir=kb_root(global_dir=global_dir),
        embedder=embedder or get_embedder(),
    )


# ── ✂️ chunking ───────────────────────────────────────────────────────


def split_text(text: str, *, limit: int = CHUNK_CHAR_LIMIT,
                overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Sliding-window split for messages over ``limit`` chars. Short
    messages pass through as one chunk. Overlap helps preserve context
    across boundaries (a phrase near a chunk edge appears in both)."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        out.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return out


# ── 🔄 ingestion ──────────────────────────────────────────────────────


def _message_to_chunks(msg_dict: dict, session_id: str, msg_index: int,
                        project_path: str | None) -> Iterable[Chunk]:
    """Turn one serialized LangChain message into one or more Chunks."""
    kw = msg_dict.get("kwargs", {}) or msg_dict.get("data", {}) or {}
    content = kw.get("content", "")
    if isinstance(content, list):
        # multimodal content — flatten text blocks only
        content = "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict)
        )
    text = str(content).strip()
    if not text:
        return
    role_type = msg_dict.get("type", "unknown")
    role_map = {
        "human": "user",
        "ai": "assistant",
        "tool": "tool",
        "system": "system",
    }
    role = role_map.get(role_type, role_type)
    additional = kw.get("additional_kwargs", {}) or {}
    created_at = additional.get("created_at")

    sub_chunks = split_text(text)
    for sub_i, sub_text in enumerate(sub_chunks):
        chunk_idx = msg_index * 1000 + sub_i  # leave room for sub-chunks
        meta = {
            "role": role,
            "msg_index": msg_index,
            "sub_index": sub_i,
            "created_at": created_at,
        }
        if project_path:
            meta["project_path"] = project_path
        yield Chunk(
            text=sub_text,
            source_id=session_id,
            chunk_index=chunk_idx,
            metadata=meta,
        )


def ingest_session(session_id: str, *, kb: KnowledgeBase | None = None,
                    sessions_dir: Path | None = None) -> int:
    """Re-index one session. Removes any prior chunks for this session
    first (idempotent), then re-adds. Returns the chunk count added."""
    from talos.memory.sessions import sessions_dir as _sd
    sessions_dir = sessions_dir or _sd()
    f = sessions_dir / f"{session_id}.json"
    if not f.is_file():
        return 0
    try:
        raw_messages = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0

    # Look up the project_path from the index (set during migration / future
    # global writes). Falls back to None.
    from talos.memory.sessions import get_session_meta
    meta = get_session_meta(session_id) or {}
    project_path = meta.get("project_path")

    kb = kb or open_sessions_kb()
    kb.remove_source(session_id)

    chunks: list[Chunk] = []
    for idx, msg in enumerate(raw_messages):
        chunks.extend(_message_to_chunks(msg, session_id, idx, project_path))
    if not chunks:
        return 0
    return kb.add_chunks(chunks)


def ingest_all(*, kb: KnowledgeBase | None = None,
               sessions_dir: Path | None = None) -> dict:
    """Index every session in ``sessions_dir``. Returns
    ``{"sessions": N, "chunks": M}``."""
    from talos.memory.sessions import sessions_dir as _sd, _session_files
    sessions_dir = sessions_dir or _sd()
    if not sessions_dir.is_dir():
        return {"sessions": 0, "chunks": 0}
    kb = kb or open_sessions_kb()
    files = _session_files(sessions_dir)
    sess_n = 0
    chunk_n = 0
    for f in files:
        added = ingest_session(f.stem, kb=kb, sessions_dir=sessions_dir)
        if added:
            sess_n += 1
            chunk_n += added
    return {"sessions": sess_n, "chunks": chunk_n}


# ── 🔍 search wrapper ────────────────────────────────────────────────


def search_sessions(query: str, *, k: int = 5,
                     kb: KnowledgeBase | None = None,
                     project_path: str | None = None) -> list[Hit]:
    """Semantic search across the sessions KB.

    ``project_path`` filters results to chunks from sessions originally
    created under that project (None = all projects). Useful for "find
    the conversation about X in this repo's history.\""""
    kb = kb or open_sessions_kb()
    hits = kb.search(query, k=max(k * 3 if project_path else k, k))
    if project_path:
        hits = [h for h in hits if h.chunk.metadata.get("project_path") == project_path]
    return hits[:k]


def aggregate_to_sessions(hits: list[Hit]) -> list[dict]:
    """Roll chunk-level hits up to one entry per session (best chunk wins).
    Returns ``[{session_id, score, snippet, role, project_path}, ...]``
    sorted by score ascending (lowest distance = best match)."""
    by_session: dict[str, Hit] = {}
    for h in hits:
        existing = by_session.get(h.chunk.source_id)
        if existing is None or h.score < existing.score:
            by_session[h.chunk.source_id] = h
    out = []
    for sid, h in by_session.items():
        snippet = h.chunk.text[:200].replace("\n", " ")
        out.append({
            "session_id": sid,
            "score": h.score,
            "snippet": snippet,
            "role": h.chunk.metadata.get("role", "?"),
            "project_path": h.chunk.metadata.get("project_path"),
        })
    out.sort(key=lambda d: d["score"])
    return out
