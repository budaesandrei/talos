"""🗂 User-set knowledge bases — the kiro-parity `/knowledge` feature.

Sits on top of the M60 KB primitive but enforces "user-set only" in
its listing: `kb_root_user()` is a separate subdir from where
SessionsKB lives, so `talos knowledge show` doesn't pollute its view
with the sessions index.

Each user-set KB is created by `add_kb(name, paths, …)`:

* one or more local file or directory paths (URLs land in M64)
* optional include/exclude glob patterns
* a sliding-window chunker for arbitrary text (different from
  SessionsKB's one-chunk-per-message strategy)

Re-ingest is idempotent at the source level: re-adding the same file
removes the prior chunks first. ``update_kb(kb_id, paths=...)``
re-ingests everything (or the given paths) using the KB's stored
include/exclude patterns.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from talos.memory.embeddings import Embedder, get_embedder
from talos.memory.knowledge import (
    Chunk,
    KBMeta,
    KnowledgeBase,
    delete_kb,
    list_kbs,
    short_id,
)


CHUNK_CHAR_LIMIT = 1500
CHUNK_OVERLAP = 200

# Same file types kiro indexes — text, code, markdown, configs, data
SUPPORTED_EXTENSIONS = frozenset({
    ".txt", ".log", ".rtf", ".tex", ".rst",
    ".md", ".markdown", ".mdx",
    ".json", ".yaml", ".yml", ".toml",
    ".ini", ".conf", ".cfg", ".properties", ".env",
    ".csv", ".tsv",
    ".svg",
    # code
    ".py", ".rs", ".go", ".js", ".jsx", ".ts", ".tsx",
    ".java", ".c", ".cpp", ".h", ".hpp", ".rb", ".php",
    ".swift", ".kt", ".kts", ".cs", ".sh", ".bash", ".zsh",
    ".html", ".htm", ".xml", ".css", ".scss", ".sass", ".less",
    ".sql",
})

# Special filenames without extensions (kiro convention)
SUPPORTED_BASENAMES = frozenset({
    "Dockerfile", "Makefile", "LICENSE", "CHANGELOG", "README",
})


# ── 🗂 storage layout ─────────────────────────────────────────────────


def kb_root_user(*, global_dir: Path | None = None) -> Path:
    """``~/.talos/kb/user/`` — separate from ``~/.talos/kb/sessions/``
    so the user-facing `/knowledge show` doesn't accidentally include
    the SessionsKB."""
    if global_dir is None:
        from talos.infra.vault import global_dir as _gd
        global_dir = _gd()
    return global_dir / "kb" / "user"


@dataclass
class KBSource:
    """One file/dir/URL added to a KB. Stored so update_kb knows what
    to re-ingest and which patterns were originally used."""

    path: str           # filesystem path or URL
    kind: str           # "file" | "dir" | "url" (url in M64)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    added_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )


@dataclass
class UserKBManifest:
    """Per-KB sidecar file — listed sources + creation timestamp.
    The KBMeta in kb.json describes the KB itself; this describes
    *what was added to it*."""

    kb_id: str
    name: str
    sources: list[KBSource] = field(default_factory=list)


def manifest_path(kb_dir: Path) -> Path:
    return kb_dir / "sources.json"


def load_manifest(kb_dir: Path) -> UserKBManifest | None:
    f = manifest_path(kb_dir)
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    sources = [KBSource(**s) for s in data.get("sources", [])]
    return UserKBManifest(
        kb_id=data["kb_id"], name=data["name"], sources=sources,
    )


def save_manifest(kb_dir: Path, manifest: UserKBManifest) -> None:
    kb_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "kb_id": manifest.kb_id,
        "name": manifest.name,
        "sources": [asdict(s) for s in manifest.sources],
    }
    manifest_path(kb_dir).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── ✂️ chunking ───────────────────────────────────────────────────────


def split_text(text: str, *, limit: int = CHUNK_CHAR_LIMIT,
                overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Sliding-window split. Identical to sessions_kb's split_text but
    reproduced here because the two might evolve independently (sessions
    have natural turn boundaries; arbitrary text doesn't)."""
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


# ── 📁 file discovery ─────────────────────────────────────────────────


def _is_supported(p: Path) -> bool:
    if p.name in SUPPORTED_BASENAMES:
        return True
    return p.suffix.lower() in SUPPORTED_EXTENSIONS


