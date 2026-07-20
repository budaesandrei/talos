"""🗂️ Knowledge base — a content-agnostic, vector-indexed text corpus.

The shared primitive behind M61's session search and M63+'s `/knowledge`
for arbitrary files. Each KB is one sqlite-vec database holding chunks
+ their embeddings, plus a metadata sidecar describing the KB (name,
source type, ID, created_at).

Concepts:

* **Chunk** — one unit of indexed text. Has a ``source_id`` (which
  session/file/URL it came from), a ``chunk_index`` (position within
  that source), and an arbitrary ``metadata`` dict for filtering and
  attribution. Same source can have many chunks; chunks are the
  searchable unit.

* **KnowledgeBase** — open one with ``KnowledgeBase.open(name, dir,
  embedder)``. ``add_chunks(chunks)`` indexes; ``search(query, k=5,
  filter={...})`` returns ranked ``Hit``s; ``remove_source(source_id)``
  deletes everything from one source (useful for re-indexing one
  session/file after it changed); ``clear()`` wipes all chunks.

* **Hit** — what search returns: the chunk + its similarity score. We
  use L2 distance from sqlite-vec; lower = better.

Storage layout:

    <dir>/<kb_id>/
      index.db                  # sqlite + sqlite-vec virtual table
      kb.json                   # KB metadata: name, kind, embedder name, created_at

Embedder is injected — production uses ``SentenceTransformersEmbedder``;
tests use ``HashEmbedder``. The KB stores the embedder *name* so a
KB built with one model can't accidentally be queried with another
(dim-mismatch caught at open time)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from talos.memory.embeddings import Embedder, get_embedder


# ── 🧱 data types ─────────────────────────────────────────────────────


@dataclass
class Chunk:
    """One indexable text fragment."""

    text: str
    source_id: str  # session id, file path, URL — what this chunk came from
    chunk_index: int = 0  # position within the source
    metadata: dict = field(default_factory=dict)


@dataclass
class Hit:
    """One search result."""

    chunk: Chunk
    score: float  # L2 distance from sqlite-vec; lower is closer

    def __repr__(self) -> str:
        preview = self.chunk.text[:60].replace("\n", " ")
        return f"Hit(score={self.score:.3f}, src={self.chunk.source_id!r}, '{preview}…')"


@dataclass
class KBMeta:
    """Metadata about a knowledge base — persisted next to its index."""

    kb_id: str
    name: str
    kind: str  # "sessions" | "files" | "urls" | …
    embedder_name: str
    dim: int
    created_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )


# ── 🆔 short IDs (git-style 7-char hashes) ────────────────────────────


def short_id(text: str) -> str:
    """Deterministic 7-character hex id derived from text + timestamp.
    Same shape as a git short hash so they read as familiar."""
    seed = f"{text}|{datetime.now().isoformat(timespec='microseconds')}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:7]


# ── 🛠 sqlite-vec helpers ─────────────────────────────────────────────


def _pack(vec: list[float]) -> bytes:
    """Pack a vector as little-endian float32 — sqlite-vec's wire format."""
    return struct.pack(f"{len(vec)}f", *vec)


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with sqlite-vec loaded. Fails clearly if the
    package isn't installed (rather than a cryptic 'no such function')."""
    try:
        import sqlite_vec  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "🗂 knowledge bases need sqlite-vec — install with: "
            'pip install -e ".[knowledge]"'
        ) from exc
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


# ── 🗂 the KB itself ──────────────────────────────────────────────────


