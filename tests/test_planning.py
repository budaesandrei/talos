"""Tests for /plan (M24)."""

from talos.commands import dispatch
from talos.planning import READY_MARKER, construct_prompt, is_ready, save_plan


def test_dispatch_routes_plan_with_args():
    assert dispatch("/plan add user auth") == ("plan", "add user auth")
    assert dispatch("/plan") == ("plan", "")


def test_ready_detection():
    assert is_ready(f"# Plan: x\n…\n{READY_MARKER}")
    assert not is_ready("Which database do you use?")


def test_save_strips_marker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = save_plan(f"# Plan: demo\n## Intent\nstuff\n{READY_MARKER}")
    text = path.read_text(encoding="utf-8")
    assert "# Plan: demo" in text and READY_MARKER not in text


def test_construct_prompt_demands_acceptance_checks():
    out = construct_prompt("# Plan: x")
    assert "unit of work" in out.lower() and "acceptance criteria" in out.lower()
