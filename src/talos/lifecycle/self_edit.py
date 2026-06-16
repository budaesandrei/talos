"""🔧 Self-edit — Talos proposes changes to its own source, safely.

Spawns a *sub-agent* in an isolated git worktree, lets it satisfy a
natural-language edit request against a copy of the source, captures
the diff, runs the test suite, and persists everything to
``.talos/self-edits/<id>/`` for human review.

The host process never touches its own source. The sub-agent can do
anything it likes inside the worktree — the worst case is a worktree
that gets deleted. The diff is the deliverable; applying it (next
milestone) is a separate human-gated step.

Five injected boundaries — everything that touches the filesystem or
spawns a subprocess is a callable, so offline tests can stub it out:

* ``create_worktree_fn(branch) -> Path`` — make an isolated checkout
* ``sub_agent_fn(worktree, request)`` — run the editor (default: a
  ``python -m talos run`` subprocess with cwd=worktree)
* ``diff_fn(worktree) -> (diff_text, files_changed)``
* ``test_fn(worktree) -> (passed, output)``
* ``cleanup_worktree_fn(worktree)``

The whole flow is one async function so it composes with the existing
asyncio runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

# ── ✨ tiny helpers (kept local; nothing exotic) ───────────────────────


def slugify(text: str, max_words: int = 5) -> str:
    """Reuse the scheduling slugify rules to make a stable edit id."""
    from talos.lifecycle.scheduling import slugify as _slug
    return _slug(text, max_words=max_words)


# ── 📋 the candidate ───────────────────────────────────────────────────


@dataclass
class SelfEditCandidate:
    """One proposed self-edit — the unit of work the human reviews."""

    edit_id: str
    branch: str
    request: str
    diff: str
    files_changed: list[str] = field(default_factory=list)
    test_passed: bool = False
    test_output: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    sub_agent_error: str | None = None
    # Set in M54 — kept here so the persisted JSON shape is forward-compatible.
    verifier_verdict: dict | None = None
    protected_violations: list[str] = field(default_factory=list)
    applied: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


# ── 🗂️ storage ─────────────────────────────────────────────────────────


def candidates_dir() -> Path:
    return Path(".talos") / "self-edits"


def candidate_dir(edit_id: str) -> Path:
    return candidates_dir() / edit_id


def save_candidate(c: SelfEditCandidate) -> Path:
    d = candidate_dir(c.edit_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "candidate.json").write_text(c.to_json(), encoding="utf-8")
    (d / "request.md").write_text(c.request.strip() + "\n", encoding="utf-8")
    (d / "diff.patch").write_text(c.diff or "", encoding="utf-8")
    (d / "test_output.txt").write_text(c.test_output or "", encoding="utf-8")
    return d


def load_candidate(edit_id: str) -> SelfEditCandidate | None:
    f = candidate_dir(edit_id) / "candidate.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return SelfEditCandidate(**data)


def list_candidates() -> list[SelfEditCandidate]:
    base = candidates_dir()
    if not base.is_dir():
        return []
    out = []
    for d in sorted(base.iterdir(), reverse=True):
        if d.is_dir():
            c = load_candidate(d.name)
            if c is not None:
                out.append(c)
    return out


# ── 🌿 default git worktree manager ────────────────────────────────────


def _git(repo: Path, *args: str, check: bool = True,
         capture: bool = False) -> subprocess.CompletedProcess:
    """Tiny ``git -C <repo> ...`` wrapper. Centralized so failures get
    a uniform error message."""
    cmd = ["git", "-C", str(repo), *args]
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def repo_root_of(start: Path | None = None) -> Path:
    """Find the git repo root from a starting path (or cwd)."""
    p = (start or Path.cwd()).resolve()
    res = subprocess.run(
        ["git", "-C", str(p), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            "self-edit needs a git repo — run from inside the talos checkout"
        )
    return Path(res.stdout.strip())


def default_create_worktree(repo: Path, branch: str,
                             base: Path | None = None) -> Path:
    """``git worktree add <tmp>/<id> -b <branch> HEAD``."""
    base = base or Path(tempfile.gettempdir()) / "talos-self-edits"
    base.mkdir(parents=True, exist_ok=True)
    dest = base / branch.replace("/", "-")
    if dest.exists():
        # leftover from a prior crashed run — remove cleanly
        default_cleanup_worktree(repo, dest)
    _git(repo, "worktree", "add", str(dest), "-b", branch, "HEAD")
    return dest


def default_cleanup_worktree(repo: Path, worktree: Path) -> None:
    """``git worktree remove --force <worktree>``. Ignores failures —
    a half-cleaned worktree is better than a crash on the cleanup."""
    if not worktree.exists():
        return
    _git(repo, "worktree", "remove", "--force", str(worktree), check=False)
    # Belt-and-suspenders for git's occasional refusal to clean a dirty tree
    if worktree.exists():
        import shutil
        shutil.rmtree(worktree, ignore_errors=True)


def default_diff(worktree: Path) -> tuple[str, list[str]]:
    """Compute the patch + file list inside the worktree.

    ``git add -A`` then ``git diff --cached`` — the staging step picks
    up new files, which a plain ``git diff`` would miss."""
    _git(worktree, "add", "-A")
    diff_proc = _git(worktree, "diff", "--cached", capture=True)
    files_proc = _git(worktree, "diff", "--cached", "--name-only", capture=True)
    files = [l for l in files_proc.stdout.strip().splitlines() if l]
    return diff_proc.stdout, files


def default_run_tests(worktree: Path, timeout: int = 180) -> tuple[bool, str]:
    """``pytest -q`` in the worktree. Returns (passed, output)."""
    cmd = [sys.executable, "-m", "pytest", "-q", "tests/"]
    try:
        res = subprocess.run(
            cmd, cwd=str(worktree),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"💥 tests timed out after {timeout}s\n{exc}"
    output = (res.stdout or "") + (res.stderr or "")
    return res.returncode == 0, output


# ── 🤖 default sub-agent: a subprocess running `talos run` ────────────


SELF_EDIT_PROMPT = """\
You are editing the Talos source code. The repository root is your
current working directory — every file path is relative to it. The
``talos/`` package lives under ``src/talos/``.

