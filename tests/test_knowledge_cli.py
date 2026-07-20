"""Tests for /knowledge — user-set KBs (M63)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from talos.lifecycle import knowledge_cli as kc
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
    yield
    embeddings.reset_default_embedder()


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ── 📁 file discovery ────────────────────────────────────────────────


def test_discover_files_picks_supported_extensions(tmp_path):
    root = tmp_path / "src"
    _write(root / "main.py", "x=1")
    _write(root / "doc.md", "# title")
    _write(root / "image.png", "binary")  # unsupported
    _write(root / "sub" / "more.py", "y=2")
    files = kc.discover_files(root)
    names = {f.name for f in files}
    assert names == {"main.py", "doc.md", "more.py"}


def test_discover_files_honors_include_patterns(tmp_path):
    root = tmp_path / "x"
    _write(root / "a.py", "")
    _write(root / "b.md", "")
    files = kc.discover_files(root, include=["**/*.py"])
    assert [f.name for f in files] == ["a.py"]


def test_discover_files_honors_exclude_patterns(tmp_path):
    root = tmp_path / "x"
    _write(root / "keep.py", "")
    _write(root / "ignore" / "drop.py", "")
    files = kc.discover_files(root, exclude=["ignore/**"])
    names = {f.name for f in files}
    assert names == {"keep.py"}


def test_discover_files_picks_special_basenames(tmp_path):
    """Dockerfile, Makefile, README etc. have no extension but should
    still be indexed (the kiro convention)."""
    root = tmp_path / "x"
    _write(root / "Dockerfile", "FROM alpine")
    _write(root / "README", "hi")
    files = kc.discover_files(root)
    names = {f.name for f in files}
    assert names == {"Dockerfile", "README"}


def test_discover_files_single_file_returns_it(tmp_path):
    f = tmp_path / "x.py"
    _write(f, "x")
    assert kc.discover_files(f) == [f]


def test_discover_files_unsupported_single_file_returns_empty(tmp_path):
    f = tmp_path / "x.png"
    _write(f, "")
    assert kc.discover_files(f) == []


# ── 🗂 add_kb / list_user_kbs / remove_kb ────────────────────────────


def test_add_kb_creates_kb_and_indexes_chunks(tmp_path):
    docs = tmp_path / "docs"
    _write(docs / "a.md", "alpha content")
    _write(docs / "b.md", "bravo content")
    kb, n = kc.add_kb(name="docs", path=docs)
    assert n == 2  # one chunk per short file
    assert kb.count() == 2


def test_add_kb_extending_same_name_appends_source(tmp_path):
    """Adding a new source to a KB with the same name doesn't recreate;
    it appends to the manifest."""
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    _write(d1 / "a.md", "alpha")
    _write(d2 / "b.md", "bravo")
    kb, _ = kc.add_kb(name="combined", path=d1)
    kb2, _ = kc.add_kb(name="combined", path=d2)
    assert kb2.meta.kb_id == kb.meta.kb_id
    manifest = kc.load_manifest(kb.dir)
    assert len(manifest.sources) == 2


def test_add_kb_idempotent_on_same_source(tmp_path):
    """Re-adding the same path drops prior chunks for those files before
    re-adding, so count stays the same."""
    docs = tmp_path / "docs"
    _write(docs / "a.md", "alpha")
    kb1, _ = kc.add_kb(name="x", path=docs)
    kb2, _ = kc.add_kb(name="x", path=docs)
    assert kb2.count() == 1


def test_add_kb_no_supported_files_returns_zero(tmp_path):
    d = tmp_path / "empty"
    _write(d / "image.png", "")
    kb, n = kc.add_kb(name="img", path=d)
    assert n == 0


def test_list_user_kbs_excludes_sessions_kb(tmp_path):
    """The SessionsKB lives at ~/.talos/kb/sessions/, not under user/.
    list_user_kbs must NOT include it."""
    from talos.memory.sessions_kb import open_sessions_kb
    open_sessions_kb()  # creates ~/.talos/kb/sessions/<id>/
    # User KB
    d = tmp_path / "d"
    _write(d / "x.md", "anything")
    kc.add_kb(name="user1", path=d)
    metas = kc.list_user_kbs()
    names = [m.name for m in metas]
    assert names == ["user1"]


def test_remove_kb_by_name(tmp_path):
    d = tmp_path / "d"
    _write(d / "x.md", "")
    kc.add_kb(name="goner", path=d)
    assert kc.remove_kb(kb_id_or_name="goner") is True
    assert kc.list_user_kbs() == []
    # Idempotent
    assert kc.remove_kb(kb_id_or_name="goner") is False


def test_remove_kb_by_id(tmp_path):
    d = tmp_path / "d"
    _write(d / "x.md", "")
    kb, _ = kc.add_kb(name="goner", path=d)
    assert kc.remove_kb(kb_id_or_name=kb.meta.kb_id) is True


def test_clear_all_kbs(tmp_path):
    d1 = tmp_path / "d1"; d2 = tmp_path / "d2"
    _write(d1 / "a.md", ""); _write(d2 / "b.md", "")
    kc.add_kb(name="a", path=d1)
    kc.add_kb(name="b", path=d2)
    assert kc.clear_all_kbs() == 2
    assert kc.list_user_kbs() == []


# ── ♻️ update_kb ─────────────────────────────────────────────────────


def test_update_kb_re_indexes_changed_files(tmp_path):
    d = tmp_path / "d"
    _write(d / "x.md", "original")
    kb, _ = kc.add_kb(name="updating", path=d)
    hits_before = kb.search("original", k=1)
    assert hits_before and "original" in hits_before[0].chunk.text

    # Change the file
    _write(d / "x.md", "edited content here")
    res = kc.update_kb(kb_id_or_name="updating")
    assert res["sources"] >= 1
    assert res["chunks"] >= 1
    # New content is findable; old content is gone (idempotent re-add
    # removed the prior chunk for x.md)
    hits_after = kb.search("edited content here", k=1)
    assert hits_after and "edited" in hits_after[0].chunk.text


def test_update_kb_missing_returns_error():
    res = kc.update_kb(kb_id_or_name="does-not-exist")
    assert "error" in res


# ── 🔍 search_user_kbs ────────────────────────────────────────────────


def test_search_returns_ranked_hits(tmp_path):
    d = tmp_path / "d"
    _write(d / "auth.md", "auth refactor token validation")
    _write(d / "billing.md", "invoice generation pipeline")
    kc.add_kb(name="docs", path=d)
    hits = kc.search_user_kbs("auth refactor token validation", k=2)
    assert hits
    assert "auth" in hits[0]["snippet"]


def test_search_filtered_to_single_kb(tmp_path):
    d1 = tmp_path / "d1"; d2 = tmp_path / "d2"
    _write(d1 / "alpha.md", "alpha")
    _write(d2 / "alpha.md", "alpha")
    kc.add_kb(name="kb1", path=d1)
    kc.add_kb(name="kb2", path=d2)
    hits = kc.search_user_kbs("alpha", k=10, kb_id_or_name="kb2")
    assert all(h["kb_name"] == "kb2" for h in hits)


# ── 🤖 agent tools ───────────────────────────────────────────────────


def test_recall_knowledge_tool(tmp_path):
    from talos.tools.knowledge_tool import recall_knowledge
    d = tmp_path / "d"
    _write(d / "doc.md", "important content")
    kc.add_kb(name="tooltest", path=d)
    out = recall_knowledge.invoke({"query": "important content", "k": 1})
    parsed = json.loads(out)
    assert parsed and parsed[0]["kb_name"] == "tooltest"


def test_list_kbs_tool(tmp_path):
    from talos.tools.knowledge_tool import list_kbs_tool
    d = tmp_path / "d"; _write(d / "x.md", "")
    kc.add_kb(name="visible", path=d)
    out = list_kbs_tool.invoke({})
    assert "visible" in out


def test_recall_empty_query_errors(tmp_path):
    from talos.tools.knowledge_tool import recall_knowledge
    assert recall_knowledge.invoke({"query": ""}).startswith("Error")


def test_add_kb_tool_writes(tmp_path):
    from talos.tools.knowledge_tool import add_kb_tool
    d = tmp_path / "d"
    _write(d / "doc.md", "stuff")
    out = add_kb_tool.invoke({"name": "agent-set", "path": str(d)})
    assert "added" in out
    assert "agent-set" in out
    # And it actually landed
    metas = kc.list_user_kbs()
    assert any(m.name == "agent-set" for m in metas)


def test_remove_kb_tool(tmp_path):
    from talos.tools.knowledge_tool import remove_kb_tool
    d = tmp_path / "d"; _write(d / "x.md", "")
    kc.add_kb(name="bye", path=d)
    out = remove_kb_tool.invoke({"kb_id_or_name": "bye"})
    assert "removed" in out


# ── ⌨️ dispatch + /knowledge ─────────────────────────────────────────


def test_knowledge_in_help_text():
    from talos.ui.commands import help_text, BUILTINS
    assert "/knowledge" in BUILTINS
    assert "/knowledge" in help_text()


def test_knowledge_dispatches_as_builtin():
    from talos.ui.commands import dispatch
    assert dispatch("/knowledge") == ("builtin", "/knowledge")
