"""Headless test of the Textual frontend via Textual's Pilot."""

import pytest

pytest.importorskip("textual")

from langchain_core.messages import AIMessage

from tests.fakes import FakeToolCallingModel


async def test_textual_app_runs_a_turn(tmp_path, monkeypatch):
    import talos.tui_app as tui_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        tui_app, "build_llm",
        lambda model=None: FakeToolCallingModel(responses=[
            AIMessage(content="**hello** from textual",
                      usage_metadata={"input_tokens": 7, "output_tokens": 3,
                                      "total_tokens": 10}),
        ]),
    )
    app = tui_app.TalosApp(model="mock")
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one("Input").value = "hi"
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert app.usage["total"] == 10          # sidebar data flowed
        agents = app.query(".agent")
        assert len(agents) == 1                  # one agent block rendered
        assert app.messages[-1].content == "**hello** from textual"
