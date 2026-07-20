"""Tests for resume-reprint (M62) — the visual reprint of prior turns
that appears after the banner when you `talos chat -r <id>`."""

from __future__ import annotations

import pytest

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.delenv("TALOS_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)


class _FakeRT:
    """Minimal stand-in for Runtime — just needs .messages and ._header()."""
    model_name = "gpt-4o-mini"
    def __init__(self, messages):
        self.messages = messages
    def _header(self):
        return "[bold]▌⚒ talos[/]"


def test_reprint_returns_count_of_visible_messages(capsys):
    from talos.agent.runtime import reprint_history
    rt = _FakeRT([
        HumanMessage(content="hello"),
        AIMessage(content="hi there"),
        HumanMessage(content="what's the time"),
        AIMessage(content="it's 3pm"),
    ])
    n = reprint_history(rt)
    assert n == 4
    out = capsys.readouterr().out
    assert "hello" in out
    assert "hi there" in out
    assert "resumed" in out.lower()


def test_reprint_filters_gap_notices(capsys):
    """M58 gap notices are UI hints, not conversation — they should NOT
    reappear when reprinting (would look weird in the scrollback)."""
    from talos.agent.runtime import reprint_history
    from talos.agent.time_awareness import gap_notice
    from datetime import timedelta

    notice = gap_notice(timedelta(hours=14))
    rt = _FakeRT([
        HumanMessage(content="real user line"),
        notice,
        HumanMessage(content="second real user line"),
    ])
    n = reprint_history(rt)
    assert n == 2  # the notice was filtered
    out = capsys.readouterr().out
    assert "14h" not in out  # the gap-notice text doesn't appear


def test_reprint_filters_random_system_messages(capsys):
    """A SystemMessage that's not a compaction summary is also UI hint
    territory (vault/scrubber/etc.) — filter out."""
    from talos.agent.runtime import reprint_history
    rt = _FakeRT([
        SystemMessage(content="some system note"),
        HumanMessage(content="real line"),
    ])
    n = reprint_history(rt)
    assert n == 1


def test_reprint_keeps_compaction_summary(capsys):
    """The 'older turns compacted' marker should be visible so the user
    knows the conversation has a folded history."""
    from talos.agent.runtime import reprint_history
    from talos.memory.compaction import SUMMARY_MARKER
    rt = _FakeRT([
        SystemMessage(content=f"{SUMMARY_MARKER}\nsome digest"),
        HumanMessage(content="continuing the conversation"),
    ])
    n = reprint_history(rt)
    assert n == 2
    out = capsys.readouterr().out
    assert "compacted" in out


def test_reprint_renders_tool_calls(capsys):
    from talos.agent.runtime import reprint_history
    rt = _FakeRT([
        HumanMessage(content="list the files"),
        AIMessage(
            content="",
            tool_calls=[{"name": "list_dir", "args": {"path": "."}, "id": "1"}],
        ),
        ToolMessage(content="file_a.py\nfile_b.py", tool_call_id="1", name="list_dir"),
        AIMessage(content="here they are"),
    ])
    n = reprint_history(rt)
    assert n == 4
    out = capsys.readouterr().out
    assert "list_dir" in out
    assert "file_a.py" in out  # the tool result preview


def test_reprint_returns_zero_for_empty(capsys):
    from talos.agent.runtime import reprint_history
    rt = _FakeRT([])
    assert reprint_history(rt) == 0
    out = capsys.readouterr().out
    assert "resumed" not in out.lower()
