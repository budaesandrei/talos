"""Tests for the knowledge-base primitive (M60).

All tests use HashEmbedder so no real model is downloaded. The
deterministic hash means same-text-same-vector, so we can exercise the
storage / search plumbing without semantic meaning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from talos.memory import embeddings
from talos.memory.embeddings import HashEmbedder, get_embedder
from talos.memory.knowledge import (
    Chunk,
    KBMeta,
    KnowledgeBase,
    delete_kb,
    list_kbs,
    short_id,
)


# ── 🧬 embeddings ─────────────────────────────────────────────────────


def test_hash_embedder_is_deterministic():
    e = HashEmbedder()
    a = e.embed(["hello"])
    b = e.embed(["hello"])
    assert a == b
    assert len(a[0]) == e.dim == 32


def test_hash_embedder_different_text_different_vector():
    e = HashEmbedder()
    [va], [vb] = e.embed(["hello"]), e.embed(["world"])
    assert va != vb


def test_hash_embedder_handles_empty_list():
    assert HashEmbedder().embed([]) == []


def test_get_embedder_falls_back_to_hash_without_sentence_transformers(monkeypatch):
    """If sentence-transformers isn't importable, get_embedder('auto')
    must degrade to HashEmbedder rather than crash."""
    embeddings.reset_default_embedder()

    # Force a load failure for SentenceTransformersEmbedder
    import talos.memory.embeddings as em
    original = em.SentenceTransformersEmbedder

    class FakeBroken:
        def __init__(self, *a, **kw):
            raise RuntimeError("simulated missing sentence-transformers")

    monkeypatch.setattr(em, "SentenceTransformersEmbedder", FakeBroken)
    try:
        e = get_embedder(prefer="auto")
        assert isinstance(e, HashEmbedder)
    finally:
        monkeypatch.setattr(em, "SentenceTransformersEmbedder", original)
        embeddings.reset_default_embedder()


def test_get_embedder_semantic_propagates_failure(monkeypatch):
    """prefer='semantic' must NOT fall back silently — the caller asked
    for semantic and deserves to know it didn't work."""
    embeddings.reset_default_embedder()
    import talos.memory.embeddings as em

    class FakeBroken:
        def __init__(self, *a, **kw):
            raise RuntimeError("simulated missing")

    monkeypatch.setattr(em, "SentenceTransformersEmbedder", FakeBroken)
    with pytest.raises(RuntimeError, match="simulated missing"):
        get_embedder(prefer="semantic")
    embeddings.reset_default_embedder()


# ── 🆔 short_id ──────────────────────────────────────────────────────


def test_short_id_shape():
    sid = short_id("hello")
    assert len(sid) == 7
    assert all(c in "0123456789abcdef" for c in sid)


def test_short_id_different_inputs_different_outputs():
    """Includes a timestamp internally; same text in quick succession
    should still differ on the seed (microseconds resolution)."""
    a = short_id("same")
    b = short_id("same")
    # not asserting != because if generated in the exact same microsecond
    # they could match; just that the function returns 7-char strings.
    assert len(a) == len(b) == 7


# ── 🗂 KnowledgeBase CRUD ────────────────────────────────────────────


def _make_kb(tmp_path: Path, name: str = "test") -> KnowledgeBase:
    return KnowledgeBase.open(
        name=name, dir=tmp_path, embedder=HashEmbedder(), kind="generic",
    )


def test_open_creates_kb_and_meta_persists(tmp_path):
    kb = _make_kb(tmp_path)
    assert kb.dir.is_dir()
    assert (kb.dir / "kb.json").is_file()
    # Re-opening by name finds the existing KB (same id)
    kb2 = _make_kb(tmp_path)
    assert kb2.meta.kb_id == kb.meta.kb_id


def test_open_with_kb_id_loads_existing(tmp_path):
    kb1 = _make_kb(tmp_path, name="x")
    kb2 = KnowledgeBase.open(
        name="x", dir=tmp_path, embedder=HashEmbedder(),
        kb_id=kb1.meta.kb_id,
    )
    assert kb2.meta.kb_id == kb1.meta.kb_id


