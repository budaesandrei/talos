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

# fail fast on connect; bound the read. A models list is small — no reason
# to wait 20s. This is the difference between "quick" and "looks hung".
_HTTP_TIMEOUT = httpx.Timeout(connect=4.0, read=10.0, write=4.0, pool=4.0)

from talos.config import settings

PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
CACHE_FILE = Path.home() / ".talos" / "cache" / "model_prices.json"
CACHE_TTL = 7 * 86_400  # a week

# 🧯 Bundled snapshot for when the LiteLLM db is unreachable (offline,
# corporate proxy, …). Costs are $/token; approximate by design — the
# fetched db wins whenever available. Update casually.
FALLBACK_PRICES = {
    "claude-sonnet-4-5":  {"max_input_tokens": 200000, "input_cost_per_token": 3e-6,    "output_cost_per_token": 15e-6,  "supports_vision": True},
    "claude-opus-4-1":    {"max_input_tokens": 200000, "input_cost_per_token": 15e-6,   "output_cost_per_token": 75e-6,  "supports_vision": True},
    "claude-haiku-4-5":   {"max_input_tokens": 200000, "input_cost_per_token": 1e-6,    "output_cost_per_token": 5e-6,   "supports_vision": True},
    "gpt-4o":             {"max_input_tokens": 128000, "input_cost_per_token": 2.5e-6,  "output_cost_per_token": 10e-6,  "supports_vision": True},
    "gpt-4o-mini":        {"max_input_tokens": 128000, "input_cost_per_token": 0.15e-6, "output_cost_per_token": 0.6e-6, "supports_vision": True},
    "gpt-4.1":            {"max_input_tokens": 1047576,"input_cost_per_token": 2e-6,    "output_cost_per_token": 8e-6,   "supports_vision": True},
    "o3":                 {"max_input_tokens": 200000, "input_cost_per_token": 2e-6,    "output_cost_per_token": 8e-6,   "supports_vision": True},
    "deepseek-chat":      {"max_input_tokens": 65536,  "input_cost_per_token": 0.27e-6, "output_cost_per_token": 1.1e-6, "supports_vision": False},
}

_db_memo: dict | None = None      # GitHub db: one resolution per process
_models_memo: list | None = None   # /models list: fetched once per process
_provider_meta: dict = {}          # raw per-model metadata from /models


class ModelInfo(BaseModel):
    id: str
    context: int | None = None        # max input tokens
    input_per_m: float | None = None  # $ per 1M input tokens
    output_per_m: float | None = None
    vision: bool | None = None        # multimodal (image input)?


def _price_db() -> dict:
    """LiteLLM's pricing JSON, cached on disk + memoized in process.
    Unreachable → bundled FALLBACK_PRICES (approximate beats absent)."""
    global _db_memo
    if _db_memo is not None:
        return _db_memo
    _db_memo = _fetch_price_db() or FALLBACK_PRICES
    return _db_memo


def _fetch_price_db() -> dict:
    try:
        if CACHE_FILE.is_file() and time.time() - CACHE_FILE.stat().st_mtime < CACHE_TTL:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        resp = httpx.get(PRICES_URL, timeout=_HTTP_TIMEOUT, verify=settings.verify_ssl)
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


def _entry_meta(entry: dict) -> dict:
    """Pricing/capability fields straight from the provider's /models
    entry. Enterprise gateways (LiteLLM proxies etc.) return
    input_cost_per_token / output_cost_per_token either at the top level
    or under model_info — and those are YOUR negotiated prices, more
    accurate than any public database. OpenRouter's pricing block is
    translated to the same shape."""
    info = {**(entry.get("model_info") or {}), **entry}
    out = {}
    for key in ("input_cost_per_token", "output_cost_per_token",
                "max_input_tokens", "supports_vision"):
        if info.get(key) is not None:
            out[key] = info[key]
    pricing = entry.get("pricing") or {}
    try:
        if pricing.get("prompt") is not None:
            out.setdefault("input_cost_per_token", float(pricing["prompt"]))
        if pricing.get("completion") is not None:
            out.setdefault("output_cost_per_token", float(pricing["completion"]))
    except (TypeError, ValueError):
        pass
    if entry.get("context_length"):
        out.setdefault("max_input_tokens", entry["context_length"])
    return out


def provider_meta(model_id: str) -> dict:
    """Cached /models metadata for one model (exact id, then bare name)."""
    for key in (model_id, model_id.split("/")[-1]):
        if key in _provider_meta:
            return _provider_meta[key]
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
    db = _price_db() if db is None else db  # 🐛 resolve ONCE — resolving
    # per-model meant one (possibly 15s-timeout) fetch attempt per row,
    # which is exactly how '/models hangs' bugs are born
    out = []
    for entry in _normalize_payload(payload):
        mid = entry.get("id") or entry.get("name", "")
        if not mid:
            continue
        # provider's own fields first, public db second
        meta = {**lookup(mid, db), **_entry_meta(entry)}
        arch = entry.get("architecture") or {}      # OpenRouter extension

        def per_m(key):
            return float(meta[key]) * 1_000_000 if meta.get(key) is not None else None

        vision = None
        if arch.get("modality"):
            vision = "image" in str(arch["modality"])
        elif "supports_vision" in meta:
            vision = bool(meta["supports_vision"])

        out.append(
            ModelInfo(
                id=mid,
                context=meta.get("max_input_tokens"),
                input_per_m=per_m("input_cost_per_token"),
                output_per_m=per_m("output_cost_per_token"),
                vision=vision,
            )
        )
    return out


def list_models(refresh: bool = False) -> list[ModelInfo]:
    """The provider's /models — fetched ONCE per process, then cached.
    Also caches each entry's raw metadata so estimate_cost can use the
    provider's own per-token prices."""
    global _models_memo
    if _models_memo is not None and not refresh:
        return _models_memo
    base = (settings.base_url or "https://api.openai.com/v1").rstrip("/")
    resp = httpx.get(
        f"{base}/models",
        headers={"Authorization": f"Bearer {settings.api_key}"},
        timeout=_HTTP_TIMEOUT,
        verify=settings.verify_ssl,
    )
    resp.raise_for_status()
    payload = resp.json()
    for entry in _normalize_payload(payload):
        mid = entry.get("id") or entry.get("name", "")
        meta = _entry_meta(entry)
        if mid and meta:
            _provider_meta[mid] = meta
    _models_memo = parse_models(payload)
    return _models_memo


_prime_error: str | None = None  # why the startup prime failed, if it did


def prime_models_cache() -> int:
    """🔥 Called in the background at chat startup: one /models round trip
    warms both the picker and the cost engine. Returns models found."""
    global _prime_error
    try:
        n = len(list_models())
        _prime_error = None
        return n
    except Exception as exc:
        _prime_error = f"{type(exc).__name__}: {exc}"
        return 0


def estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    """Dollars for this many tokens. Pricing priority:

    1. the provider's own /models metadata (your enterprise prices)
    2. the public LiteLLM db (GitHub, cached) / bundled fallback
    3. unknown → None (the UI hides cost rather than guessing)

    If only one side is priced, the other coalesces to 0.
    """
    meta = provider_meta(model_id) or lookup(model_id)
    cin, cout = meta.get("input_cost_per_token"), meta.get("output_cost_per_token")
    if cin is None and cout is None:
        return None
    return input_tokens * float(cin or 0) + output_tokens * float(cout or 0)