def _pattern_variants(pat: str) -> list[str]:
    """fnmatch doesn't have glob's ``**`` recursive-directory semantics.
    Generate alternates so ``**/*.py`` also matches a top-level
    ``foo.py`` (with no intermediate directory). Specifically, we strip
    any leading ``**/`` (or trailing ``/**``) so the inner pattern can
    match siblings outside of the directory traversal."""
    out = [pat]
    if pat.startswith("**/"):
        out.append(pat[3:])
    if pat.endswith("/**"):
        out.append(pat[:-3])
    return out


def _matches_patterns(p: Path, base: Path, includes: list[str],
                       excludes: list[str]) -> bool:
    """Glob match against the path *relative* to the base directory.
    No include patterns means 'match all'. Exclude wins ties.

    Patterns are expanded via :func:`_pattern_variants` so ``**/*.py``
    works the way users expect (matches top-level + nested)."""
    try:
        rel = p.relative_to(base).as_posix()
    except ValueError:
        rel = p.as_posix()

    def _hits(patterns: list[str]) -> bool:
        for pat in patterns:
            for variant in _pattern_variants(pat):
                if fnmatch.fnmatch(rel, variant):
                    return True
                if fnmatch.fnmatch(p.name, variant):
                    return True
        return False

    if _hits(excludes):
        return False
    if not includes:
        return True
    return _hits(includes)


def discover_files(path: Path, *, include: list[str] | None = None,
                    exclude: list[str] | None = None) -> list[Path]:
    """Walk ``path`` and return every supported file matching the
    include/exclude patterns. A single file path is returned as-is
    when supported."""
    include = include or []
    exclude = exclude or []
    if path.is_file():
        return [path] if _is_supported(path) else []
    if not path.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(path.rglob("*")):
        if not p.is_file():
            continue
        if not _is_supported(p):
            continue
        if not _matches_patterns(p, path, include, exclude):
            continue
        out.append(p)
    return out


# ── ✏️ KB CRUD on user-set KBs ────────────────────────────────────────


def add_kb(
    *,
    name: str,
    path: str | Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    embedder: Embedder | None = None,
    global_dir: Path | None = None,
) -> tuple[KnowledgeBase, int]:
    """Create or extend a KB by adding a file/dir source.

    Returns ``(kb, chunks_added)``. If a KB with this name already
    exists, the new source is appended; otherwise a fresh KB is
    created. Idempotent at the source level: re-adding the same path
    removes its prior chunks first.
    """
    root = kb_root_user(global_dir=global_dir)
    embedder = embedder or get_embedder()
    kb = KnowledgeBase.open(name=name, dir=root, kind="files",
                             embedder=embedder)
    path = Path(path).resolve()
    files = discover_files(path, include=include, exclude=exclude)
    if not files:
        return kb, 0

    # Idempotent: drop any prior chunks for these source paths
    source_ids = [str(f.resolve()) for f in files]
    for sid in source_ids:
        kb.remove_source(sid)

    chunks: list[Chunk] = []
    for f, sid in zip(files, source_ids):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, piece in enumerate(split_text(text)):
            chunks.append(Chunk(
                text=piece, source_id=sid, chunk_index=i,
                metadata={"path": sid, "name": f.name, "kb_name": name},
            ))
    n = kb.add_chunks(chunks) if chunks else 0

    # Update the sidecar manifest
    manifest = load_manifest(kb.dir) or UserKBManifest(kb_id=kb.meta.kb_id, name=name)
    src = KBSource(
        path=str(path), kind="dir" if path.is_dir() else "file",
        include=list(include or []), exclude=list(exclude or []),
    )
    # Replace if a source with the same path already existed
    manifest.sources = [s for s in manifest.sources if s.path != src.path]
    manifest.sources.append(src)
    save_manifest(kb.dir, manifest)
    return kb, n


def remove_kb(
    *,
    kb_id_or_name: str,
    global_dir: Path | None = None,
) -> bool:
    """Delete a user-set KB by id or by name. Returns True on success."""
    root = kb_root_user(global_dir=global_dir)
    # Try by id first
    if (root / kb_id_or_name).is_dir():
        return delete_kb(root, kb_id_or_name)
    # Fall back to name match
    for meta in list_user_kbs(global_dir=global_dir):
        if meta.name == kb_id_or_name:
            return delete_kb(root, meta.kb_id)
    return False


