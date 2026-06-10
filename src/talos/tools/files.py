"""📁 File tools — read, write, edit, list, glob, grep.

Design notes (mirrors what bigger agents do):

- Paths resolve relative to the **current working directory**, so the agent
  works on whatever project you launched it from.
- Outputs are truncated so a huge file can't blow up the context window.
- ``edit_file`` requires the old text to appear **exactly once** — this
  forces the model to pick an unambiguous anchor instead of clobbering code.
"""

import re
from pathlib import Path

from langchain_core.tools import tool

MAX_OUTPUT_CHARS = 8_000


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated, {len(text) - limit} more chars]"


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


@tool
def read_file(path: str, offset: int = 0, limit: int = 500) -> str:
    """Read a text file. Returns numbered lines from `offset`, up to `limit` lines."""
    p = _resolve(path)
    if not p.is_file():
        return f"Error: {path} is not a file"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    window = lines[offset : offset + limit]
    body = "\n".join(f"{offset + i + 1:5d}│{line}" for i, line in enumerate(window))
    note = f"\n… [{len(lines)} lines total]" if len(lines) > offset + limit else ""
    return _truncate(body + note)


@tool
def write_file(path: str, content: str) -> str:
    """Create or overwrite a file with the given content."""
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {p}"


@tool
def edit_file(path: str, old_text: str, new_text: str) -> str:
    """Replace `old_text` with `new_text` in a file. `old_text` must appear exactly once."""
    p = _resolve(path)
    if not p.is_file():
        return f"Error: {path} is not a file"
    content = p.read_text(encoding="utf-8")
    count = content.count(old_text)
    if count == 0:
        return "Error: old_text not found — read the file and copy it exactly"
    if count > 1:
        return f"Error: old_text appears {count} times — add surrounding context to make it unique"
    p.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
    return f"Edited {p}"


@tool
def list_dir(path: str = ".") -> str:
    """List a directory: directories get a trailing /, files show their size."""
    p = _resolve(path)
    if not p.is_dir():
        return f"Error: {path} is not a directory"
    entries = []
    for child in sorted(p.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
        if child.is_dir():
            entries.append(f"{child.name}/")
        else:
            entries.append(f"{child.name}  ({child.stat().st_size:,} B)")
    return _truncate("\n".join(entries) or "(empty)")


@tool
def glob_files(pattern: str, path: str = ".") -> str:
    """Find files matching a glob pattern, e.g. '**/*.py'."""
    p = _resolve(path)
    matches = sorted(str(m.relative_to(p)) for m in p.glob(pattern) if m.is_file())
    return _truncate("\n".join(matches[:500]) or "(no matches)")


@tool
def grep(pattern: str, path: str = ".", glob: str = "**/*") -> str:
    """Search file contents with a regex. Returns file:line:match lines."""
    p = _resolve(path)
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"Error: bad regex: {exc}"
    hits: list[str] = []
    for f in sorted(p.glob(glob)):
        if not f.is_file() or ".git" in f.parts or ".venv" in f.parts:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue  # skip binary/unreadable files
        for lineno, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{f.relative_to(p)}:{lineno}:{line.strip()[:200]}")
                if len(hits) >= 200:
                    return _truncate("\n".join(hits) + "\n… [stopped at 200 hits]")
    return _truncate("\n".join(hits) or "(no matches)")
