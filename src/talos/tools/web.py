"""🌐 Web tool — fetch a URL as readable text.

A deliberately small fetch: strip tags, collapse whitespace, truncate.
Enough for docs lookups without pulling in a full browser.
"""

import re

import httpx
from langchain_core.tools import tool

MAX_OUTPUT_CHARS = 8_000


@tool
def web_fetch(url: str) -> str:
    """Fetch a URL and return its text content (HTML tags stripped)."""
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return f"Error: {exc}"

    text = resp.text
    if "html" in resp.headers.get("content-type", ""):
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)

    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "… [truncated]"
    return text.strip()
