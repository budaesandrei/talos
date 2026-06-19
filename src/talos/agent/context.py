"""🧠 Context assembly — everything Talos knows *before* you say anything.

The system prompt is assembled fresh for each session from layered parts:

1. the base persona       (``prompts/system.md``)         — ships with Talos
2. 📜 rules               (``TALOS.md``, ``~/.talos/TALOS.md``) — written by you
3. 🧠 memory              (``.talos/memory.md``)           — written by the agent
4. 🎒 skills index        (``.talos/skills/*/SKILL.md``)   — names only, lazy-loaded
5. runtime environment    (cwd, date)

This layering is the same pattern as CLAUDE.md / AGENTS.md / .cursorrules:
stable instructions live in files, not in chat history.
"""

from datetime import datetime
from pathlib import Path

from talos.integrations.agents import agents_summary
from talos.config import PACKAGE_ROOT, settings
from talos.memory.notes import load_memory
from talos.lifecycle.skills import skills_summary

BASE_PROMPT_PATH = PACKAGE_ROOT / "prompts" / "system.md"

# Project rules first, then global personal rules.
RULES_LOCATIONS = [
    Path("TALOS.md"),
    Path.home() / ".talos" / "TALOS.md",
]


def load_rules() -> str:
    chunks = []
    for location in RULES_LOCATIONS:
        if location.is_file():
            chunks.append(f"<!-- from {location} -->\n" + location.read_text(encoding="utf-8").strip())
    return "\n\n".join(chunks)


def environment_info() -> str:
    from talos.infra.environment import describe  # late import (reads settings)

    return (
        "## Environment\n"
        f"{describe()}\n"
        f"- working directory: {Path.cwd()}\n"
        f"- today's date: {datetime.now():%Y-%m-%d}"
    )


def build_system_prompt() -> str:
    parts = [BASE_PROMPT_PATH.read_text(encoding="utf-8").strip()]

    rules = load_rules()
    if rules:
        parts.append("## Rules (from TALOS.md — always follow these)\n" + rules)

    memory = load_memory()
    if memory:
        parts.append("## Memory (facts you saved in earlier sessions)\n" + memory)

    skills = skills_summary()
    if skills:
        parts.append(skills)

    agents = agents_summary()
    if agents:
        parts.append(agents)

    # 🪞 self-knowledge: a compact index of Talos's own source so the agent
    # can answer "where would I add X?" without grepping. Lazy on errors —
    # a malformed cache must never block the system prompt.
    try:
        from talos.lifecycle.self_knowledge import manifest_summary

        index = manifest_summary()
        if index:
            parts.append(index)
    except Exception:
        pass

    # 🔐 vault: list available handles by name + description so the model
    # knows when to use {{secret:name}} substitution in shell commands.
    # SECRET values never enter the prompt; VALUE handles are inlined.
    try:
        from talos.infra.vault import vault_summary

        vsum = vault_summary()
        if vsum:
            parts.append(vsum)
    except Exception:
        pass

    if settings.workspace_snapshot:
        try:
            from talos.agent.workspace import snapshot

            snap = snapshot()
            if snap:
                parts.append(snap)
        except Exception:
            pass

    if settings.think:
        parts.append(
            "## Think mode 💭\n"
            "Before your final answer, reason step by step inside a single "
            "<thinking>…</thinking> block: restate the goal, consider "
            "approaches, note risks. Then give your answer OUTSIDE the block. "
            "Keep the thinking concise."
        )

    parts.append(environment_info())
    return "\n\n".join(parts)
