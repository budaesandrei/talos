"""🎒 Skills — on-demand knowledge the agent loads only when relevant.

A skill is a folder under ``.talos/skills/<name>/`` containing a
``SKILL.md`` with frontmatter:

    ---
    name: deploy-checklist
    description: Step-by-step deploy procedure for this project
    ---
    (the actual instructions…)

The trick that makes skills cheap: only the **name + description** go into
the system prompt. The body is fetched by the ``load_skill`` tool when the
model decides it's relevant. Compare with rules (always loaded) — skills
are the lazy-loaded counterpart.
"""

from pathlib import Path

from pydantic import BaseModel


class Skill(BaseModel):
    """One discovered skill. Pydantic v2 model: validated on construction,
    so a broken SKILL.md fails loudly here instead of mysteriously later."""

    name: str
    description: str
    path: Path


def skills_dir() -> Path:
    return Path(".talos") / "skills"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a minimal '--- key: value ---' header. No YAML dependency."""
    if not text.startswith("---"):
        return {}, text
    try:
        header, body = text[3:].split("---", 1)
    except ValueError:
        return {}, text
    meta = {}
    for line in header.strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, body.strip()


def discover_skills() -> list[Skill]:
    found = []
    base = skills_dir()
    if not base.is_dir():
        return found
    for skill_file in sorted(base.glob("*/SKILL.md")):
        meta, _ = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        found.append(
            Skill(
                name=meta.get("name", skill_file.parent.name),
                description=meta.get("description", "(no description)"),
                path=skill_file,
            )
        )
    return found


def skill_body(name: str) -> str:
    for skill in discover_skills():
        if skill.name == name:
            _, body = _parse_frontmatter(skill.path.read_text(encoding="utf-8"))
            return body
    available = ", ".join(s.name for s in discover_skills()) or "(none)"
    return f"Error: no skill named '{name}'. Available: {available}"


def skills_summary() -> str:
    """The cheap index that goes into the system prompt."""
    skills = discover_skills()
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description}" for s in skills]
    return (
        "## Skills (load with the load_skill tool when relevant)\n"
        + "\n".join(lines)
    )
