"""🪞 Self-knowledge — Talos understands its own source tree.

Real agents — Claude Code, Cursor, Kiro — get a lot of mileage from
knowing what they're built out of. When you ask "where would I add a
new slash command?" the agent should answer instantly without grepping,
because the layout of its own codebase is already in its system prompt.

Two surfaces:

* **Compact index** (in the system prompt every turn). One line per
  module: ``src/talos/memory/sessions.py — 💾 conversations that
  survive a restart``. Cheap (~50 lines, a few hundred tokens) and
  earned its keep the first time the agent answers "what file holds
  X?" without a single ``grep`` call. Built by walking ``src/talos/``
  and pulling the first sentence of each module's leading docstring —
  which Talos already has on every file (a habit worth keeping).

* **Deep read** (the ``read_self`` tool, lazy). When the agent needs
  the full picture of one module, it calls ``read_self("memory/sessions.py")``
  and gets the full file. Same shape as ``load_skill`` — pay for the
  detail only when you reach for it.

Persistence: the manifest is cached at ``.talos/self/manifest.json``
and regenerated when any source file is newer than the cache. That
means ``talos self show`` is fast on the second run, but never stale
after an edit.

Why "first sentence of the docstring" works: Talos's convention is
that every module starts with an emoji-prefixed one-liner describing
its purpose (read any file in ``src/talos/`` to see it). Auto-extracting
that gives us a curated manifest without any hand-maintained list to
drift out of sync.
"""

from __future__ import annotations

import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path


# ── 📐 the schema ──────────────────────────────────────────────────────


@dataclass
class ModuleFact:
    """One file in Talos's source tree, indexed by purpose."""

    path: str  # POSIX-style relative path, e.g. "src/talos/memory/sessions.py"
    package: str  # immediate parent under src/talos/, e.g. "memory"
    purpose: str  # first sentence of the leading docstring
    module: str  # dotted name, e.g. "talos.memory.sessions"


# ── 🗂️ storage ─────────────────────────────────────────────────────────


def self_dir() -> Path:
    """Where the cached manifest lives. Per-project, like everything else
    under .talos/."""
    return Path(".talos") / "self"


def manifest_file() -> Path:
    return self_dir() / "manifest.json"


# ── 🔍 source walking ──────────────────────────────────────────────────


def source_root() -> Path:
    """The ``talos/`` package root. Resolves from this file's location so
    it works regardless of where the process is running from."""
    # __file__ is .../src/talos/lifecycle/self_knowledge.py
    # parents[1] = .../src/talos/
    return Path(__file__).resolve().parents[1]


def _first_sentence(text: str) -> str:
    """Trim a docstring down to a one-line summary. Prefers the first
    period-terminated sentence; falls back to the first non-empty line."""
    text = (text or "").strip()
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    # If the first line ends with a period, return as-is.
    if "." in first_line:
        head = first_line.split(".", 1)[0].strip()
        return head + "."
    return first_line