Approach:

1. Use ``list_dir``, ``glob_files``, ``read_file``, and ``grep`` to
   orient yourself. The system prompt's "Self-knowledge" section
   already lists every module's purpose.
2. Make the smallest change that satisfies the request. Match existing
   conventions — feature-based subpackages, lazy imports inside CLI
   commands, pydantic v2 models, offline tests with the fake LLM.
3. Use ``write_file`` / ``edit_file`` to apply the change.
4. Do NOT run tests yourself (a sandboxed pytest run happens after you).
5. Do NOT commit, push, or run git commands.
6. Do NOT add new dependencies without saying so explicitly in your
   final message. The reviewer needs to see them.

When the change is complete, stop and produce a short summary of what
you changed and why. That summary becomes the commit message draft.

The request follows:
"""


def default_sub_agent(worktree: Path, request: str, *,
                       model: str | None = None,
                       timeout: int = 600) -> str:
    """Spawn ``python -m talos run`` as a subprocess with cwd=worktree.

    The sub-agent runs with ``--yolo`` (no permission prompts — the
    worktree boundary IS the safety perimeter, not the per-tool gate)
    and inherits the parent's TALOS_* env vars. Returns the subprocess
    stdout for inclusion in the candidate record.
    """
    full_prompt = SELF_EDIT_PROMPT + "\n" + request.strip() + "\n"
    cmd = [sys.executable, "-m", "talos", "run", full_prompt, "--yolo"]
    if model:
        cmd.extend(["--model", model])
    env = os.environ.copy()
    # Disable the workspace snapshot in the sub-agent — it bloats the
    # context with the worktree's own state.
    env["TALOS_WORKSPACE_SNAPSHOT"] = "false"
    try:
        res = subprocess.run(
            cmd, cwd=str(worktree), env=env,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return f"💥 sub-agent timed out after {timeout}s\n{exc}"
    return (res.stdout or "") + (res.stderr or "")


# ── 🏃 the orchestrator ───────────────────────────────────────────────


def make_edit_id(request: str) -> str:
    """``20260616-141500-add-foo-cmd`` — sortable + descriptive."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = slugify(request) or "edit"
    return f"{ts}-{slug}"


