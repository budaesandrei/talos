"""🧠 Memory tool — lets the agent write its own long-term memory."""

from langchain_core.tools import tool

from talos.memory import append_memory


@tool
def save_memory(fact: str) -> str:
    """Save a short, durable fact to long-term memory (it will be visible in
    every future session). Use for stable preferences and project facts,
    not for transient details."""
    return append_memory(fact)
