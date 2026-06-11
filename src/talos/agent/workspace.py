"""🗂️ Workspace awareness — orient Talos the moment it launches.

Most agents are *lazily* workspace-aware: they only learn the project by
exploring with tools when a question forces it. That's fine, but the
polished agents (Claude Code, Kiro) inject a cheap snapshot up front so
"what is this project?" is answerable instantly without a tool round trip.

``snapshot()`` produces ~200 tokens: the top-level tree, git branch + last
commit, and the README's first lines. It's added as a context layer so
it's always in the system prompt — current as of session start.

``/init`` goes further (Claude Code's pattern): it surveys the repo and
writes a starter TALOS.md rules file, giving the workspace *persistent*
awareness across sessions.
"""

import subprocess
from pathlib import Path

IGNORE = {".git", ".venv", "node_modules", "__pycache__", ".talos", "dist", "build"}


def _git(args: list[str]) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _tree(root: Path, max_entries: int = 30) -> str:
    entries = []
    for child in sorted(root.iterdir(), key=lambda c: (c.is_file(), c.name)):
        if child.name in IGNORE or child.name.startswith("."):
            continue
        entries.append(child.name + ("/" if child.is_dir() else ""))
        if len(entries) >= max_entries:
            entries.append("…")
            break
    return "  ".join(entries)


def _readme_head(root: Path, lines: int = 8) -> str:
    for name in ("README.md", "README.rst", "README.txt", "readme.md"):
        p = root / name
        if p.is_file():
            head = p.read_text(encoding="utf-8", errors="replace").splitlines()[:lines]
            return "\n".join(head)
    return ""


def snapshot() -> str:
    """A cheap workspace briefing for the system prompt."""
    root = Path.cwd()
    parts = [f"## Workspace ({root.name})"]

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch:
        last = _git(["log", "-1", "--pretty=%s (%cr)"])
        parts.append(f"- git: {branch} — {last}")
        dirty = _git(["status", "--porcelain"])
        if dirty:
            parts.append(f"- uncommitted changes in {len(dirty.splitlines())} file(s)")

    tree = _tree(root)
    if tree:
        parts.append(f"- top level: {tree}")

    readme = _readme_head(root)
    if readme:
        parts.append("- README (head):\n" + readme)

    return "\n".join(parts)


INIT_PROMPT = """Survey this project (read the README, key config files, and
representative source) and write a TALOS.md that will orient any agent
working here in future sessions. Include: what the project is, its stack,
how to run/test it, important conventions, and any 'never do this' rules
you can infer. Keep it tight — a page at most. Output ONLY the TALOS.md
content, no preamble."""
