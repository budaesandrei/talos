"""👥 Team tool — fan out parallel subagents (Anthropic's research pattern).

The serial `task` tool delegates one bounded job at a time. For breadth
work — "research these 5 libraries", "review each of these modules" — a
lead agent gets far more done by dispatching workers **in parallel** and
synthesizing their reports. Anthropic's multi-agent research setup beat
single-agent by ~90% on breadth tasks this way.

`team` runs N briefs concurrently via asyncio.gather, each on its own
fresh graph (own context, scoped read-only tools by default), writing to
a shared scratchpad file so the lead can see partial results. Workers
never get `team` or `task` — no recursive fan-out explosions.
"""

import asyncio
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from talos.config import settings
from talos.agent.context import environment_info
from talos.agent.graph.builder import build_agent_graph
from talos.agent.llm import build_llm
from talos.infra.permissions import PermissionGate

MAX_WORKERS = 6  # cap concurrency to stay polite to the provider


def scratchpad() -> Path:
    return Path(".talos") / "team_scratch.md"


async def _run_worker(brief: str, idx: int) -> str:
    from talos.tools.task_tool import DEFAULT_SUBAGENT_TOOLS, _resolve_tools

    graph = build_agent_graph(
        llm=build_llm(),
        tools=_resolve_tools(DEFAULT_SUBAGENT_TOOLS),  # read-only by default
        system_prompt=(
            "You are worker #%d on a team. Do your assigned brief thoroughly "
            "but report back CONCISELY (a few sentences + any findings). You "
            "cannot modify files.\n\n%s" % (idx, environment_info())
        ),
        gate=PermissionGate(approver=None, yolo=settings.yolo),
    )
    try:
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=brief)]},
            config={"recursion_limit": settings.max_iterations},
        )
    except Exception as exc:  # one worker failing shouldn't sink the team
        return f"worker #{idx} error: {exc}"
    for msg in reversed(result["messages"]):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            _append_scratch(idx, brief, text)
            return text
    return f"worker #{idx}: no answer"


def _append_scratch(idx: int, brief: str, text: str) -> None:
    try:
        scratchpad().parent.mkdir(parents=True, exist_ok=True)
        with scratchpad().open("a", encoding="utf-8") as fh:
            fh.write(f"\n## worker #{idx}: {brief[:60]}\n{text}\n")
    except OSError:
        pass


@tool
async def team(briefs: list[str]) -> str:
    """Dispatch several independent briefs to parallel worker subagents and
    collect their reports. Use for breadth tasks where the pieces don't
    depend on each other (research N things, review N files). Each worker
    is read-only and cannot see the others. Returns all reports."""
    if not briefs:
        return "no briefs given"
    briefs = briefs[:MAX_WORKERS]
    scratchpad().unlink(missing_ok=True)  # fresh board per dispatch
    results = await asyncio.gather(
        *(_run_worker(b, i + 1) for i, b in enumerate(briefs))
    )
    return "\n\n".join(f"### worker #{i+1}\n{r}" for i, r in enumerate(results))
