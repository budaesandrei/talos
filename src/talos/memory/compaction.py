"""🗜️ Context compaction — the trick that makes Talos long-running.

Every LLM call re-sends the entire history, so a long session eventually
(a) blows the model's context window and (b) re-bills the whole prefix on
every step. Compaction is the fix used by every 2026 long-horizon agent:
when the conversation grows past a threshold, summarize the OLD turns into
a compact brief and keep only the recent ones verbatim.

Talos compacts automatically. The signal is exact, not estimated: the
provider reports `input_tokens` with every reply (= the real size of the
context it just read), and we know the model's `max_input_tokens` from
/models. When usage crosses ``compact_at`` (default 70%), the next turn
folds everything except the last ``keep_recent`` messages into one summary
SystemMessage. The summary call itself is metered like any other.

Compacted text doesn't vanish — M34 routes it into the graph memory so
you can still recall topics from far behind.
"""

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

SUMMARY_MARKER = "📓 CONVERSATION SUMMARY (older turns, compacted)"

SUMMARY_PROMPT = """Summarize the conversation below into a compact briefing
that lets the assistant continue seamlessly. Preserve: decisions made,
facts established, files/paths touched, open threads, and the user's
goals and preferences. Use terse bullet points. Omit pleasantries."""


def estimate_tokens(messages: list[BaseMessage]) -> int:
    """Rough fallback when no provider count is available (~4 chars/token)."""
    chars = 0
    for m in messages:
        c = m.content
        chars += len(c) if isinstance(c, str) else len(str(c))
    return chars // 4


def is_summary(msg: BaseMessage) -> bool:
    return isinstance(msg, SystemMessage) and SUMMARY_MARKER in str(msg.content)


def _split(messages: list[BaseMessage], keep_recent: int):
    """Partition into (to_summarize, to_keep), preserving any existing
    summary and never splitting a tool call from its result."""
    existing = [m for m in messages if is_summary(m)]
    body = [m for m in messages if not is_summary(m)]

    if len(body) <= keep_recent:
        return existing, [], body

    cut = len(body) - keep_recent
    # don't strand a ToolMessage from the AIMessage that called it
    while cut < len(body) and isinstance(body[cut], ToolMessage):
        cut += 1
    return existing, body[:cut], body[cut:]


async def compact(
    messages: list[BaseMessage],
    summarize,                 # async (prior, transcript[, span]) -> str
    keep_recent: int = 6,
) -> tuple[list[BaseMessage], bool]:
    """Return (new_messages, did_compact).

    ``summarize`` is injected so this module stays LLM-agnostic and
    testable (the runtime passes a real LLM call; tests pass a fake).

    Time-awareness (M58): the time span of the folded turns is passed to
    ``summarize`` as a third argument when available, so the digest text
    can naturally mention "these turns spanned 2 days". Older summarize
    callables that take only two arguments still work — we fall back
    gracefully via TypeError catching.
    """
    from talos.agent.time_awareness import format_span, time_span

    existing, to_summarize, to_keep = _split(messages, keep_recent)
    if not to_summarize:
        return messages, False

    prior = "\n\n".join(str(m.content) for m in existing)
    transcript = "\n".join(
        f"{_role(m)}: {m.content}" for m in to_summarize if str(m.content).strip()
    )
    span_str = format_span(time_span(to_summarize))
    try:
        digest = await summarize(prior, transcript, span_str)
    except TypeError:
        # Two-arg callable — backward compat with pre-M58 callers + tests.
        digest = await summarize(prior, transcript)

    summary = SystemMessage(content=f"{SUMMARY_MARKER}\n{digest.strip()}")
    return [summary, *to_keep], True


def _role(m: BaseMessage) -> str:
    if isinstance(m, HumanMessage):
        return "user"
    if isinstance(m, AIMessage):
        return "assistant"
    if isinstance(m, ToolMessage):
        return f"tool[{m.name}]"
    return "system"


def fuel_gauge(used: int, limit: int | None, width: int = 8) -> str:
    """A tiny ▰▱ bar for the rprompt: how full is the context?"""
    if not limit:
        return ""
    frac = max(0.0, min(1.0, used / limit))
    filled = round(frac * width)
    bar = "▰" * filled + "▱" * (width - filled)
    return f"{bar} {frac*100:.0f}%"
