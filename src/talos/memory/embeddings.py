"""🧬 Embeddings — turn text into vectors so we can semantic-search it.

The knowledge-base layer (M60) needs an injectable embedding function.
Three implementations live here:

* ``SentenceTransformersEmbedder`` (default) — ~80MB ``all-MiniLM-L6-v2``
  model from the ``sentence-transformers`` package. Downloads once to
  ``~/.cache/huggingface`` on first use, runs locally thereafter. 384-dim
  embeddings; same model kiro uses for its semantic ``/knowledge`` index.

* ``HashEmbedder`` (tests + fallback) — deterministic, no model, fixed
  dimension. Vectors are derived from text via MD5 hashing so the same
  input always produces the same output. Not semantically meaningful
  (a query for "auth" won't find a doc about "login"), but offline,
  zero-dep, and good enough for tests of the surrounding plumbing.

* ``Embedder`` (Protocol) — the interface every backend implements:
  ``embed(texts) -> list[list[float]]`` plus ``name`` and ``dim``.

The default ``get_embedder()`` selects sentence-transformers if it's
installed, else falls back to HashEmbedder with a clear warning so the
user knows search quality will be poor until they install
``pip install -e ".[knowledge]"``.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Protocol


log = logging.getLogger(__name__)


# ── 🧭 the interface ──────────────────────────────────────────────────


class Embedder(Protocol):
    """A function that turns a list of strings into a list of vectors."""

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


# ── 🔢 deterministic hash fallback (tests + no-deps degradation) ──────


class HashEmbedder:
    """Deterministic, model-free embedder for tests and graceful
    degradation. NOT semantically meaningful — a query for "auth"
    won't find a doc about "login". But same-text-same-vector is
    enough to exercise the storage and search plumbing offline."""

    name = "hash-sha256"
    dim = 32  # SHA-256 produces exactly 32 bytes

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            digest = hashlib.sha256((t or "").encode("utf-8")).digest()
            # 32 bytes → 32 floats in [-1, 1]
            vec = [(b - 128) / 128.0 for b in digest]
            out.append(vec)
        return out


# ── 🤗 sentence-transformers (the real one) ───────────────────────────


class SentenceTransformersEmbedder:
    """Local semantic embedding via ``sentence-transformers``.

    The default model is ``all-MiniLM-L6-v2`` — ~80MB, 384-dim, the
    standard "small but good" choice for English semantic search. First
    use downloads it to ``~/.cache/huggingface``; offline thereafter."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2",
                 *, cache_folder: str | None = None) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "🧬 semantic embeddings need sentence-transformers — "
                'install with: pip install -e ".[knowledge]"'
            ) from exc
        # Honor HF_HOME / SENTENCE_TRANSFORMERS_HOME if set; otherwise
        # the package picks its default (~/.cache/huggingface).
        self._model = SentenceTransformer(model_name, cache_folder=cache_folder)
        self.name = model_name
        self.dim = self._model.get_sentence_embedding_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # convert_to_numpy=True is the fastest path; we cast to plain
        # python lists for sqlite-vec which expects JSON-able floats.
        vectors = self._model.encode(texts, convert_to_numpy=True,
                                      show_progress_bar=False)
        return [v.tolist() for v in vectors]


# ── 🏭 default selection ──────────────────────────────────────────────


_cached_default: Embedder | None = None


def get_embedder(*, prefer: str = "auto") -> Embedder:
    """Return an embedder. ``prefer='auto'`` tries sentence-transformers
    first, falls back to HashEmbedder with a warning. ``prefer='hash'``
    or ``prefer='semantic'`` forces a specific backend.

    Cached: the first call materializes the embedder (potentially
    downloading the model); subsequent calls reuse it."""
    global _cached_default
    if _cached_default is not None and prefer == "auto":
        return _cached_default

    if prefer == "hash":
        _cached_default = HashEmbedder()
        return _cached_default

    if prefer in ("semantic", "auto"):
        try:
            embedder: Embedder = SentenceTransformersEmbedder()
            if prefer == "auto":
                _cached_default = embedder
            return embedder
        except RuntimeError as exc:
            if prefer == "semantic":
                raise
            log.warning(
                "🧬 sentence-transformers not installed — "
                "knowledge search will use a non-semantic hash embedder. "
                'Install with: pip install -e ".[knowledge]"  (%s)', exc,
            )
            _cached_default = HashEmbedder()
            return _cached_default

    raise ValueError(f"unknown embedder preference: {prefer!r}")


def reset_default_embedder() -> None:
    """Test helper — clear the cache so the next ``get_embedder()`` call
    re-selects."""
    global _cached_default
    _cached_default = None


def configure_default(embedder: Embedder) -> None:
    """Test helper — pin a specific embedder as the default. Bypasses
    the auto-detection so tests don't accidentally download a model."""
    global _cached_default
    _cached_default = embedder
