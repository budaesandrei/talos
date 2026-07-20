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


# ── 🛡️ M54: protected-files allowlist ──────────────────────────────────


def test_protected_files_list_includes_critical_paths():
    """The minimum allowlist: safety machinery + self-edit code + tests."""
    must_protect = {
        "src/talos/infra/policy.py",
        "src/talos/infra/permissions.py",
        "src/talos/infra/sandbox.py",
        "src/talos/lifecycle/self_edit.py",
        "src/talos/lifecycle/self_knowledge.py",
        "src/talos/lifecycle/scheduling.py",
        "tests/test_self_edit.py",
        "tests/test_self_knowledge.py",
    }
    missing = must_protect - se.PROTECTED_FILES
    assert not missing, f"expected these in PROTECTED_FILES: {missing}"


def test_check_protected_violations_empty_when_clean():
    assert se.check_protected_violations(["src/talos/cli.py"]) == []
    assert se.check_protected_violations([]) == []


def test_check_protected_violations_returns_sorted_subset():
    files = [
        "src/talos/lifecycle/self_edit.py",
        "src/talos/cli.py",
        "src/talos/infra/policy.py",
        "src/talos/agent/runtime.py",
    ]
    violations = se.check_protected_violations(files)
    assert violations == [
        "src/talos/infra/policy.py",
        "src/talos/lifecycle/self_edit.py",
    ]


@pytest.mark.asyncio
async def test_protected_violations_recorded_on_candidate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create, cleanup, _, _ = _fake_worktree_factory(tmp_path)

    def sub_agent(worktree: Path, _r: str) -> str:
        (worktree / "x").write_text("")
        return "did the thing"

    cand = await se.run_self_edit(
        "rewrite policy.py",
        create_worktree_fn=create,
        cleanup_worktree_fn=cleanup,
        sub_agent_fn=sub_agent,
        diff_fn=_make_diff_fn(
            "+evil", ["src/talos/infra/policy.py", "src/talos/cli.py"]
        ),
        test_fn=_make_test_fn(passed=True, output="ok"),
    )
    assert cand.protected_violations == ["src/talos/infra/policy.py"]


# ── 🔍 M54: verifier ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verifier_passes_when_fake_says_approve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create, cleanup, _, _ = _fake_worktree_factory(tmp_path)

    async def fake_verifier(candidate):
        return {
            "passes_request": True,
            "evidence": "the diff added the foo command",
            "concerns": [],
            "recommendation": "approve",
        }

    cand = await se.run_self_edit(
        "add a foo command",
        create_worktree_fn=create,
        cleanup_worktree_fn=cleanup,
        sub_agent_fn=lambda w, _r: (w / "foo.py").write_text("x") or "",
        diff_fn=_make_diff_fn("+x", ["src/talos/cli.py"]),
        test_fn=_make_test_fn(passed=True, output="ok"),
        verifier_fn=fake_verifier,
    )
    assert cand.verifier_verdict["recommendation"] == "approve"


@pytest.mark.asyncio
async def test_verifier_skipped_when_no_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create, cleanup, _, _ = _fake_worktree_factory(tmp_path)
    verifier_calls = []

    async def fake_verifier(candidate):
        verifier_calls.append(True)
        return {"recommendation": "approve"}

    await se.run_self_edit(
        "no-op",
        create_worktree_fn=create,
        cleanup_worktree_fn=cleanup,
        sub_agent_fn=lambda _w, _r: "did nothing",
        diff_fn=_make_diff_fn("", []),
        test_fn=_make_test_fn(),
        verifier_fn=fake_verifier,
    )
    assert verifier_calls == []  # no diff → no verifier call


@pytest.mark.asyncio
async def test_verifier_crash_recorded_as_revise(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create, cleanup, _, _ = _fake_worktree_factory(tmp_path)

    async def bad_verifier(_c):
        raise ConnectionError("LLM unreachable")

    cand = await se.run_self_edit(
        "edit",
        create_worktree_fn=create,
        cleanup_worktree_fn=cleanup,
        sub_agent_fn=lambda w, _r: (w / "a.py").write_text("") or "",
        diff_fn=_make_diff_fn("+", ["a.py"]),
        test_fn=_make_test_fn(),
        verifier_fn=bad_verifier,
    )
    assert cand.verifier_verdict["recommendation"] == "revise"
    assert any("verifier crashed" in c for c in cand.verifier_verdict["concerns"])


@pytest.mark.asyncio
async def test_verify_candidate_handles_chatty_llm():
    """parse_verdict needs to extract JSON from prose-wrapped replies."""
    c = se.SelfEditCandidate(
        edit_id="x", branch="y", request="add a foo command", diff="+x",
        files_changed=["a.py"], test_passed=True, test_output="ok",
    )
    async def chatty_llm(_sys, _user):
        return ("Sure! Here's my review:\n\n"
                '{"passes_request": true, "evidence": "yep",'
                ' "concerns": [], "recommendation": "approve"}\n\n'
                "Let me know if you need more!")
    verdict = await se.verify_candidate(c, chatty_llm)
    assert verdict["recommendation"] == "approve"
    assert verdict["passes_request"] is True


@pytest.mark.asyncio
async def test_verify_candidate_defaults_to_revise_on_garbage():
    c = se.SelfEditCandidate(
        edit_id="x", branch="y", request="r", diff="+x",
        files_changed=["a.py"], test_passed=True, test_output="ok",
    )
    async def garbage_llm(_sys, _user):
        return "no idea what you want"
    verdict = await se.verify_candidate(c, garbage_llm)
    assert verdict["recommendation"] == "revise"
    assert verdict["passes_request"] is False


# ── 🧩 M54: apply (refusal paths only — git ops are integration-tested) ──


def test_apply_refuses_missing_candidate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ok, msg = se.apply_candidate("does-not-exist")
    assert ok is False and "no candidate" in msg


def test_apply_refuses_empty_diff(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = se.SelfEditCandidate(
        edit_id="e", branch="b", request="r", diff="",
        files_changed=[], test_passed=True,
    )
    se.save_candidate(c)
    ok, msg = se.apply_candidate("e")
    assert ok is False and "empty" in msg.lower()


def test_apply_refuses_protected_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = se.SelfEditCandidate(
        edit_id="e", branch="b", request="r", diff="+ x",
        files_changed=["src/talos/infra/policy.py"],
        protected_violations=["src/talos/infra/policy.py"],
        test_passed=True,
    )
    se.save_candidate(c)
    ok, msg = se.apply_candidate("e", force=False)
    assert ok is False
    assert "protected" in msg.lower()
    assert "policy.py" in msg


def test_apply_refuses_already_applied(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = se.SelfEditCandidate(
        edit_id="e", branch="b", request="r", diff="+x",
        files_changed=["a.py"], test_passed=True, applied=True,
    )
    se.save_candidate(c)
    ok, msg = se.apply_candidate("e")
    assert ok is False and "already" in msg
