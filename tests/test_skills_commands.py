"""Tests for skills discovery and slash commands (M10)."""

from pathlib import Path

from talos.ui.commands import dispatch
from talos.lifecycle.skills import discover_skills, skill_body, skills_summary
from talos.tools.skill_tool import load_skill


def _make_skill(tmp_path, name="deploy", desc="how to deploy"):
    d = tmp_path / ".talos" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\nStep 1: don't panic.\n",
        encoding="utf-8",
    )


def test_skill_discovery_and_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_skill(tmp_path)
    found = discover_skills()
    assert [s.name for s in found] == ["deploy"]
    assert "deploy: how to deploy" in skills_summary()


def test_skill_body_is_lazy_loaded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_skill(tmp_path)
    assert "don't panic" in skill_body("deploy")
    assert "no skill named" in load_skill.invoke({"name": "nope"})


def test_dispatch_routes_correctly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_dir = tmp_path / ".talos" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "review.md").write_text("Review this: $ARGUMENTS", encoding="utf-8")

    assert dispatch("hello there") == ("chat", "hello there")
    assert dispatch("/help") == ("builtin", "/help")
    assert dispatch("/quit") == ("builtin", "/exit")
    assert dispatch("/review src/") == ("prompt", "Review this: src/")
    assert dispatch("/nope")[0] == "unknown"
