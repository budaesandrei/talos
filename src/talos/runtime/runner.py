"""🏃 Runtime — drives the compiled graph and renders the conversation.

Two entry points:

- ``repl()``      → interactive chat session (``talos chat``)
- ``run_once()``  → single prompt, print, exit (``talos run`` / ``--no-interactive``)

Streaming: we ask LangGraph for two parallel views of the run —

- ``messages`` mode yields LLM **token chunks** as they're generated
  (that's what makes text appear live), and
- ``updates`` mode yields each node's **finished output** (that's where we
  learn about tool calls and tool results, and collect history).
"""

import json

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langgraph.errors import GraphRecursionError
from rich.console import Console

from talos.config import settings
from talos.context import build_system_prompt
from talos.graph.builder import build_agent_graph
from talos.llm import build_llm
from talos.tools import get_tools

console = Console()


def get_message_text(message: BaseMessage) -> str:
    """Message content can be a plain string or a list of content blocks."""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
        )
    return str(content)


def _args_preview(args: dict, limit: int = 120) -> str:
    text = json.dumps(args, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Runtime:
    """One conversation: a compiled graph + the message history."""

    def __init__(self, model: str | None = None):
        self.graph = build_agent_graph(
            llm=build_llm(model),
            tools=get_tools(),
            system_prompt=build_system_prompt(),
        )
        self.messages: list[BaseMessage] = []

    async def turn(self, user_input: str) -> str:
        """Send one user message through the graph, streaming the output."""
        self.messages.append(HumanMessage(content=user_input))
        collected: list[BaseMessage] = []
        streamed_any = False

        try:
            async for mode, payload in self.graph.astream(
                {"messages": self.messages},
                config={"recursion_limit": settings.max_iterations},
                stream_mode=["messages", "updates"],
            ):
                if mode == "messages":
                    chunk, meta = payload
                    # Live-print assistant text tokens (not tool-call deltas).
                    if (
                        isinstance(chunk, AIMessageChunk)
                        and meta.get("langgraph_node") == "agent"
                    ):
                        text = get_message_text(chunk)
                        if text:
                            print(text, end="", flush=True)
                            streamed_any = True

                elif mode == "updates":
                    for node, update in (payload or {}).items():
                        for msg in (update or {}).get("messages", []):
                            collected.append(msg)
                            self._render_side_effects(msg)

        except GraphRecursionError:
            console.print(
                f"\n[yellow]⚠️  stopped: hit max_iterations "
                f"({settings.max_iterations})[/]"
            )

        if streamed_any:
            print()  # finish the streamed line
        self.messages.extend(collected)

        for msg in reversed(collected):
            if isinstance(msg, AIMessage):
                return get_message_text(msg)
        return ""

    def _render_side_effects(self, msg: BaseMessage) -> None:
        """Show tool activity so the user can follow what the agent does."""
        if isinstance(msg, AIMessage) and msg.tool_calls:
            print(flush=True)
            for call in msg.tool_calls:
                console.print(
                    f"[dim]🔧 {call['name']}({_args_preview(call['args'])})[/]"
                )
        elif isinstance(msg, ToolMessage):
            preview = get_message_text(msg).strip().splitlines()
            first = preview[0] if preview else ""
            more = f" (+{len(preview) - 1} lines)" if len(preview) > 1 else ""
            console.print(f"[dim]   ↳ {first[:120]}{more}[/]")


async def run_once(prompt: str, model: str | None = None) -> None:
    """⚡ One-shot mode: single turn, then exit (good for scripts/pipes)."""
    await Runtime(model).turn(prompt)


async def repl(model: str | None = None, initial_prompt: str | None = None) -> None:
    """💬 Interactive mode."""
    rt = Runtime(model)
    console.print(
        f"[bold cyan]🤖 talos[/] — model [magenta]{model or settings.model}[/] · "
        "[dim]/exit to quit[/]"
    )

    if initial_prompt:
        console.print(f"[bold cyan]you ›[/] {initial_prompt}")
        await rt.turn(initial_prompt)

    while True:
        try:
            user_input = console.input("[bold cyan]you ›[/] ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye 👋[/]")
            break

        stripped = user_input.strip()
        if not stripped:
            continue
        if stripped in {"/exit", "/quit"}:
            console.print("[dim]bye 👋[/]")
            break

        await rt.turn(user_input)
