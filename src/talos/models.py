"""📇 Model discovery & pricing.

`GET /v1/models` IS standard across OpenAI-compatible providers — but only
the bare minimum (id, owned_by). The juicy fields the ecosystem wants
(max_input_tokens, input_cost_per_token, supports_vision, …) are NOT part
of the OpenAI spec; you see them on OpenRouter's /models and on LiteLLM
proxies, and nowhere else.

So Talos does what LiteLLM users do: enrich whatever /models returns with
LiteLLM's community-maintained pricing & capability database (cached for a
week under ~/.talos/cache). Providers that ship their own metadata
(OpenRouter) win over the database.
"""

import json
import time
from pathlib import Path

import httpx
from pydantic import BaseModel

from talos.config import settings

PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
CACHE_FILE = Path.home() / ".talos" / "cache" / "model_prices.json"
CACHE_TTL = 7 * 86_400  # a week


class ModelInfo(BaseModel):
    id: str
    context: int | None = None        # max input tokens
    input_per_m: float | None = None  # $ per 1M input tokens
    output_per_m: float | None = None
    vision: bool | None = None        # multimodal (image input)?


def _price_db() -> dict:
    """LiteLLM's pricing JSON, cached. Failure → {} (cost just hides)."""
    try:
        if CACHE_FILE.is_file() and time.time() - CACHE_FILE.stat().st_mtime < CACHE_TTL:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        resp = httpx.get(PRICES_URL, timeout=15, verify=settings.verify_ssl)
        resp.raise_for_status()
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(resp.text, encoding="utf-8")
        return resp.json()
    except Exception:
        try:  # stale cache beats nothing
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}


def lookup(model_id: str, db: dict | None = None) -> dict:
    """Find a model in the pricing db — exact id, then without the
    provider prefix ('anthropic/claude-x' → 'claude-x')."""
    db = _price_db() if db is None else db
    for key in (model_id, model_id.split("/")[-1]):
        if key in db:
            return db[key]
    return {}


def _normalize_payload(payload) -> list[dict]:
    """/models responses come in three shapes in the wild:
    {"data": [...]}, a bare [...] list (some compat layers, incl.
    Anthropic's), and {"models": [...]} (Gemini-style). Items are usually
    dicts but occasionally plain id strings. Normalize all of it."""
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = payload.get("data") or payload.get("models") or []
    else:
        entries = []
    return [{"id": e} if isinstance(e, str) else e
            for e in entries if isinstance(e, (str, dict))]


def parse_models(payload, db: dict | None = None) -> list[ModelInfo]:
    """Standard fields from /models + enrichment (OpenRouter > LiteLLM db)."""
    out = []
    for entry in _normalize_payload(payload):
        mid = entry.get("id") or entry.get("name", "")
        if not mid:
            continue
        meta = lookup(mid, db)
        pricing = entry.get("pricing") or {}        # OpenRouter extension
        arch = entry.get("architecture") or {}      # OpenRouter extension

        def per_m(or_key, ll_key):
            if pricing.get(or_key) is not None:
                try:
                    return float(pricing[or_key]) * 1_000_000
                except (TypeError, ValueError):
                    pass
            if meta.get(ll_key) is not None:
                return float(meta[ll_key]) * 1_000_000
            return None

        vision = None
        if arch.get("modality"):
            vision = "image" in str(arch["modality"])
        elif "supports_vision" in meta:
            vision = bool(meta["supports_vision"])

        out.append(
            ModelInfo(
                id=mid,
                context=entry.get("context_length") or meta.get("max_input_tokens"),
                input_per_m=per_m("prompt", "input_cost_per_token"),
                output_per_m=per_m("completion", "output_cost_per_token"),
                vision=vision,
            )
        )
    return out


def list_models() -> list[ModelInfo]:
    """Hit the provider's /models endpoint (the standard part)."""
    base = (settings.base_url or "https://api.openai.com/v1").rstrip("/")
    resp = httpx.get(
        f"{base}/models",
        headers={"Authorization": f"Bearer {settings.api_key}"},
        timeout=20,
        verify=settings.verify_ssl,
    )
    resp.raise_for_status()
    return parse_models(resp.json())


def estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    """Dollars for this many tokens, or None when pricing is unknown."""
    meta = lookup(model_id)
    cin, cout = meta.get("input_cost_per_token"), meta.get("output_cost_per_token")
    if cin is None and cout is None:
        return None
    return input_tokens * float(cin or 0) + output_tokens * float(cout or 0)