class KnowledgeBase:
    """A vector-indexed text corpus, backed by sqlite-vec."""

    def __init__(self, dir_path: Path, meta: KBMeta, embedder: Embedder):
        self.dir = dir_path
        self.meta = meta
        self.embedder = embedder
        self._conn: sqlite3.Connection | None = None

    # — open / create —

    @classmethod
    def open(cls, *, name: str, dir: Path, embedder: Embedder | None = None,
             kind: str = "generic", kb_id: str | None = None) -> "KnowledgeBase":
        """Open (or create) a KB under ``dir/<kb_id>/``.

        If ``kb_id`` is None and no existing KB matches the name, a new
        short id is generated and a fresh KB is created. If a KB already
        exists at ``dir/<kb_id>``, it's loaded (and an embedder mismatch
        raises clearly).
        """
        embedder = embedder or get_embedder()
        dir.mkdir(parents=True, exist_ok=True)

        # If kb_id given, that's authoritative
        if kb_id is not None:
            kb_root = dir / kb_id
            meta_file = kb_root / "kb.json"
            if meta_file.is_file():
                meta = KBMeta(**json.loads(meta_file.read_text(encoding="utf-8")))
                kb = cls(kb_root, meta, embedder)
                kb._check_embedder_match()
                return kb
            # else fall through and create with this id
        else:
            # Look for an existing KB with this name
            for sub in dir.iterdir():
                if not sub.is_dir():
                    continue
                mf = sub / "kb.json"
                if not mf.is_file():
                    continue
                try:
                    data = json.loads(mf.read_text(encoding="utf-8"))
                    if data.get("name") == name:
                        meta = KBMeta(**data)
                        kb = cls(sub, meta, embedder)
                        kb._check_embedder_match()
                        return kb
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
            kb_id = short_id(name)

        # Create fresh
        kb_root = dir / kb_id
        kb_root.mkdir(parents=True, exist_ok=True)
        meta = KBMeta(
            kb_id=kb_id, name=name, kind=kind,
            embedder_name=embedder.name, dim=embedder.dim,
        )
        (kb_root / "kb.json").write_text(
            json.dumps(asdict(meta), indent=2), encoding="utf-8"
        )
        kb = cls(kb_root, meta, embedder)
        kb._init_schema()
        return kb

    def _check_embedder_match(self) -> None:
        if (self.meta.embedder_name != self.embedder.name
                or self.meta.dim != self.embedder.dim):
            raise RuntimeError(
                f"embedder mismatch for KB {self.meta.name!r}: "
                f"index was built with {self.meta.embedder_name} (dim {self.meta.dim}) "
                f"but current embedder is {self.embedder.name} (dim {self.embedder.dim}). "
                "Delete the KB and re-ingest, or set the matching embedder."
            )

    @property
    def db_path(self) -> Path:
        return self.dir / "index.db"

    def _conn_lazy(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _connect(self.db_path)
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        conn = self._conn if self._conn is not None else _connect(self.db_path)
        if self._conn is None:
            self._conn = conn
        conn.executescript(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks
              USING vec0(embedding float[{self.meta.dim}]);
            CREATE TABLE IF NOT EXISTS chunk_meta (
              rowid INTEGER PRIMARY KEY,
              text TEXT NOT NULL,
              source_id TEXT NOT NULL,
              chunk_index INTEGER NOT NULL DEFAULT 0,
              metadata TEXT NOT NULL DEFAULT '{{}}'
            );
            CREATE INDEX IF NOT EXISTS idx_source ON chunk_meta(source_id);
        """)
        conn.commit()

    # — write —

    def add_chunks(self, chunks: list[Chunk]) -> int:
        """Index a batch of chunks. Returns the number added.

        Embedding happens in one call (batched) for efficiency. Inserts
        into both the vec0 table and the metadata sidecar within a single
        transaction so a crash mid-insert can't leave them out of sync."""
        if not chunks:
            return 0
        conn = self._conn_lazy()
        vectors = self.embedder.embed([c.text for c in chunks])
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            for chunk, vec in zip(chunks, vectors):
                cur.execute(
                    "INSERT INTO chunk_meta(text, source_id, chunk_index, metadata) "
                    "VALUES (?, ?, ?, ?)",
                    (chunk.text, chunk.source_id, chunk.chunk_index,
                     json.dumps(chunk.metadata)),
                )
                rowid = cur.lastrowid
                cur.execute(
                    "INSERT INTO chunks(rowid, embedding) VALUES (?, ?)",
                    (rowid, _pack(vec)),
                )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return len(chunks)

    def remove_source(self, source_id: str) -> int:
        """Delete every chunk belonging to one source. Returns deleted count."""
        conn = self._conn_lazy()
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT rowid FROM chunk_meta WHERE source_id = ?", (source_id,)
        ).fetchall()
        if not rows:
            return 0
        ids = [r[0] for r in rows]
        cur.execute("BEGIN")
        try:
            for rid in ids:
                cur.execute("DELETE FROM chunks WHERE rowid = ?", (rid,))
                cur.execute("DELETE FROM chunk_meta WHERE rowid = ?", (rid,))
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return len(ids)

    def clear(self) -> int:
        """Drop every chunk. Returns count cleared."""
        conn = self._conn_lazy()
        n = conn.execute("SELECT COUNT(*) FROM chunk_meta").fetchone()[0]
        conn.executescript(
            "DELETE FROM chunks; DELETE FROM chunk_meta;"
        )
        conn.commit()
        return n

    def count(self) -> int:
        """How many chunks are indexed."""
        conn = self._conn_lazy()
        return conn.execute("SELECT COUNT(*) FROM chunk_meta").fetchone()[0]

    def sources(self) -> list[str]:
        """Distinct source_ids currently in the index."""
        conn = self._conn_lazy()
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT source_id FROM chunk_meta ORDER BY source_id"
        ).fetchall()]

    # — read —

    def search(self, query: str, k: int = 5,
               source_id: str | None = None) -> list[Hit]:
        """Top-k chunks closest to the query (L2 distance).

        ``source_id`` is an optional filter — when set, only chunks from
        that source are returned (useful for "search within this session").
        """
        if not query.strip():
            return []
        conn = self._conn_lazy()
        qvec = self.embedder.embed([query])[0]
        # sqlite-vec requires k=? on knn queries
        rows = conn.execute(
            """SELECT chunks.rowid, chunks.distance, chunk_meta.text,
                      chunk_meta.source_id, chunk_meta.chunk_index, chunk_meta.metadata
               FROM chunks
               JOIN chunk_meta ON chunk_meta.rowid = chunks.rowid
               WHERE embedding MATCH ? AND k = ?
               ORDER BY distance""",
            (_pack(qvec), k),
        ).fetchall()
        hits: list[Hit] = []
        for rowid, dist, text, src, idx, meta_json in rows:
            if source_id is not None and src != source_id:
                continue
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except json.JSONDecodeError:
                meta = {}
            hits.append(Hit(
                chunk=Chunk(text=text, source_id=src, chunk_index=idx, metadata=meta),
                score=float(dist),
            ))
        return hits

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# ── 📒 KB registry (listing all KBs under a root) ─────────────────────


def list_kbs(root: Path) -> list[KBMeta]:
    """All KBs found under ``root``. Returns metadata only; doesn't open
    the underlying databases."""
    if not root.is_dir():
        return []
    out: list[KBMeta] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        mf = sub / "kb.json"
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            out.append(KBMeta(**data))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return out


def delete_kb(root: Path, kb_id: str) -> bool:
    """Recursively remove a KB directory."""
    import shutil
    target = root / kb_id
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True
