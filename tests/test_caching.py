"""💾 Prompt-cache breakpoint tests (Anthropic cache_control markers)."""

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from talos.agent.caching import MAX_BREAKPOINTS, add_cache_breakpoints, cache_enabled
from talos.config import settings


def _breakpoints(msgs) -> int:
    n = 0
    for m in msgs:
        if isinstance(m.content, list):
            n += sum(
                1 for b in m.content
                if isinstance(b, dict) and "cache_control" in b
            )
    return n


def test_system_plus_recent_cascade_never_exceeds_limit():
    msgs = [SystemMessage(content="sys")] + [
        HumanMessage(content=f"q{i}") for i in range(10)
    ]
    out = add_cache_breakpoints(msgs)
    assert _breakpoints(out) == MAX_BREAKPOINTS
    # system prompt is a breakpoint…
    assert "cache_control" in out[0].content[0]
    # …and so are the LAST three messages (the rolling cascade)
    for m in out[-3:]:
        assert "cache_control" in m.content[-1]
    # older middle messages untouched
    assert isinstance(out[3].content, str)


def test_originals_are_not_mutated():
    msgs = [SystemMessage(content="sys"), HumanMessage(content="q")]
    add_cache_breakpoints(msgs)
    assert isinstance(msgs[0].content, str) and isinstance(msgs[1].content, str)


def test_tool_call_only_message_is_skipped_not_broken():
    msgs = [
        SystemMessage(content="s"),
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}]),
        ToolMessage(content="result", tool_call_id="1"),
    ]
    out = add_cache_breakpoints(msgs)
    # the empty AIMessage can't carry a marker and must survive as-is
    assert out[2].content == ""
    # markable neighbours picked up the cascade instead
    assert isinstance(out[3].content, list)
    assert _breakpoints(out) <= MAX_BREAKPOINTS


def test_multimodal_content_marks_last_block():
    msgs = [
        SystemMessage(content="s"),
        HumanMessage(content=[
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]),
    ]
    out = add_cache_breakpoints(msgs)
    assert "cache_control" in out[1].content[-1]
    assert "cache_control" not in out[1].content[0]


def test_cache_enabled_modes(monkeypatch):
    monkeypatch.setattr(settings, "prompt_cache", "off")
    assert not cache_enabled("claude-sonnet-4-5")
    monkeypatch.setattr(settings, "prompt_cache", "on")
    assert cache_enabled("gpt-4o-mini")
    monkeypatch.setattr(settings, "prompt_cache", "auto")
    assert cache_enabled("anthropic/claude-sonnet-4-5")
    assert cache_enabled("claude-haiku-4-5")
    assert not cache_enabled("gpt-4o-mini")  # OpenAI caches automatically
