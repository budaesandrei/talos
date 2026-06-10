"""Tests for parallel agent teams (M41)."""

from langchain_core.messages import AIMessage

from talos.tools import team_tool
from tests.fakes import FakeToolCallingModel


async def test_team_runs_briefs_in_parallel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # each worker gets its own fake model returning a distinct answer
    monkeypatch.setattr(
        team_tool, "build_llm",
        lambda model=None: FakeToolCallingModel(responses=[AIMessage(content="report")]),
    )
    out = await team_tool.team.ainvoke(
        {"briefs": ["research A", "research B", "research C"]})
    assert out.count("worker #") == 3
    # scratchpad captured the partial reports
    assert (tmp_path / ".talos" / "team_scratch.md").is_file()


async def test_team_caps_concurrency(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        team_tool, "build_llm",
        lambda model=None: FakeToolCallingModel(responses=[AIMessage(content="r")]),
    )
    many = [f"brief {i}" for i in range(20)]
    out = await team_tool.team.ainvoke({"briefs": many})
    assert out.count("### worker") == team_tool.MAX_WORKERS  # capped


async def test_empty_briefs():
    out = await team_tool.team.ainvoke({"briefs": []})
    assert "no briefs" in out
