"""🔧 Tool registry.

A *tool* is just a Python function with a good docstring, wrapped with
``@tool`` so the LLM can see its JSON schema and request calls to it.
``get_tools()`` is the single place the runtime asks for "everything
Talos can do".
"""

from langchain_core.tools import BaseTool

from talos.tools.files import edit_file, glob_files, grep, list_dir, read_file, write_file
from talos.tools.memory_tool import save_memory
from talos.tools.shell import shell
from talos.tools.recall_tool import recall_memory
from talos.tools.skill_tool import load_skill
from talos.tools.task_tool import task
from talos.tools.team_tool import team
from talos.tools.self_tool import read_self
from talos.tools.knowledge_tool import (
    add_kb_tool, list_kbs_tool, recall_knowledge, remove_kb_tool,
    update_kb_tool,
)
from talos.tools.meta_tools import (
    create_skill_tool,
    list_checkpoints_tool,
    list_links_tool,
    list_mcp_servers_tool,
    list_models_tool,
    list_runs_tool,
    list_schedules_tool,
    list_vault_handles_tool,
    schedule_add_tool,
    schedule_remove_tool,
    schedule_show_tool,
)
from talos.tools.sessions_tool import list_sessions_tool, search_sessions_tool
from talos.tools.vault_tool import vault_get
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
        web_fetch,
        save_memory,
        load_skill,
        recall_memory,
        read_self,
        list_sessions_tool,
        search_sessions_tool,
        recall_knowledge,
        list_kbs_tool,
        add_kb_tool,
        update_kb_tool,
        remove_kb_tool,
        list_schedules_tool,
        schedule_show_tool,
        schedule_add_tool,
        schedule_remove_tool,
        list_runs_tool,
        list_models_tool,
        list_checkpoints_tool,
        create_skill_tool,
        list_vault_handles_tool,
        list_links_tool,
        list_mcp_servers_tool,
        vault_get,
        task,
        team,
    ]
