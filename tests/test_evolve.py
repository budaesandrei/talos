"""Tests for the /evolve lifecycle layer (M44)."""

from talos.lifecycle import evolve
from talos.ui.commands import dispatch


def test_dispatch_routes_evolve():
    assert dispatch("/evolve focus on perf") == ("evolve", "focus on perf")
    assert dispatch("/evolve") == ("evolve", "")


def test_phase_ready_markers():
    assert evolve.is_debt_ready("…\nDEBT REPORT READY")
    assert not evolve.is_debt_ready("still working")
    assert evolve.is_requirements_ready("…\nREQUIREMENTS READY")


def test_all_personas_have_prompts():
    for hat in evolve.PERSONAS:
        p = evolve.research_prompt(hat)
        assert hat in p and "web_fetch" in p          # grounded, not imagined
        assert "Do NOT invent" in p


def test_unknown_hat_is_safe():
    assert "a user" in evolve.research_prompt("nonexistent-hat")
