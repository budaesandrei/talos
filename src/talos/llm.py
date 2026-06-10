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

    return ChatOpenAI(
        model=model or settings.model,
        api_key=settings.api_key or "not-set",  # local servers ignore it
        base_url=settings.base_url,
        temperature=settings.temperature,
        **extra,
    )
