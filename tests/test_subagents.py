"""Tests for subagent definitions and the task tool (M12)."""

from langchain_core.messages import AIMessage

from talos.agents import agents_summary, discover_agents
from talos.tools import task_tool

from .fakes import FakeToolCallingModel


def _make_agent(tmp_path):
    d = tmp_path / ".talos" / "agents"
    d.mkdir(parents=True)
    (d / "researcher.md").write_text(
        "---\nname: researcher\ndescription: digs through code\n"
        "tools: read_file, grep\n---\nYou are a researcher.",
        encoding="utf-8",
    )


def test_discovery_parses_frontmatter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent(tmp_path)
    (defn,) = discover_agents()
    assert defn.name == "researcher"
    assert defn.tools == ["read_file", "grep"]
    assert "You are a researcher." in defn.system_prompt
    assert "researcher: digs through code" in agents_summary()


async def test_task_tool_runs_subagent_and_returns_final_answer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent(tmp_path)
    monkeypatch.setattr(
        task_tool, "build_llm",
        lambda model=None: FakeToolCallingModel(
            responses=[AIMessage(content="report: all good")]
        ),
    )
    out = await task_tool.task.ainvoke({"agent": "researcher", "prompt": "check x"})
    assert out == "report: all good"


async def test_task_tool_rejects_unknown_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = await task_tool.task.ainvoke({"agent": "ghost", "prompt": "boo"})
    assert "no subagent" in out