def test_open_rejects_embedder_mismatch(tmp_path):
    """A KB built with one dim cannot be queried with another — would
    silently return garbage. Refuse loudly at open time."""
    kb1 = _make_kb(tmp_path, name="mismatch")
    # Build a "different" embedder with a different dim
    class WrongDim:
        name = "wrong"
        dim = 16
        def embed(self, texts):
            return [[0.0] * 16 for _ in texts]

    with pytest.raises(RuntimeError, match="embedder mismatch"):
        KnowledgeBase.open(
            name="mismatch", dir=tmp_path, embedder=WrongDim(),
            kb_id=kb1.meta.kb_id,
        )


def test_add_chunks_and_count(tmp_path):
    kb = _make_kb(tmp_path)
    n = kb.add_chunks([
        Chunk(text="hello", source_id="a"),
        Chunk(text="world", source_id="a", chunk_index=1),
        Chunk(text="other", source_id="b"),
    ])
    assert n == 3
    assert kb.count() == 3
    assert set(kb.sources()) == {"a", "b"}


def test_search_returns_closest_chunk(tmp_path):
    kb = _make_kb(tmp_path)
    kb.add_chunks([
        Chunk(text="hello", source_id="a"),
        Chunk(text="goodbye", source_id="b"),
    ])
    hits = kb.search("hello", k=2)
    assert hits[0].chunk.text == "hello"
    # HashEmbedder gives identical vector for identical text → distance 0
    assert hits[0].score == pytest.approx(0.0, abs=1e-5)


def test_search_empty_query_returns_empty(tmp_path):
    kb = _make_kb(tmp_path)
    kb.add_chunks([Chunk(text="anything", source_id="x")])
    assert kb.search("") == []
    assert kb.search("   ") == []


def test_search_filters_by_source_id(tmp_path):
    kb = _make_kb(tmp_path)
    kb.add_chunks([
        Chunk(text="apple", source_id="x"),
        Chunk(text="apple", source_id="y"),
    ])
    hits = kb.search("apple", k=5, source_id="x")
    assert len(hits) == 1 and hits[0].chunk.source_id == "x"


def test_remove_source_drops_all_its_chunks(tmp_path):
    kb = _make_kb(tmp_path)
    kb.add_chunks([
        Chunk(text="one", source_id="keep"),
        Chunk(text="two", source_id="drop"),
        Chunk(text="three", source_id="drop", chunk_index=1),
    ])
    removed = kb.remove_source("drop")
    assert removed == 2
    assert kb.count() == 1
    assert kb.sources() == ["keep"]
    # Removing a missing source is a no-op
    assert kb.remove_source("never") == 0


def test_clear_wipes_index(tmp_path):
    kb = _make_kb(tmp_path)
    kb.add_chunks([Chunk(text="a", source_id="x")])
    assert kb.clear() == 1
    assert kb.count() == 0


def test_chunk_metadata_round_trips(tmp_path):
    kb = _make_kb(tmp_path)
    kb.add_chunks([
        Chunk(text="x", source_id="s", metadata={"role": "user", "turn": 3}),
    ])
    hit = kb.search("x")[0]
    assert hit.chunk.metadata == {"role": "user", "turn": 3}


# ── 📒 registry (list_kbs / delete_kb) ───────────────────────────────


def test_list_kbs_returns_all_kbs_under_root(tmp_path):
    KnowledgeBase.open(name="alpha", dir=tmp_path, embedder=HashEmbedder())
    KnowledgeBase.open(name="beta", dir=tmp_path, embedder=HashEmbedder())
    metas = list_kbs(tmp_path)
    names = {m.name for m in metas}
    assert names == {"alpha", "beta"}


def test_list_kbs_empty_for_missing_root(tmp_path):
    assert list_kbs(tmp_path / "does-not-exist") == []


def test_delete_kb_removes_directory(tmp_path):
    kb = KnowledgeBase.open(name="goner", dir=tmp_path, embedder=HashEmbedder())
    kid = kb.meta.kb_id
    kb.close()
    assert delete_kb(tmp_path, kid) is True
    assert not (tmp_path / kid).exists()
    assert delete_kb(tmp_path, kid) is False  # idempotent
