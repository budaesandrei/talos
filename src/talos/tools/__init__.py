"""🔧 Tool registry.

A *tool* is just a Python function with a good docstring, wrapped with
``@tool`` so the LLM can see its JSON schema and request calls to it.
``get_tools()`` is the single place the runtime asks for "everything
Talos can do".
"""

from langchain_core.tools import BaseTool

from talos.tools.files import edit_file, glob_files, grep, list_dir, read_file, write_file
from talos.tools.jobs import job_kill, job_status
from talos.tools.memory_tool import save_memory
from talos.tools.shell import shell
from talos.tools.recall_tool import recall_memory
from talos.tools.skill_tool import load_skill
from talos.tools.task_tool import task
from talos.tools.team_tool import team
from talos.tools.web import web_fetch


def get_tools() -> list[BaseTool]:
    """Return all tools available to the agent."""
    return [
        read_file,
        write_file,
        edit_file,
        list_dir,
        glob_files,
        grep,
        shell,
        job_status,
        job_kill,
        web_fetch,
        save_memory,
        load_skill,
        recall_memory,
        task,
        team,
    ]
