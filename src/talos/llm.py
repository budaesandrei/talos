"""🔌 LLM factory.

Talos talks to any OpenAI-compatible endpoint through ``ChatOpenAI``.
That single class covers OpenAI, Anthropic (via its /v1 compat layer),
OpenRouter, Ollama, vLLM, LM Studio, … because they all speak the same
HTTP protocol — only ``base_url`` + ``api_key`` change.
"""

import httpx
from langchain_openai import ChatOpenAI

from talos.config import settings


def build_llm(model: str | None = None) -> ChatOpenAI:
    """Create a chat model from the current settings.

    ``model`` lets callers (e.g. ``talos chat --model ...`` or a subagent
    definition) override the configured default per invocation.
    """
    extra = {}
    if not settings.verify_ssl:
        # 🔓 corporate-proxy escape hatch: hand ChatOpenAI pre-built httpx
        # clients that skip certificate verification (see config.py).
        extra["http_client"] = httpx.Client(verify=False)
        extra["http_async_client"] = httpx.AsyncClient(verify=False)

    return ChatOpenAI(
        model=model or settings.model,
        api_key=settings.api_key or "not-set",  # local servers ignore it
        base_url=settings.base_url,
        temperature=settings.temperature,
        **extra,
    )
