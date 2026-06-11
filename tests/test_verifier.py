"""Tests for the plan verifier (M36)."""

from talos.lifecycle.planning import parse_verdict


def test_parse_clean_verdict():
    raw = '{"units":[{"name":"auth","passed":true,"evidence":"login works"}],"all_passed":true}'
    v = parse_verdict(raw)
    assert v["all_passed"] and v["units"][0]["passed"]


def test_parse_chatty_verdict():
    raw = 'Sure! Here is the result:\n{"units":[],"all_passed":false}\nHope that helps'
    v = parse_verdict(raw)
    assert v["all_passed"] is False


def test_parse_garbage_is_safe():
    v = parse_verdict("no json here")
    assert v == {"units": [], "all_passed": False}


async def test_verify_plan_runs_judge(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage

    from talos.agent import runtime as runner
    from tests.fakes import FakeToolCallingModel

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        runner, "build_llm",
        lambda model=None: FakeToolCallingModel(responses=[AIMessage(
            content='{"units":[{"name":"x","passed":true,"evidence":"done"}],"all_passed":true}')]),
    )
    rt = runner.Runtime(model="mock", interactive=False)
    rt.messages = [HumanMessage(content="did the thing"), AIMessage(content="done")]
    verdict = await rt.verify_plan("# Plan: x\n## UoW 1")
    assert verdict["all_passed"]
