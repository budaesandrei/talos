"""🔌 LLM factory.

Talos talks to any OpenAI-compatible endpoint through ``ChatOpenAI``.
That single class covers OpenAI, Anthropic (via its /v1 compat layer),
OpenRouter, Ollama, vLLM, LM Studio, … because they all speak the same
HTTP protocol — only ``base_url`` + ``api_key`` change.
"""

import os

# must be set BEFORE langchain_openai imports: stops it injecting a custom
# TCP-keepalive transport (which both prints a warning and breaks httpx's
# proxy autodetection). We pass our own clients anyway.
os.environ.setdefault("LANGCHAIN_OPENAI_TCP_KEEPALIVE", "0")

import httpx
from langchain_openai import ChatOpenAI

from talos.config import settings


def build_llm(model: str | None = None) -> ChatOpenAI:
    """Create a chat model from the current settings.

    ``model`` lets callers (e.g. ``talos chat --model ...`` or a subagent
    definition) override the configured default per invocation.
    """
    # Always supply our own httpx clients: it silences langchain-openai's
    # "injected a custom transport" warning AND keeps httpx's standard
    # proxy autodetection (trust_env). verify honors TALOS_VERIFY_SSL.
    extra = {
        "http_client": httpx.Client(verify=settings.verify_ssl),
        "http_async_client": httpx.AsyncClient(verify=settings.verify_ssl),
    }

    if settings.reasoning_effort:
        # 🧠 only sent when configured: non-reasoning models would 400
        extra["reasoning_effort"] = settings.reasoning_effort

    return ChatOpenAI(
        model=model or settings.model,
        api_key=settings.api_key or "not-set",  # local servers ignore it
        base_url=settings.base_url,
        temperature=settings.temperature,
        **extra,
    )


class _LocalEmbedder:
    """🏠 In-process embeddings via fastembed — ONNX, no torch, no server.

    Loads once (~50 MB for all-MiniLM-L6-v2, downloaded on first use) and
    embeds on CPU in milliseconds; at graph-memory volume (a handful of
    short texts per compaction) that's all the horsepower recall needs.
    Mirrors the two OpenAIEmbeddings methods graph memory calls, so the
    rest of the code never knows whether vectors came from HTTP or RAM.
    """

    def __init__(self, model: str):
        from fastembed import TextEmbedding  # lazy: optional [memory] dep

        # bare names get the sentence-transformers namespace for free
        name = model if "/" in model else f"sentence-transformers/{model}"
        self._model = TextEmbedding(model_name=name)

    def embed_query(self, text: str) -> list[float]:
        return [float(x) for x in next(iter(self._model.embed([text])))]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        # CPU-bound and tiny batches — not worth a thread pool
        return [[float(x) for x in v] for v in self._model.embed(texts)]


def build_embedder():
    """🧭 Embeddings client for vector recall in graph memory.

    Two modes, picked by TALOS_EMBED_MODEL:

    - ``local:all-MiniLM-L6-v2`` → in-process via fastembed (no endpoint)
    - anything else → the chat endpoint's OpenAI-compatible /embeddings
      (``text-embedding-3-small`` on OpenAI, ``nomic-embed-text`` on Ollama…)

    Returns ``None`` when unset — graph memory then degrades gracefully
    to keyword recall.
    """
    if not settings.embed_model:
        return None
    if settings.embed_model.startswith("local:"):
        return _LocalEmbedder(settings.embed_model[len("local:"):])
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        model=settings.embed_model,
        api_key=settings.api_key or "not-set",
        base_url=settings.base_url,
        http_client=httpx.Client(verify=settings.verify_ssl),
        http_async_client=httpx.AsyncClient(verify=settings.verify_ssl),
        # OpenAIEmbeddings pre-tokenizes with tiktoken and ships token IDs;
        # non-OpenAI endpoints (Ollama, vLLM…) want plain strings instead.
        check_embedding_ctx_length=False,
    )