def list_user_kbs(*, global_dir: Path | None = None) -> list[KBMeta]:
    """All user-set KBs. The SessionsKB is NOT included — different
    storage root."""
    return list_kbs(kb_root_user(global_dir=global_dir))


def open_user_kb(*, name: str | None = None, kb_id: str | None = None,
                  embedder: Embedder | None = None,
                  global_dir: Path | None = None) -> KnowledgeBase | None:
    """Open a user KB by name or id. Returns None if not found."""
    root = kb_root_user(global_dir=global_dir)
    if kb_id:
        meta_file = root / kb_id / "kb.json"
        if not meta_file.is_file():
            return None
        return KnowledgeBase.open(
            name="", dir=root, kb_id=kb_id,
            embedder=embedder or get_embedder(),
        )
    if name:
        for meta in list_user_kbs(global_dir=global_dir):
            if meta.name == name:
                return KnowledgeBase.open(
                    name=name, dir=root, kb_id=meta.kb_id,
                    embedder=embedder or get_embedder(),
                )
    return None


def update_kb(
    *,
    kb_id_or_name: str,
    embedder: Embedder | None = None,
    global_dir: Path | None = None,
) -> dict:
    """Re-ingest every source listed in a KB's manifest. Returns
    ``{"sources": N, "chunks": M}``."""
    kb = (open_user_kb(kb_id=kb_id_or_name, embedder=embedder,
                        global_dir=global_dir)
          or open_user_kb(name=kb_id_or_name, embedder=embedder,
                           global_dir=global_dir))
    if kb is None:
        return {"sources": 0, "chunks": 0, "error": f"no KB named {kb_id_or_name!r}"}
    manifest = load_manifest(kb.dir)
    if manifest is None or not manifest.sources:
        return {"sources": 0, "chunks": 0}
    total_chunks = 0
    sources_done = 0
    for src in manifest.sources:
        if src.kind == "url":
            # M64 territory — handled there; skip in M63
            continue
        path = Path(src.path)
        files = discover_files(path, include=src.include, exclude=src.exclude)
        if not files:
            continue
        source_ids = [str(f.resolve()) for f in files]
        for sid in source_ids:
            kb.remove_source(sid)
        chunks: list[Chunk] = []
        for f, sid in zip(files, source_ids):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, piece in enumerate(split_text(text)):
                chunks.append(Chunk(
                    text=piece, source_id=sid, chunk_index=i,
                    metadata={"path": sid, "name": f.name, "kb_name": manifest.name},
                ))
        if chunks:
            kb.add_chunks(chunks)
            total_chunks += len(chunks)
            sources_done += 1
    return {"sources": sources_done, "chunks": total_chunks}


def clear_all_kbs(*, global_dir: Path | None = None) -> int:
    """Remove every user-set KB. Returns the count removed. Does NOT
    touch the SessionsKB (different root)."""
    root = kb_root_user(global_dir=global_dir)
    metas = list_user_kbs(global_dir=global_dir)
    for m in metas:
        delete_kb(root, m.kb_id)
    return len(metas)


# ── 🔍 search across user KBs ────────────────────────────────────────


def search_user_kbs(query: str, *, k: int = 5,
                     kb_id_or_name: str | None = None,
                     global_dir: Path | None = None,
                     embedder: Embedder | None = None) -> list[dict]:
    """Search one KB by id/name, or all user KBs when unset. Returns
    ``[{kb_id, kb_name, source, snippet, score}]`` sorted by score."""
    results: list[dict] = []
    if kb_id_or_name:
        kb = (open_user_kb(kb_id=kb_id_or_name, embedder=embedder,
                            global_dir=global_dir)
              or open_user_kb(name=kb_id_or_name, embedder=embedder,
                               global_dir=global_dir))
        kbs = [kb] if kb else []
    else:
        metas = list_user_kbs(global_dir=global_dir)
        kbs = [open_user_kb(kb_id=m.kb_id, embedder=embedder,
                             global_dir=global_dir) for m in metas]
        kbs = [k for k in kbs if k is not None]
    for kb in kbs:
        hits = kb.search(query, k=k)
        for h in hits:
            snip = h.chunk.text[:200].replace("\n", " ")
            results.append({
                "kb_id": kb.meta.kb_id,
                "kb_name": kb.meta.name,
                "source": h.chunk.source_id,
                "name": h.chunk.metadata.get("name", ""),
                "snippet": snip,
                "score": h.score,
            })
    results.sort(key=lambda r: r["score"])
    return results[:k]
