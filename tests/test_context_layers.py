"""Tests for rules, memory, and sessions (M8)."""

from langchain_core.messages import AIMessage, HumanMessage

from talos.context import build_system_prompt
from talos.memory import append_memory, load_memory
from talos.sessions import latest_session_id, list_sessions, load_session, save_session


def test_rules_file_lands_in_system_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "TALOS.md").write_text("Always answer in haiku.", encoding="utf-8")
    prompt = build_system_prompt()
    assert "Always answer in haiku." in prompt
    assert "Rules" in prompt


def test_memory_roundtrip_and_prompt_injection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_memory() == ""
    append_memory("user prefers tabs")
    assert "user prefers tabs" in load_memory()
    assert "user prefers tabs" in build_system_prompt()


def test_session_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
    save_session("20260101-000000", msgs)
    loaded = load_session("20260101-000000")
    assert [m.content for m in loaded] == ["hi", "hello"]
    assert latest_session_id() == "20260101-000000"
    assert list_sessions()[0]["messages"] == 2