async def run_self_edit(
    request: str,
    *,
    repo: Path | None = None,
    create_worktree_fn: Callable[[str], Path] | None = None,
    cleanup_worktree_fn: Callable[[Path], None] | None = None,
    sub_agent_fn: Callable[[Path, str], str] | None = None,
    diff_fn: Callable[[Path], tuple[str, list[str]]] | None = None,
    test_fn: Callable[[Path], tuple[bool, str]] | None = None,
    skip_tests: bool = False,
    log: Callable[[str], None] | None = None,
    keep_worktree: bool = False,
) -> SelfEditCandidate:
    """Run one self-edit attempt end-to-end.

    Defaults to the git-backed implementations; tests pass in stubs.
    Returns the persisted ``SelfEditCandidate`` whether the sub-agent
    succeeded, failed, or produced no diff at all — failure modes are
    interesting data, not crashes.
    """
    edit_id = make_edit_id(request)
    branch = f"self-edits/{edit_id}"
    log = log or (lambda _: None)

    if repo is None and create_worktree_fn is None:
        # Default mode binds the create/cleanup/diff/test fns to the git repo.
        repo = repo_root_of()
    if create_worktree_fn is None:
        create_worktree_fn = lambda b: default_create_worktree(repo, b)  # noqa: E731
    if cleanup_worktree_fn is None:
        cleanup_worktree_fn = lambda w: default_cleanup_worktree(repo, w)  # noqa: E731
    if sub_agent_fn is None:
        sub_agent_fn = default_sub_agent
    if diff_fn is None:
        diff_fn = default_diff
    if test_fn is None:
        test_fn = default_run_tests

    worktree: Path | None = None
    sub_agent_error: str | None = None
    sub_agent_output = ""
    diff_text = ""
    files: list[str] = []
    test_passed = False
    test_output = "(tests skipped)" if skip_tests else ""

    try:
        log(f"🌿 creating worktree {branch}")
        worktree = create_worktree_fn(branch)

        log(f"🤖 running sub-agent against {worktree}")
        try:
            # sub-agent is sync — run in a thread so we don't block the loop
            raw = await asyncio.to_thread(
                sub_agent_fn, worktree, request,
            )
            sub_agent_output = "" if raw is None else str(raw)
        except Exception as exc:  # noqa: BLE001 — the daemon must keep going
            sub_agent_error = f"{type(exc).__name__}: {exc}"
            log(f"💥 sub-agent failed: {sub_agent_error}")

        log("📐 computing diff")
        try:
            diff_text, files = diff_fn(worktree)
        except Exception as exc:  # noqa: BLE001
            sub_agent_error = (sub_agent_error or "") + (
                f"\n💥 diff failed: {type(exc).__name__}: {exc}"
            )

        if files and not skip_tests:
            log(f"🧪 running tests in worktree ({len(files)} file(s) changed)")
            try:
                test_passed, test_output = test_fn(worktree)
            except Exception as exc:  # noqa: BLE001
                test_passed = False
                test_output = f"💥 test runner failed: {type(exc).__name__}: {exc}"
        elif not files:
            log("🪞 sub-agent produced no changes")
            test_output = "(no changes; tests skipped)"

    finally:
        if worktree is not None and not keep_worktree:
            log(f"🧹 cleaning up worktree {worktree}")
            try:
                cleanup_worktree_fn(worktree)
            except Exception as exc:  # noqa: BLE001
                log(f"⚠️ cleanup failed (non-fatal): {exc}")

    candidate = SelfEditCandidate(
        edit_id=edit_id,
        branch=branch,
        request=request,
        diff=diff_text,
        files_changed=files,
        test_passed=test_passed,
        test_output=(test_output or "")
                    + ("\n\n--- sub-agent output ---\n" + sub_agent_output
                       if sub_agent_output else ""),
        sub_agent_error=sub_agent_error,
    )
    save_candidate(candidate)
    log(
        f"📝 candidate {edit_id} saved → {candidate_dir(edit_id)} "
        f"({len(files)} file(s) changed, tests {'✅' if test_passed else '❌'})"
    )
    return candidate
