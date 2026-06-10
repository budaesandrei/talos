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


async def test_usage_is_tracked_per_turn_and_session(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage

    from talos.runtime import runner
    from tests.fakes import FakeToolCallingModel

    monkeypatch.chdir(tmp_path)
    reply = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    )
    monkeypatch.setattr(
        runner, "build_llm", lambda model=None: FakeToolCallingModel(responses=[reply])
    )
    rt = runner.Runtime(interactive=False)
    await rt.turn("hello")
    assert rt.usage["input"] == 100
    assert rt.usage["total"] == 120
    assert rt.usage["turns"] == 1


async def test_session_gets_llm_title_and_usage_persists(tmp_path, monkeypatch):
    import asyncio

    from langchain_core.messages import AIMessage

    from talos.runtime import runner
    from talos.sessions import all_time_usage, get_session_meta, list_sessions
    from tests.fakes import FakeToolCallingModel

    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    def fake_llm(model=None):
        # 1st build_llm → the conversation model; later ones → title model.
        # (pydantic copies the responses list, so they can't share one.)
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeToolCallingModel(responses=[
                AIMessage(content="hi", usage_metadata={
                    "input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
            ])
        return FakeToolCallingModel(responses=[AIMessage(content="rename files project")])

    monkeypatch.setattr(runner, "build_llm", fake_llm)
    rt = runner.Runtime(interactive=False)
    await rt.turn("help me rename my files")
    await asyncio.sleep(0.05)  # let the fire-and-forget title task finish

    meta = get_session_meta(rt.session_id)
    assert meta["title"] == "rename files project"
    assert meta["usage"]["total"] == 15
    assert list_sessions()[0]["title"] == "rename files project"
    assert all_time_usage()["total"] == 15


def test_reasoning_effort_only_sent_when_configured(monkeypatch):
    from talos import llm as llm_mod

    monkeypatch.setattr(llm_mod.settings, "reasoning_effort", None)
    assert llm_mod.build_llm().reasoning_effort is None

    monkeypatch.setattr(llm_mod.settings, "reasoning_effort", "high")
    assert llm_mod.build_llm().reasoning_effort == "high"
