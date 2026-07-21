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

from talos.config import parse_extra_headers, settings


class _BearerTokenAuth(httpx.Auth):
    """🔐 Refresh-per-request bearer auth (MSAL). The token getter is
    cheap — MSAL caches until near expiry — and setting the header at
    send time overrides the static api_key the OpenAI SDK put there,
    so long sessions never ride an expired token."""

    def __init__(self, get_token):
        self._get_token = get_token

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        yield request


def _client_kwargs() -> dict:
    """Shared httpx client config: TLS, 🏷️ enterprise extra headers
    (case preserved on HTTP/1.1 — some gateways are case-sensitive),
    and 🔐 MSAL bearer auth when configured."""
    kwargs: dict = {"verify": settings.verify_ssl}
    headers = parse_extra_headers()
    if headers:
        kwargs["headers"] = headers
    from talos.integrations.msal_auth import get_token, msal_enabled

    if msal_enabled():
        kwargs["auth"] = _BearerTokenAuth(get_token)
    return kwargs


def build_llm(model: str | None = None) -> ChatOpenAI:
    """Create a chat model from the current settings.

    ``model`` lets callers (e.g. ``talos chat --model ...`` or a subagent
    definition) override the configured default per invocation.
    """
    # Always supply our own httpx clients: it silences langchain-openai's
    # "injected a custom transport" warning AND keeps httpx's standard
    # proxy autodetection (trust_env). verify honors TALOS_VERIFY_SSL.
    ckw = _client_kwargs()
    extra = {
        "http_client": httpx.Client(**ckw),
        "http_async_client": httpx.AsyncClient(**ckw),
    }

    if settings.reasoning_effort:
        # 🧠 only sent when configured: non-reasoning models would 400
        extra["reasoning_effort"] = settings.reasoning_effort

    return ChatOpenAI(
        # 📊 without this, streamed responses from strict OpenAI-compatible
        # endpoints carry NO usage block at all — cost tracking would read
        # zeros. Sends stream_options={"include_usage": true}; the usage
        # (incl. cached-token details) arrives on the final chunk.
        stream_usage=settings.stream_usage,
        model=model or settings.model,
        api_key=settings.api_key or "not-set",  # local servers ignore it
        base_url=settings.base_url,
        temperature=settings.temperature,
        **extra,
    )
