"""Tests for the self-edit sandbox (M53).

The orchestrator's five boundaries (worktree create/cleanup, sub-agent,
diff, tests) are all injected — these tests stub every one of them.
No real git, no real subprocess, no LLM. The point is to exercise the
*orchestration* (cleanup runs even on failure, diff=empty is handled,
sub-agent crashes turn into recorded errors not test failures) rather
than the git/subprocess plumbing.

The default git-backed impls are tested separately, lightly — just that
they call into git correctly without actually creating worktrees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from talos.lifecycle import self_edit as se


# ── 🎭 fakes ───────────────────────────────────────────────────────────


def _fake_worktree_factory(tmp_path: Path):
    """Returns a stub create-fn that builds a directory inside tmp_path."""
    created = []
    cleaned = []

    def create(branch: str) -> Path:
        d = tmp_path / "worktrees" / branch.replace("/", "-")
        d.mkdir(parents=True)
        created.append(d)
        return d

    def cleanup(worktree: Path) -> None:
        cleaned.append(worktree)

    return create, cleanup, created, cleaned


def _make_diff_fn(diff_text: str = "", files=()):
    """A diff stub that returns whatever you tell it to."""
    def diff(_w: Path):
        return diff_text, list(files)
    return diff


def _make_test_fn(passed: bool = True, output: str = "ok"):
    def test(_w: Path):
        return passed, output
    return test


# ── 🏃 orchestration ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_writes_candidate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create, cleanup, created, cleaned = _fake_worktree_factory(tmp_path)

    def sub_agent(worktree: Path, request: str) -> str:
        # Sub-agent "edits" by writing a stub file inside the worktree.
        (worktree / "foo.py").write_text("x = 1\n")
        return "wrote foo.py"

    cand = await se.run_self_edit(
        "add a foo command",
        create_worktree_fn=create,
        cleanup_worktree_fn=cleanup,
        sub_agent_fn=sub_agent,
        diff_fn=_make_diff_fn("+x = 1", ["foo.py"]),
        test_fn=_make_test_fn(passed=True, output="1 passed"),
    )
    assert cand.test_passed is True
    assert cand.files_changed == ["foo.py"]
    assert "+x = 1" in cand.diff
    assert cand.sub_agent_error is None
    # Cleanup ran exactly once on the created worktree
    assert created == cleaned

    # Persisted artifacts
    d = se.candidate_dir(cand.edit_id)
    assert (d / "candidate.json").is_file()
    assert (d / "diff.patch").read_text() == "+x = 1"
    assert (d / "request.md").read_text().strip() == "add a foo command"
    assert "1 passed" in (d / "test_output.txt").read_text()


@pytest.mark.asyncio
async def test_subagent_crash_is_captured_not_raised(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create, cleanup, _, cleaned = _fake_worktree_factory(tmp_path)

    def sub_agent(_w: Path, _r: str) -> str:
        raise RuntimeError("boom")

    cand = await se.run_self_edit(
        "do a thing",
        create_worktree_fn=create,
        cleanup_worktree_fn=cleanup,
        sub_agent_fn=sub_agent,
        diff_fn=_make_diff_fn("", []),
        test_fn=_make_test_fn(),
    )
    assert "boom" in (cand.sub_agent_error or "")
    assert cand.files_changed == []
    # Cleanup still ran — that's the whole point of the try/finally.
    assert cleaned, "cleanup should run even when sub-agent crashes"


@pytest.mark.asyncio
async def test_empty_diff_skips_tests(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create, cleanup, _, _ = _fake_worktree_factory(tmp_path)
    test_calls = []

    def test_fn(_w: Path):
        test_calls.append(True)
        return True, "shouldn't run"

    cand = await se.run_self_edit(
        "no-op",
        create_worktree_fn=create,
        cleanup_worktree_fn=cleanup,
        sub_agent_fn=lambda _w, _r: "did nothing",
        diff_fn=_make_diff_fn("", []),
        test_fn=test_fn,
    )
    assert cand.files_changed == []
    assert cand.test_passed is False  # default for empty diff
    assert test_calls == []  # the test-fn should NOT have been called


@pytest.mark.asyncio
async def test_skip_tests_flag_skips_runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create, cleanup, _, _ = _fake_worktree_factory(tmp_path)
    test_calls = []

    def test_fn(_w: Path):
        test_calls.append(True)
        return False, "should not run"

    cand = await se.run_self_edit(
        "edit",
        create_worktree_fn=create,
        cleanup_worktree_fn=cleanup,
        sub_agent_fn=lambda w, _r: (w / "f.py").write_text("y = 2\n") or "did",
        diff_fn=_make_diff_fn("+y = 2", ["f.py"]),
        test_fn=test_fn,
        skip_tests=True,
    )
    assert cand.test_passed is False
    assert test_calls == []
    assert "skipped" in cand.test_output.lower()


@pytest.mark.asyncio
async def test_keep_worktree_skips_cleanup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create, cleanup, _, cleaned = _fake_worktree_factory(tmp_path)

    await se.run_self_edit(
        "edit",
        create_worktree_fn=create,
        cleanup_worktree_fn=cleanup,
        sub_agent_fn=lambda _w, _r: "ok",
        diff_fn=_make_diff_fn("+", ["f.py"]),
        test_fn=_make_test_fn(),
        keep_worktree=True,
    )
    assert cleaned == [], "cleanup should not run with keep_worktree=True"


@pytest.mark.asyncio
async def test_failing_tests_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create, cleanup, _, _ = _fake_worktree_factory(tmp_path)
    cand = await se.run_self_edit(
        "buggy edit",
        create_worktree_fn=create,
        cleanup_worktree_fn=cleanup,
        sub_agent_fn=lambda w, _r: (w / "x.py").write_text("oops\n") or "",
        diff_fn=_make_diff_fn("+oops", ["x.py"]),
        test_fn=_make_test_fn(passed=False, output="3 failed"),
    )
    assert cand.test_passed is False
    assert "3 failed" in cand.test_output


# ── 📋 candidate storage ──────────────────────────────────────────────


def test_save_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = se.SelfEditCandidate(
        edit_id="20260616-141500-test", branch="self-edits/test",
        request="add x", diff="+x", files_changed=["x.py"],
        test_passed=True, test_output="ok",
    )
    se.save_candidate(c)
    loaded = se.load_candidate(c.edit_id)
    assert loaded.request == "add x"
    assert loaded.diff == "+x"
    assert loaded.files_changed == ["x.py"]
    assert loaded.test_passed is True


def test_list_candidates_newest_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("20260615-100000-a", "20260616-100000-b", "20260614-100000-c"):
        c = se.SelfEditCandidate(
            edit_id=name, branch=f"self-edits/{name}",
            request=name, diff="", files_changed=[],
        )
        se.save_candidate(c)
    out = se.list_candidates()
    assert [c.edit_id for c in out] == [
        "20260616-100000-b", "20260615-100000-a", "20260614-100000-c",
    ]


# ── ✨ small helpers ──────────────────────────────────────────────────


def test_make_edit_id_includes_request_slug():
    eid = se.make_edit_id("add a foo command quickly")
    # ts-prefix, slug suffix
    assert "-add-a-foo-command" in eid
    # 8-char date + dash + 6-char time + dash + slug
    head = eid[:15]
    assert head[8] == "-" and head[:8].isdigit() and head[9:].isdigit()
