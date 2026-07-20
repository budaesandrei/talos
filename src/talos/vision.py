"""👁 Vision input — let multimodal models see images.

When a user message references an image — a path like ``./shot.png`` or a
pasted ``data:image/...`` URL — we turn the plain-text turn into a
multimodal message: a list of content blocks mixing text and image_url,
the format ChatOpenAI (and every OpenAI-compatible vision model) expects.

Especially handy for the snapshot-driven checks the user wanted: a
Playwright/desktop tool saves a screenshot, and Talos can actually look
at it to decide if the UI is right.

Detection is conservative — we only build image blocks when the model
supports vision (per /models) and the path/URL really resolves, so
text-only models and ordinary file mentions are never disturbed.
"""

import base64
import mimetypes
import re
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# bare paths ending in an image extension, or data: URLs
_PATH_RE = re.compile(r"(?:^|\s)([~./\w\-]+\.(?:png|jpe?g|gif|webp|bmp))\b", re.I)
_DATA_RE = re.compile(r"data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=]+")


def model_supports_vision(model_id: str) -> bool:
    from talos.models import lookup, provider_meta

    meta = provider_meta(model_id) or lookup(model_id)
    return bool(meta.get("supports_vision"))


def _encode(path: Path) -> str | None:
    try:
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        data = base64.b64encode(path.read_bytes()).decode()
        return f"data:{mime};base64,{data}"
    except OSError:
        return None


def extract_images(text: str) -> list[str]:
    """Return data-URL image sources found in the text (paths encoded)."""
    sources = []
    for m in _DATA_RE.findall(text):
        sources.append(m)
    for m in _PATH_RE.findall(text):
        p = Path(m).expanduser()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            url = _encode(p)
            if url:
                sources.append(url)
    return sources


def build_content(text: str, model_id: str):
    """Return either the plain string (no images / no vision) or a list of
    multimodal content blocks for ChatOpenAI."""
    if not model_supports_vision(model_id):
        return text
    images = extract_images(text)
    if not images:
        return text
    blocks = [{"type": "text", "text": text}]
    for src in images:
        blocks.append({"type": "image_url", "image_url": {"url": src}})
    return blocks
