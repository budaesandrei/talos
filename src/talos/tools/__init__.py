"""🔧 Tool registry.

A *tool* is just a Python function with a good docstring, wrapped so the
LLM can see its JSON schema and request calls to it. ``get_tools()`` is the
single place the runtime asks for "everything Talos can do".
"""

from langchain_core.tools import BaseTool


def get_tools() -> list[BaseTool]:
    """Return all tools available to the agent (grows in later milestones)."""
    return []
