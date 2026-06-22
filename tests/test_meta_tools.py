"""Tests for the M66 agent-tool backfill — every non-safety CLI verb
exposed as a tool so the agent can drive Talos in natural language."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from talos.infra import vault
from talos.infra.vault import InMemoryBackend
from talos.memory import embeddings


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir(); project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TALOS_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(project)
    embeddings.configure_default(embeddings.HashEmbedder())
    vault.configure(persistent=InMemoryBackend(), session=InMemoryBackend())
    vault._session_index.clear()
    yield
    embeddings.reset_default_embedder()


# ── 📅 schedules ──────────────────────────────────────────────────────


def test_list_schedules_tool_returns_empty_array():
    from talos.tools.meta_tools import list_schedules_tool
    out = list_schedules_tool.invoke({})
    assert json.loads(out) == []


def test_schedule_add_tool_creates_then_list_finds_it():
    from talos.tools.meta_tools import list_schedules_tool, schedule_add_tool
    out = schedule_add_tool.invoke({
        "prompt": "say hi", "cron": "0 9 * * *", "name": "greet",
    })
    parsed = json.loads(out)
    assert parsed["id"] == "greet"
    assert parsed["cron"] == "0 9 * * *"
    # And it's discoverable
    listed = json.loads(list_schedules_tool.invoke({}))
    assert any(s["id"] == "greet" for s in listed)


def test_schedule_add_tool_rejects_bad_cron():
    from talos.tools.meta_tools import schedule_add_tool
    out = schedule_add_tool.invoke({"prompt": "hi", "cron": "not a cron"})
    assert out.startswith("Error")


def test_schedule_show_tool_returns_schedule_and_runs():
    from talos.tools.meta_tools import schedule_add_tool, schedule_show_tool
    schedule_add_tool.invoke({"prompt": "x", "cron": "0 * * * *", "name": "s1"})
    out = schedule_show_tool.invoke({"schedule_id": "s1"})
    parsed = json.loads(out)
    assert parsed["schedule"]["id"] == "s1"
    assert "recent_runs" in parsed


def test_schedule_show_tool_missing_id():
    from talos.tools.meta_tools import schedule_show_tool
    out = schedule_show_tool.invoke({"schedule_id": "nope"})
    assert out.startswith("Error") and "no schedule" in out


def test_schedule_remove_tool():
    from talos.tools.meta_tools import schedule_add_tool, schedule_remove_tool
    schedule_add_tool.invoke({"prompt": "x", "cron": "0 * * * *", "name": "g"})
    out = schedule_remove_tool.invoke({"schedule_id": "g"})
    assert out == "removed"
    # Idempotent — second remove reports not found
    assert "no schedule" in schedule_remove_tool.invoke({"schedule_id": "g"})


# ── 📬 runs ──────────────────────────────────────────────────────────


def test_list_runs_tool_returns_empty_array_when_no_runs():
    from talos.tools.meta_tools import list_runs_tool
    out = list_runs_tool.invoke({})
    assert json.loads(out) == []


def test_list_runs_tool_filtered_by_schedule_id():
    """Write a fake run record directly and confirm the tool reads it."""
    from datetime import datetime
    from talos.lifecycle.scheduling import write_run
    from talos.tools.meta_tools import list_runs_tool

    write_run(
        "myjob", datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 9, 0, 1),
        status="ok", prompt="ping", response="pong",
    )
    out = list_runs_tool.invoke({"schedule_id": "myjob"})
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["response"] == "pong"


# ── ⏪ checkpoints ───────────────────────────────────────────────────


def test_list_checkpoints_tool_empty_when_none():
    from talos.tools.meta_tools import list_checkpoints_tool
    out = list_checkpoints_tool.invoke({})
    assert json.loads(out) == []


# ── 🎒 skills ────────────────────────────────────────────────────────


def test_create_skill_tool_writes_file():
    from talos.lifecycle.skills import discover_skills, skills_dir
    from talos.tools.meta_tools import create_skill_tool

    out = create_skill_tool.invoke({
        "name": "deploy",
        "description": "how to deploy this project",
        "body": "1. run tests\n2. push to main",
    })
    assert "saved skill" in out
    f = skills_dir() / "deploy" / "SKILL.md"
    assert f.is_file()
    txt = f.read_text()
    assert "deploy" in txt
    assert "how to deploy" in txt
    assert "push to main" in txt
    # Discovery picks it up
    assert any(s.name == "deploy" for s in discover_skills())


def test_create_skill_tool_refuses_overwrite():
    from talos.tools.meta_tools import create_skill_tool
    create_skill_tool.invoke({"name": "x", "description": "d", "body": "b"})
    out = create_skill_tool.invoke({"name": "x", "description": "d", "body": "b"})
    assert out.startswith("Error") and "already exists" in out


def test_create_skill_tool_validates_required_fields():
    from talos.tools.meta_tools import create_skill_tool
    out = create_skill_tool.invoke({"name": "", "description": "d", "body": "b"})
    assert out.startswith("Error")
    out = create_skill_tool.invoke({"name": "ok", "description": "", "body": "b"})
    assert out.startswith("Error")


# ── 🔐 vault listing (read-only) ─────────────────────────────────────


def test_list_vault_handles_tool_returns_metadata_only():
    """SECRET values must NEVER appear in the tool output."""
    from talos.tools.meta_tools import list_vault_handles_tool

    vault.add_entry("ghpat", "supersecret123",
                     kind="secret", description="github PAT", scope="project")
    vault.add_entry("dashboard", "https://x.example.com",
                     kind="value", description="prod URL", scope="project")
    out = list_vault_handles_tool.invoke({})
    parsed = json.loads(out)
    assert any(h["handle"] == "ghpat" for h in parsed)
    # SECRET value NEVER in the output
    assert "supersecret123" not in out
    # VALUE handles are still listed (their values are in vault_summary
    # via the system prompt, not this tool)
    assert any(h["handle"] == "dashboard" for h in parsed)


# ── 📇 models ────────────────────────────────────────────────────────


def test_list_models_tool_handles_errors_gracefully(monkeypatch):
    """If /v1/models can't be reached, the tool returns an error
    string — never crashes."""
    from talos.tools.meta_tools import list_models_tool

    def boom():
        raise ConnectionError("no provider configured")
    monkeypatch.setattr("talos.integrations.models.list_models", boom)
    out = list_models_tool.invoke({})
    assert out.startswith("Error")


# ── 🔌 MCP + 🔗 links ────────────────────────────────────────────────


def test_list_mcp_servers_tool_empty_config(tmp_path):
    from talos.tools.meta_tools import list_mcp_servers_tool
    out = list_mcp_servers_tool.invoke({})
    parsed = json.loads(out)
    assert parsed == {}


def test_list_links_tool_empty():
    from talos.tools.meta_tools import list_links_tool
    out = list_links_tool.invoke({})
    assert json.loads(out) == []


# ── 🔬 sanity: full tool registry includes all the new ones ──────────


def test_get_tools_contains_meta_tools():
    from talos.tools import get_tools
    names = {t.name for t in get_tools()}
    expected = {
        "list_schedules_tool", "schedule_add_tool", "schedule_remove_tool",
        "schedule_show_tool", "list_runs_tool",
        "list_models_tool", "list_checkpoints_tool", "create_skill_tool",
        "list_vault_handles_tool", "list_links_tool", "list_mcp_servers_tool",
    }
    missing = expected - names
    assert not missing, f"missing tools: {missing}"