def _module_docstring(path: Path) -> str:
    """Parse a Python file with AST and return its module-level docstring."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return ""
    doc = ast.get_docstring(tree)
    return doc or ""


def _dotted_module(path: Path, src_root: Path) -> str:
    """Convert ``src/talos/memory/sessions.py`` → ``talos.memory.sessions``."""
    try:
        rel = path.relative_to(src_root)
    except ValueError:
        return ""
    parts = ["talos"] + list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(path: Path, src_root: Path) -> str:
    """First component under src/talos/. The top-level files (cli.py,
    config.py, __init__.py) get the synthetic package name 'core'.

    ``src_root`` is the ``talos/`` package root, so ``parts[0]`` is the
    subpackage name (agent, memory, …) directly."""
    try:
        rel = path.relative_to(src_root)
    except ValueError:
        return "core"
    parts = rel.parts
    if len(parts) <= 1:
        return "core"
    return parts[0]


def walk_source(src_root: Path | None = None) -> list[ModuleFact]:
    """Walk Talos's source tree and produce a ModuleFact per .py file.

    Skipped: ``__pycache__``, dunder files except ``__init__.py``,
    files whose docstring is empty (they'd be noise in the index).
    """
    root = src_root or source_root()
    facts: list[ModuleFact] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        name = path.name
        if name.startswith("__") and name not in ("__init__.py", "__main__.py"):
            continue
        doc = _module_docstring(path)
        if not doc:
            continue
        purpose = _first_sentence(doc)
        if not purpose:
            continue
        # Always show as src/talos/<rest> — the canonical reference path,
        # regardless of where the package was installed from.
        try:
            inside = path.relative_to(root).as_posix()
            rel = f"src/talos/{inside}"
        except ValueError:
            rel = path.as_posix()
        facts.append(
            ModuleFact(
                path=rel,
                package=_package_of(path, root),
                purpose=purpose,
                module=_dotted_module(path, root),
            )
        )
    return facts


# ── 🧮 freshness check ─────────────────────────────────────────────────


def _newest_source_mtime(src_root: Path) -> float:
    newest = 0.0
    for p in src_root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m > newest:
            newest = m
    return newest


def is_stale() -> bool:
    """True if the cached manifest is missing or older than any .py file
    in the tree."""
    f = manifest_file()
    if not f.is_file():
        return True
    try:
        cache_mtime = f.stat().st_mtime
    except OSError:
        return True
    src_root = source_root()
    if not src_root.is_dir():
        return True
    return _newest_source_mtime(src_root) > cache_mtime


# ── 💾 load / save ────────────────────────────────────────────────────


def save_manifest(facts: list[ModuleFact]) -> Path:
    self_dir().mkdir(parents=True, exist_ok=True)
    f = manifest_file()
    payload = {"facts": [asdict(x) for x in facts]}
    f.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return f


def load_manifest_cached() -> list[ModuleFact] | None:
    f = manifest_file()
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return [ModuleFact(**x) for x in data.get("facts", [])]


def manifest(force_refresh: bool = False) -> list[ModuleFact]:
    """The current manifest. Lazy: cached on disk, regenerated when the
    cache is stale relative to source mtimes (or when ``force_refresh``)."""
    if not force_refresh:
        cached = load_manifest_cached()
        if cached is not None and not is_stale():
            return cached
    facts = walk_source()
    try:
        save_manifest(facts)
    except OSError:
        # Read-only filesystem? Still return the in-memory result.
        pass
    return facts


# ── 🪞 system-prompt projection ────────────────────────────────────────


def manifest_summary(max_chars: int = 6000) -> str:
    """The compact form that goes into the system prompt — one line per
    file, grouped by package. ``max_chars`` is a soft budget; we truncate
    long packages with a "(N more files)" tail when needed."""
    facts = manifest()
    if not facts:
        return ""

    groups: dict[str, list[ModuleFact]] = {}
    for f in facts:
        groups.setdefault(f.package, []).append(f)

    # Stable order: 'core' first (the top-level), then alphabetical.
    package_order = ["core"] + sorted(p for p in groups if p != "core")
    lines: list[str] = [
        "## Self-knowledge (Talos's own source tree)",
        "_Read any of these in full with the `read_self` tool when you "
        "need detail. Paths are relative to the repo root._",
        "",
    ]
    budget = max_chars - sum(len(l) + 1 for l in lines)
    for pkg in package_order:
        if pkg not in groups:
            continue
        items = groups[pkg]
        header = f"**{pkg}/** ({len(items)} file{'s' if len(items) != 1 else ''}):"
        lines.append(header)
        budget -= len(header) + 1
        for fact in items:
            entry = f"- `{fact.path}` — {fact.purpose}"
            if budget - len(entry) - 1 < 200:  # save room for tail markers
                lines.append(f"  (… {len(items) - (items.index(fact))} more in {pkg}/)")
                budget -= 40
                break
            lines.append(entry)
            budget -= len(entry) + 1
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── 📖 the deep-read primitive (used by the read_self tool) ────────────


def deep_read(rel_path: str) -> str:
    """Read a file from Talos's own source tree by its relative path.

    Sanity rails: only paths inside ``src/talos/`` are accepted; the file
    must exist and be a regular file. Raises ``ValueError`` otherwise so
    the tool wrapper can return a readable error to the model."""
    # Reject absolute paths and parent-traversal up front — easier to
    # reason about than relying on .resolve() to catch every escape.
    s = rel_path.strip()
    if not s:
        raise ValueError("empty path")
    if s.startswith("/") or s.startswith("\\"):
        raise ValueError(
            f"refusing to read {rel_path!r}: outside Talos's source tree"
        )
    parts = s.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise ValueError(
            f"refusing to read {rel_path!r}: outside Talos's source tree"
        )
    # Strip a leading "./" or "src/talos/" prefix if present.
    if parts and parts[0] == ".":
        parts = parts[1:]
    if len(parts) >= 2 and parts[0] == "src" and parts[1] == "talos":
        parts = parts[2:]
    if not parts:
        raise ValueError(f"empty path after stripping prefix: {rel_path!r}")

    candidate = (source_root() / "/".join(parts)).resolve()
    src = source_root().resolve()
    try:
        candidate.relative_to(src)
    except ValueError:
        # Belt-and-suspenders — should be unreachable after the explicit
        # checks above, but symlink shenanigans could still escape.
        raise ValueError(
            f"refusing to read {rel_path!r}: outside Talos's source tree"
        )
    if not candidate.is_file():
        raise ValueError(f"no such file: {rel_path!r}")
    return candidate.read_text(encoding="utf-8")


def by_package(pkg: str) -> list[ModuleFact]:
    """All ModuleFacts in one subpackage."""
    return [f for f in manifest() if f.package == pkg]
