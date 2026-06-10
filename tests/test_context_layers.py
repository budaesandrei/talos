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


async def test_api_failure_does_not_kill_the_session(tmp_path, monkeypatch):
    """An exploding LLM call must save history so -r latest can resume."""
    from langchain_core.language_models.chat_models import BaseChatModel

    from talos.runtime import runner

    class ExplodingModel(BaseChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("401 Unauthorized — api key expired")

        def bind_tools(self, tools, **kwargs):
            return self

        @property
        def _llm_type(self):
            return "exploding"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "build_llm", lambda model=None: ExplodingModel())

    rt = runner.Runtime(interactive=False)
    await rt.turn("hello?")  # must NOT raise

    from talos.sessions import latest_session_id, load_session

    saved = load_session(latest_session_id())
    assert saved[-1].content == "hello?"  # history survived the crash
