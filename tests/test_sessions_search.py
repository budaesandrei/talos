"""Tests for sessions search + auto-ingest + fuzzy resume (M61)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from langchain_core.messages import AIMessage, HumanMessage

from talos.memory import embeddings, sessions, sessions_kb


@pytest.fixture(autouse=True)
def _isolated_and_autoindex_on(tmp_path, monkeypatch):
    """This file specifically WANTS to exercise auto-ingest, so we
    re-enable the env var (the global conftest turns it off)."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir(); project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TALOS_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("TALOS_SESSIONS_AUTOINDEX", "true")
    monkeypatch.chdir(project)
    # Pin the hash embedder so no model is downloaded
    embeddings.configure_default(embeddings.HashEmbedder())
    yield
    embeddings.reset_default_embedder()


# ── 🔁 auto-ingest hook ───────────────────────────────────────────────


def test_save_session_auto_ingests_into_kb():
    sid = sessions.new_session_id()
    sessions.save_session(sid, [
        HumanMessage(content="how do I parse JSON in python"),
        AIMessage(content="use the json module's loads function"),
    ])
    kb = sessions_kb.open_sessions_kb()
    assert kb.count() == 2
    assert sid in kb.sources()


def test_autoindex_env_var_disables_hook(monkeypatch):
    monkeypatch.setenv("TALOS_SESSIONS_AUTOINDEX", "false")
    sid = sessions.new_session_id()
    sessions.save_session(sid, [HumanMessage(content="should not be indexed")])
    kb = sessions_kb.open_sessions_kb()
    assert kb.count() == 0


def test_autoingest_failure_does_not_break_save(monkeypatch):
    """A bad embedder must not block save_session — the session JSON
    is the load-bearing artifact; indexing is best-effort."""
    class Broken:
        name = "broken"
        dim = 32
        def embed(self, texts):
            raise RuntimeError("simulated embed failure")
    embeddings.configure_default(Broken())
    sid = sessions.new_session_id()
    # Should NOT raise
    sessions.save_session(sid, [HumanMessage(content="ok")])
    # File still on disk
    assert (sessions.sessions_dir() / f"{sid}.json").is_file()


# ── 🔍 search returns + ranks ────────────────────────────────────────


def test_search_finds_session_by_content():
    sid = sessions.new_session_id()
    sessions.save_session(sid, [
        HumanMessage(content="auth refactor — how to split the validator"),
    ])
    kb = sessions_kb.open_sessions_kb()
    hits = sessions_kb.search_sessions(
        "auth refactor — how to split the validator", k=3, kb=kb,
    )
    assert hits and hits[0].chunk.source_id == sid


def test_search_handles_empty_index_gracefully():
    kb = sessions_kb.open_sessions_kb()
    assert kb.count() == 0
    # Searching an empty KB returns [] rather than raising
    hits = sessions_kb.search_sessions("anything", k=3, kb=kb)
    assert hits == []


# ── 🤖 agent tools (search_sessions_tool / list_sessions_tool) ───────


def test_search_sessions_tool_returns_json():
    from talos.tools.sessions_tool import search_sessions_tool

    sid = sessions.new_session_id()
    sessions.save_session(sid, [HumanMessage(content="something distinctive")])
    out = search_sessions_tool.invoke({"query": "something distinctive", "k": 1})
    assert sid in out
    parsed = json.loads(out)
    assert isinstance(parsed, list) and parsed[0]["session_id"] == sid


def test_search_sessions_tool_rejects_empty_query():
    from talos.tools.sessions_tool import search_sessions_tool
    out = search_sessions_tool.invoke({"query": "", "k": 3})
    assert out.startswith("Error")


def test_list_sessions_tool_returns_current_project_by_default():
    from talos.tools.sessions_tool import list_sessions_tool
    sid = sessions.new_session_id()
    sessions.save_session(sid, [HumanMessage(content="x")])
    out = list_sessions_tool.invoke({})
    parsed = json.loads(out)
    assert any(r["id"] == sid for r in parsed)


def test_search_sessions_tool_no_hits_returns_message():
    from talos.tools.sessions_tool import search_sessions_tool
    out = search_sessions_tool.invoke({"query": "nothing here", "k": 5})
    assert "no matching sessions" in out.lower()


# ── 💾 fuzzy resume (Runtime._resolve_resume) ────────────────────────


def test_resolve_resume_latest():
    from talos.agent.runtime import _resolve_resume
    sid = sessions.new_session_id()
    sessions.save_session(sid, [HumanMessage(content="hi")])
    assert _resolve_resume("latest") == sid


def test_resolve_resume_exact_id():
    from talos.agent.runtime import _resolve_resume
    sid = sessions.new_session_id()
    sessions.save_session(sid, [HumanMessage(content="hi")])
    assert _resolve_resume(sid) == sid


def test_resolve_resume_fuzzy_picks_best_match():
    from talos.agent.runtime import _resolve_resume
    sid_a = sessions.new_session_id()
    sessions.save_session(sid_a, [HumanMessage(content="auth refactor topic")])
    import time; time.sleep(1.1)  # ensure distinct second id
    sid_b = sessions.new_session_id()
    sessions.save_session(sid_b, [HumanMessage(content="something else entirely")])
    # Fuzzy query that's exact-text for sid_a → that's the best match
    chosen = _resolve_resume("auth refactor topic")
    assert chosen == sid_a


def test_resolve_resume_no_match_raises():
    """When no exact id matches and the KB is empty, we raise FileNotFoundError."""
    from talos.agent.runtime import _resolve_resume
    with pytest.raises(FileNotFoundError):
        _resolve_resume("does-not-exist")
