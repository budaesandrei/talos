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
from talos.permissions import PermissionGate
from talos.sessions import (
    latest_session_id,
    load_session,
    new_session_id,
    save_session,
)
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


def _ask_permission(tool_name: str, args: dict) -> str:
    """🛡️ Interactive approval prompt (used by the PermissionGate)."""
    console.print(
        f"\n[bold yellow]🛡️  {tool_name}[/]([dim]{_args_preview(args, 400)}[/])"
    )
    return console.input("[yellow]allow? \[y]es · \[n]o · \[a]lways ›[/] ")


class Runtime:
    """One conversation: a compiled graph + the message history."""

    def __init__(
        self,
        model: str | None = None,
        yolo: bool = False,
        interactive: bool = True,
        resume: str | None = None,
    ):
        gate = PermissionGate(
            approver=_ask_permission if interactive else None,
            yolo=yolo or settings.yolo,
        )
        self.graph = build_agent_graph(
            llm=build_llm(model),
            tools=get_tools(),
            system_prompt=build_system_prompt(),
            gate=gate,
        )
        # 💾 Either continue an old session or start a new one.
        if resume:
            session_id = latest_session_id() if resume == "latest" else resume
            if session_id is None:
                raise FileNotFoundError("no sessions to resume")
            self.session_id = session_id
            self.messages: list[BaseMessage] = load_session(session_id)
        else:
            self.session_id = new_session_id()
            self.messages = []

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
        save_session(self.session_id, self.messages)

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


async def run_once(prompt: str, model: str | None = None, yolo: bool = False) -> None:
    """⚡ One-shot mode: single turn, then exit (good for scripts/pipes).

    Non-interactive runs can't ask for approval, so mutating tools are
    denied unless ``--yolo`` is passed.
    """
    await Runtime(model, yolo=yolo, interactive=False).turn(prompt)


async def repl(
    model: str | None = None,
    initial_prompt: str | None = None,
    yolo: bool = False,
    resume: str | None = None,
) -> None:
    """💬 Interactive mode."""
    rt = Runtime(model, yolo=yolo, resume=resume)
    if resume and rt.messages:
        console.print(f"[dim]💾 resumed session {rt.session_id} "
                      f"({len(rt.messages)} messages)[/]")
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
