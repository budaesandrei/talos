"""🤖🤖 Subagents — specialists with their own context window.

Why subagents exist (in every serious agent framework):

- **context isolation**: a research task might read 50 files; doing that
  in the main conversation drowns it in noise. A subagent burns its own
  context and reports back one tidy summary.
- **role separation**: a reviewer with a "find problems" prompt finds
  more problems than a generalist that also wrote the code.
- **tool scoping**: a subagent only gets the tools its job needs.

Definition format — ``.talos/agents/<name>.md``:

    ---
    name: researcher
    description: Reads code/docs and answers questions with citations
    tools: read_file, grep, glob_files, web_fetch
    model: gpt-4o-mini            (optional — defaults to the main model)
    ---
    You are a research specialist. … (this body becomes its system prompt)

The main agent sees the roster in its system prompt and delegates via the
``task`` tool. Subagents can't spawn subagents (no recursion).
"""

from pathlib import Path

from pydantic import BaseModel, Field

from talos.skills import _parse_frontmatter  # same minimal frontmatter format


class AgentDef(BaseModel):
    """One subagent definition (pydantic v2 — see Skill for the rationale)."""

    name: str
    description: str
    system_prompt: str
    tools: list[str] = Field(default_factory=list)  # empty = safe default set
    model: str | None = None


def agents_dir() -> Path:
    return Path(".talos") / "agents"


def discover_agents() -> list[AgentDef]:
    found = []
    base = agents_dir()
    if not base.is_dir():
        return found
    for f in sorted(base.glob("*.md")):
        meta, body = _parse_frontmatter(f.read_text(encoding="utf-8"))
        tools = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()]
        found.append(
            AgentDef(
                name=meta.get("name", f.stem),
                description=meta.get("description", "(no description)"),
                system_prompt=body,
                tools=tools,
                model=meta.get("model") or None,
            )
        )
    return found


def agents_summary() -> str:
    """Roster for the main agent's system prompt."""
    agents = discover_agents()
    if not agents:
        return ""
    lines = [f"- {a.name}: {a.description}" for a in agents]
    return (
        "## Subagents (delegate bounded tasks with the task tool)\n"
        + "\n".join(lines)
    )
