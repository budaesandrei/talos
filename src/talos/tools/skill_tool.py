"""🎒 Skill tool — fetches a skill's full instructions on demand."""

from langchain_core.tools import tool

from talos.lifecycle.skills import skill_body


@tool
def load_skill(name: str) -> str:
    """Load the full instructions of a skill listed in the system prompt.
    Call this BEFORE attempting a task a skill covers."""
    return skill_body(name)
