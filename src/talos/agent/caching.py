"""💾 Prompt-cache breakpoints — Anthropic ``cache_control`` markers.

Anthropic-style models cache NOTHING unless the client explicitly marks
breakpoints (``"cache_control": {"type": "ephemeral"}`` on a content
block). A breakpoint caches the entire prefix up to and including that
block; at most 4 are allowed per request. OpenAI-family models are the
opposite: caching is automatic server-side and markers don't exist.

Why this matters most in Talos: the ReAct loop re-sends the whole
conversation on EVERY think→act step, re-billing the prefix each time.
With breakpoints, those repeats become cache reads (~0.1× input price)
after a one-time write (1.25×).

Layout used here (the standard cascade):

- 1 breakpoint on the system prompt — the stable prefix
- up to 3 on the most recent markable messages — the newest one covers
  the whole conversation; the older ones are fallbacks so a rolled
  window still hits a shorter cached prefix instead of missing entirely

Caching only pays off if history is append-only between calls — which
Talos's message list is (compaction rewrites it, costing one re-write).
"""

from langchain_core.messages import BaseMessage, SystemMessage

MAX_BREAKPOINTS = 4  # Anthropic's hard limit per request
_EPHEMERAL = {"type": "ephemeral"}


def cache_enabled(model_id: str) -> bool:
    """Should we add markers for this model?

    - TALOS_PROMPT_CACHE=off → never; =on → always
    - auto (default): Anthropic-family model ids, or a provider whose
      /models metadata prices cache WRITES (the Anthropic-style tell —
      OpenAI-style pricing has only reads, because writes are free and
      caching is automatic there). Uses only in-memory metadata, so it
      never blocks on a network fetch.
    """
    from talos.config import settings

    mode = (settings.prompt_cache or "auto").lower()
    if mode == "off":
        return False
    if mode == "on":
        return True
    mid = (model_id or "").lower()
    if "claude" in mid or "anthropic" in mid:
        return True
    from talos.integrations.models import provider_meta

    return provider_meta(model_id).get(
        "cache_creation_input_token_cost"
    ) is not None


def _mark(msg: BaseMessage) -> BaseMessage | None:
    """A copy of ``msg`` whose last content block carries cache_control,
    or None when there is nothing markable (e.g. a tool-call-only
    AIMessage with empty text)."""
    content = msg.content
    if isinstance(content, str):
        if not content.strip():
            return None
        blocks = [{"type": "text", "text": content,
                   "cache_control": dict(_EPHEMERAL)}]
    elif isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last = {**last, "cache_control": dict(_EPHEMERAL)}
        elif isinstance(last, str):
            last = {"type": "text", "text": last,
                    "cache_control": dict(_EPHEMERAL)}
        else:
            return None
        blocks = list(content[:-1]) + [last]
    else:
        return None
    copy = msg.model_copy()
    copy.content = blocks
    return copy


def add_cache_breakpoints(
    messages: list[BaseMessage], recent: int = 3
) -> list[BaseMessage]:
    """Marked copies of ``messages``: system prompt + the last ``recent``
    markable messages, never exceeding Anthropic's 4-breakpoint limit.
    The originals are never mutated — history stays clean."""
    out = list(messages)
    budget = MAX_BREAKPOINTS

    if out and isinstance(out[0], SystemMessage):
        marked = _mark(out[0])
        if marked is not None:
            out[0] = marked
            budget -= 1

    placed = 0
    for i in range(len(out) - 1, 0, -1):
        if placed >= min(recent, budget):
            break
        if isinstance(out[i], SystemMessage):
            continue
        marked = _mark(out[i])
        if marked is not None:
            out[i] = marked
            placed += 1
    return out
