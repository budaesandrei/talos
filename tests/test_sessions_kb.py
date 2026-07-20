"""Tests for SessionsKB ingestion + the global migration (M60)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from langchain_core.messages import AIMessage, HumanMessage, messages_to_dict

from talos.memory import sessions, sessions_kb
from talos.memory.embeddings import HashEmbedder


# ── 🧰 fixture: isolated HOME so the global sessions dir is per-test ──


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TALOS_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(project)
    yield


def _write_session(sid: str, messages: list) -> None:
    sessions.save_session(sid, messages)


# ── ➡️ migration of legacy cwd-local sessions ────────────────────────


def test_migrate_legacy_sessions_moves_files_and_stamps_project(tmp_path):
    """A legacy `.talos/sessions/<id>.json` in cwd should be copied into
    the global ~/.talos/sessions/ and stamped with project_path=cwd."""
    legacy = tmp_path / "project" / ".talos" / "sessions"
    legacy.mkdir(parents=True)
    (legacy / "20260101-100000.json").write_text(
        json.dumps(messages_to_dict([HumanMessage(content="legacy hi")])),
        encoding="utf-8",
    )
    res = sessions.migrate_legacy_sessions()
    assert res["migrated"] == 1
    assert res["skipped"] == 0
    # File copied
    global_file = sessions.sessions_dir() / "20260101-100000.json"
    assert global_file.is_file()
    # Metadata stamped
    meta = sessions.get_session_meta("20260101-100000")
    assert meta["project_path"] == sessions.current_project_path()


def test_migrate_is_idempotent(tmp_path):
    legacy = tmp_path / "project" / ".talos" / "sessions"
    legacy.mkdir(parents=True)
    (legacy / "x.json").write_text("[]", encoding="utf-8")
    sessions.migrate_legacy_sessions()
    res = sessions.migrate_legacy_sessions()
    assert res["migrated"] == 0
    assert res["skipped"] == 1


def test_migrate_returns_zero_when_no_legacy_dir(tmp_path):
    res = sessions.migrate_legacy_sessions()
    assert res["migrated"] == 0 and res["skipped"] == 0


# ── 🗃 list_sessions project filtering ───────────────────────────────


def test_list_sessions_defaults_to_current_project():
    """A session stamped with project A should only show in scope=here
    when cwd is also project A."""
    sid = sessions.new_session_id()  # stamps cwd as project_path
    _write_session(sid, [HumanMessage(content="hi")])
    rows = sessions.list_sessions(project="here")
    assert any(r["id"] == sid for r in rows)


def test_list_sessions_excludes_other_projects(tmp_path, monkeypatch):
    """A session from project A should NOT show when cwd is project B."""
    # Create a session in project A
    sid_a = sessions.new_session_id()
    _write_session(sid_a, [HumanMessage(content="in A")])
    # Switch cwd to project B
    proj_b = tmp_path / "project_b"
    proj_b.mkdir()
    monkeypatch.chdir(proj_b)
    rows = sessions.list_sessions(project="here")
    assert not any(r["id"] == sid_a for r in rows)


def test_list_sessions_all_returns_everything(tmp_path, monkeypatch):
    sid_a = sessions.new_session_id()
    _write_session(sid_a, [HumanMessage(content="in A")])
    proj_b = tmp_path / "project_b"
    proj_b.mkdir()
    monkeypatch.chdir(proj_b)
    sid_b = sessions.new_session_id()
    _write_session(sid_b, [HumanMessage(content="in B")])
    rows = sessions.list_sessions(project="all")
    ids = {r["id"] for r in rows}
    assert sid_a in ids and sid_b in ids


def test_legacy_sessions_without_project_path_still_show_in_here(tmp_path):
    """Sessions stamped before M60 won't have project_path. They should
    appear in 'here' scope (rather than vanishing from the default list)."""
    # Write a session WITHOUT going through new_session_id (no metadata)
    sid = "20250101-100000"
    sessions.save_session(sid, [HumanMessage(content="ancient")])
    rows = sessions.list_sessions(project="here")
    assert any(r["id"] == sid for r in rows)


# ── 🗂 SessionsKB ingestion ──────────────────────────────────────────


def test_ingest_session_indexes_messages():
    sid = sessions.new_session_id()
    _write_session(sid, [
        HumanMessage(content="how do I rename a file in python"),
        AIMessage(content="use os.rename or pathlib.Path.rename"),
    ])
    kb = sessions_kb.open_sessions_kb(embedder=HashEmbedder())
    n = sessions_kb.ingest_session(sid, kb=kb)
    assert n == 2
    assert kb.count() == 2


def test_ingest_is_idempotent():
    """Re-ingesting the same session should NOT double-count chunks —
    the implementation removes prior chunks for that source first."""
    sid = sessions.new_session_id()
    _write_session(sid, [HumanMessage(content="ping"), AIMessage(content="pong")])
    kb = sessions_kb.open_sessions_kb(embedder=HashEmbedder())
    sessions_kb.ingest_session(sid, kb=kb)
    sessions_kb.ingest_session(sid, kb=kb)
    assert kb.count() == 2


def test_ingest_session_missing_returns_zero():
    kb = sessions_kb.open_sessions_kb(embedder=HashEmbedder())
    assert sessions_kb.ingest_session("does-not-exist", kb=kb) == 0


def test_ingest_all_walks_directory():
    a = sessions.new_session_id()
    _write_session(a, [HumanMessage(content="first")])
    # Force a unique second id (timestamp resolution is seconds)
    import time
    time.sleep(1.1)
    b = sessions.new_session_id()
    _write_session(b, [AIMessage(content="second")])

    kb = sessions_kb.open_sessions_kb(embedder=HashEmbedder())
    res = sessions_kb.ingest_all(kb=kb)
    assert res["sessions"] == 2
    assert res["chunks"] == 2


def test_ingested_chunks_carry_role_and_msg_index():
    sid = sessions.new_session_id()
    _write_session(sid, [
        HumanMessage(content="question?"),
        AIMessage(content="answer."),
    ])
    kb = sessions_kb.open_sessions_kb(embedder=HashEmbedder())
    sessions_kb.ingest_session(sid, kb=kb)
    hit = kb.search("question", k=1)[0]
    assert hit.chunk.metadata["role"] == "user"
    assert hit.chunk.metadata["msg_index"] == 0


def test_ingested_chunks_carry_project_path():
    sid = sessions.new_session_id()
    _write_session(sid, [HumanMessage(content="hello")])
    # confirm meta has project_path
    assert sessions.get_session_meta(sid).get("project_path") == sessions.current_project_path()
    kb = sessions_kb.open_sessions_kb(embedder=HashEmbedder())
    sessions_kb.ingest_session(sid, kb=kb)
    hit = kb.search("hello", k=1)[0]
    assert hit.chunk.metadata["project_path"] == sessions.current_project_path()


# ── ✂️ chunking ──────────────────────────────────────────────────────


def test_split_text_keeps_short_intact():
    assert sessions_kb.split_text("hello world") == ["hello world"]


def test_split_text_chunks_long_message():
    text = "x" * 4000
    chunks = sessions_kb.split_text(text, limit=1000, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    # Overlap is honored — successive chunks share the last `overlap` chars
    for a, b in zip(chunks, chunks[1:]):
        assert a[-100:] == b[:100]


# ── 🔍 search wrappers ───────────────────────────────────────────────


def test_search_sessions_returns_hits():
    sid = sessions.new_session_id()
    _write_session(sid, [
        HumanMessage(content="how do I refactor an auth module"),
        AIMessage(content="extract the token validation into a separate function"),
    ])
    kb = sessions_kb.open_sessions_kb(embedder=HashEmbedder())
    sessions_kb.ingest_session(sid, kb=kb)
    hits = sessions_kb.search_sessions(
        "how do I refactor an auth module", k=2, kb=kb,
    )
    assert hits
    assert hits[0].chunk.source_id == sid


def test_search_sessions_filters_by_project(tmp_path, monkeypatch):
    """Search results from project A should be excluded when filtering
    for project B."""
    sid_a = sessions.new_session_id()
    _write_session(sid_a, [HumanMessage(content="topic alpha")])

    # Switch to project B
    proj_b = tmp_path / "project_b"
    proj_b.mkdir()
    monkeypatch.chdir(proj_b)
    sid_b = sessions.new_session_id()
    _write_session(sid_b, [HumanMessage(content="topic alpha")])

    kb = sessions_kb.open_sessions_kb(embedder=HashEmbedder())
    sessions_kb.ingest_all(kb=kb)

    proj_b_path = sessions.current_project_path()
    hits = sessions_kb.search_sessions(
        "topic alpha", k=5, kb=kb, project_path=proj_b_path,
    )
    # Only sid_b should remain
    source_ids = {h.chunk.source_id for h in hits}
    assert source_ids == {sid_b}


def test_aggregate_to_sessions_picks_best_chunk_per_session():
    from talos.memory.knowledge import Chunk, Hit
    hits = [
        Hit(chunk=Chunk(text="a", source_id="X", metadata={"role": "user"}), score=0.5),
        Hit(chunk=Chunk(text="b", source_id="X", metadata={"role": "ai"}), score=0.2),
        Hit(chunk=Chunk(text="c", source_id="Y", metadata={"role": "user"}), score=0.4),
    ]
    rolled = sessions_kb.aggregate_to_sessions(hits)
    assert [r["session_id"] for r in rolled] == ["X", "Y"]
    assert rolled[0]["score"] == 0.2  # X's best
