"""🔗 Cross-agent linking — borrow skills/agents/MCPs from your other tools.

You already have skills in ~/.kiro, ~/.cursor, ~/.claude, ~/.codex,
~/.gemini. Rather than copy them (and drift out of sync), Talos *links*
those directories: it reads them live, so adding a skill to Kiro makes it
appear in Talos too.

Three principles, each fixing a Kiro annoyance:

- **live, not imported**: we store the linked paths in .talos/links.json
  and re-scan every launch — no stale copies.
- **smart discovery**: a folder is only a skill if it actually contains
  SKILL.md. A stray .git or junk folder is silently skipped, not an
  error (Kiro complains; we don't).
- **dedup by name with priority**: the same skill in Kiro and Cursor is
  resolved once, by link order — first link wins, so you control which.

Known agent layouts are auto-detected so ``talos link ~/.kiro`` finds the
right subfolders without you spelling them out.
"""

import json
from pathlib import Path

# where each agent keeps its skills / subagents / mcp config, relative to
# the agent's home dir. Used to auto-discover when you link a root.
AGENT_LAYOUTS = {
    "kiro":   {"skills": "skills", "agents": "agents", "mcp": "settings/mcp.json"},
    "cursor": {"skills": "skills", "agents": "modes",  "mcp": "mcp.json"},
    "claude": {"skills": "skills", "agents": "agents", "mcp": ".mcp.json"},
    "codex":  {"skills": "skills", "agents": "agents", "mcp": "config.toml"},
    "gemini": {"skills": "skills", "agents": "agents", "mcp": "settings.json"},
}


def links_file() -> Path:
    return Path(".talos") / "links.json"


def load_links() -> list[str]:
    f = links_file()
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8")).get("links", [])
        except json.JSONDecodeError:
            return []
    return []


def add_link(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_dir():
        return f"not a directory: {p}"
    links = load_links()
    sp = str(p)
    if sp in links:
        return f"already linked: {p}"
    links.append(sp)
    links_file().parent.mkdir(parents=True, exist_ok=True)
    links_file().write_text(json.dumps({"links": links}, indent=1), encoding="utf-8")
    return f"🔗 linked {p}"


def remove_link(path: str) -> str:
    p = str(Path(path).expanduser())
    links = [x for x in load_links() if x != p]
    links_file().write_text(json.dumps({"links": links}, indent=1), encoding="utf-8")
    return f"unlinked {p}"


def _skill_dirs(root: Path) -> list[Path]:
    """Every subdir of `root` that actually contains a SKILL.md.
    Junk folders (.git, no SKILL.md) are silently skipped."""
    if not root.is_dir():
        return []
    return sorted(
        d for d in root.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    )


def _resolve_skill_roots(link: Path) -> list[Path]:
    """A linked path might be an agent home (~/.kiro) or a skills dir
    directly. Try the known layouts, then the path itself."""
    roots = []
    name = link.name.lstrip(".")
    layout = AGENT_LAYOUTS.get(name)
    if layout:
        roots.append(link / layout["skills"])
    roots.append(link)                 # maybe it IS a skills dir
    roots.append(link / "skills")      # or has a generic skills/ child
    return [r for r in roots if r.is_dir()]


def discover_linked_skills() -> list[dict]:
    """All skills from linked agents, deduped by name (first link wins)."""
    from talos.skills import _parse_frontmatter

    seen: dict[str, dict] = {}
    for link in load_links():
        for root in _resolve_skill_roots(Path(link)):
            for sd in _skill_dirs(root):
                meta, _ = _parse_frontmatter(
                    (sd / "SKILL.md").read_text(encoding="utf-8")
                )
                name = meta.get("name", sd.name)
                if name in seen:           # 🧹 dedup by name, priority = order
                    continue
                seen[name] = {
                    "name": name,
                    "description": meta.get("description", ""),
                    "path": str(sd / "SKILL.md"),
                    "source": Path(link).name,
                }
    return list(seen.values())


def discover_linked_mcp() -> dict:
    """Merged mcpServers from linked agents' MCP config files (JSON only;
    deduped by server name, first link wins)."""
    merged: dict = {}
    for link in load_links():
        lp = Path(link)
        name = lp.name.lstrip(".")
        layout = AGENT_LAYOUTS.get(name, {})
        candidates = [lp / layout.get("mcp", ""), lp / "mcp.json", lp / ".mcp.json"]
        for cf in candidates:
            if cf.is_file() and cf.suffix == ".json":
                try:
                    data = json.loads(cf.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                for sname, spec in (data.get("mcpServers") or {}).items():
                    merged.setdefault(sname, spec)   # first link wins
    return merged
