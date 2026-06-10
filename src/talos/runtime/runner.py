"""🏃 Runtime — drives the compiled graph and renders a pleasant TUI.

Two entry points:

- ``repl()``      → interactive chat session (``talos chat``)
- ``run_once()``  → single prompt, print, exit (``talos run`` / ``--no-interactive``)

Streaming: we ask LangGraph for two parallel views of the run —

- ``messages`` mode yields LLM **token chunks** as they're generated
  (that's what makes text appear live), and
- ``updates`` mode yields each node's **finished output** (that's where we
  learn about tool calls and tool results, and collect history).

UX details worth stealing:

- a spinner runs whenever the agent is busy and *stops the moment the
  first token arrives* — perceived latency drops to near zero
- tool calls render as dim one-liners with per-tool emoji, results as
  ``↳`` previews — enough to follow along without drowning in output
- permission prompts pause the spinner, ask, then resume
- Ctrl-C cancels the current turn, not the whole session
"""

import itertools
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
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from talos.commands import dispatch, help_text
from talos.config import settings
from talos.context import build_system_prompt
from talos.graph.builder import build_agent_graph
from talos.llm import build_llm
from talos.mcp import load_mcp_tools
from talos.mermaid import extract_mermaid, open_in_browser
from talos.permissions import PermissionGate
from talos.sessions import (
    latest_session_id,
    load_session,
    new_session_id,
    save_session,
)
from talos.tools import get_tools

console = Console()

THINKING = itertools.cycle(
    ["🤔 thinking", "🧠 reasoning", "🪄 putting it together", "☕ one moment"]
)

TOOL_EMOJI = {
    "read_file": "📖", "write_file": "✍️ ", "edit_file": "✏️ ",
    "list_dir": "📂", "glob_files": "🔍", "grep": "🔎",
    "shell": "🐚", "web_fetch": "🌐", "save_memory": "🧠",
}


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


class _Status:
    """Tiny wrapper around rich's spinner so we can pause it for input."""

    def __init__(self):
        self._status = None

    def set(self, text: str) -> None:
        self.stop()
        self._status = console.status(f"[dim]{text}[/]", spinner="dots")
        self._status.start()

    def stop(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None


class Runtime:
    """One conversation: a compiled graph + the message history."""

    def __init__(
        self,
        model: str | None = None,
        yolo: bool = False,
        interactive: bool = True,
        resume: str | None = None,
        extra_tools: list | None = None,
    ):
        self.status = _Status()
        gate = PermissionGate(
            approver=self._ask_permission if interactive else None,
            yolo=yolo or settings.yolo,
        )
        self.graph = build_agent_graph(
            llm=build_llm(model),
            tools=get_tools() + list(extra_tools or []),  # built-ins + 🔌 MCP
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
        self.last_mermaid: list[str] = []  # 🧜 filled after each reply
        # 📊 running totals from AIMessage.usage_metadata (LangChain
        # normalizes every provider's usage block into this one shape)
        self.usage = {"input": 0, "output": 0, "total": 0, "turns": 0}

    # ── 🛡️ permission prompt (pauses the spinner) ───────────────────────
    def _ask_permission(self, tool_name: str, args: dict) -> str:
        self.status.stop()
        pretty = json.dumps(args, indent=2, ensure_ascii=False, default=str)
        if len(pretty) > 600:
            pretty = pretty[:600] + "\n…"
        console.print(
            Panel(
                Text(pretty),
                title=f"🛡️  {TOOL_EMOJI.get(tool_name, '🔧')} {tool_name}",
                border_style="yellow",
                title_align="left",
            )
        )
        answer = console.input("[yellow]allow? \\[y]es · \\[n]o · \\[a]lways ›[/] ")
        self.status.set(f"⚙️  running {tool_name}…")
        return answer

    # ── 💬 one user turn ─────────────────────────────────────────────────
    async def turn(self, user_input: str) -> str:
        self._turn_usage = {"input": 0, "output": 0, "total": 0}
        self.usage["turns"] += 1
        self.messages.append(HumanMessage(content=user_input))
        collected: list[BaseMessage] = []
        # 🎨 streaming state: with markdown on, we re-render the growing
        # buffer through rich.Live (live markdown!); with it off, we print
        # raw tokens. Either way the spinner dies on the first token.
        buffer = ""
        live: Live | None = None
        prefix_printed = False

        def close_stream() -> None:
            nonlocal live, buffer, prefix_printed
            if live is not None:
                live.stop()
                live = None
            if prefix_printed:
                print()  # finish the raw streamed line
            buffer = ""
            prefix_printed = False

        self.status.set(f"{next(THINKING)}…")
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
                        if not text:
                            continue
                        self.status.stop()
                        if settings.markdown:
                            buffer += text
                            if live is None:
                                console.print("[bold magenta]talos ›[/]")
                                live = Live(
                                    console=console,
                                    refresh_per_second=8,
                                    vertical_overflow="visible",
                                )
                                live.start()
                            live.update(Markdown(buffer))
                        else:
                            if not prefix_printed:
                                console.print("[bold magenta]talos ›[/] ", end="")
                                prefix_printed = True
                            print(text, end="", flush=True)

                elif mode == "updates":
                    close_stream()  # tool lines must not fight the Live region
                    for node, update in (payload or {}).items():
                        for msg in (update or {}).get("messages", []):
                            collected.append(msg)
                            self._track_usage(msg)
                            self._render_side_effects(msg)
                        if node == "tools":
                            # back to the model for the next think step
                            self.status.set(f"{next(THINKING)}…")

        except GraphRecursionError:
            console.print(
                f"\n[yellow]⚠️  stopped: hit max_iterations "
                f"({settings.max_iterations})[/]"
            )
        except Exception as exc:  # noqa: BLE001 — an API/network error must
            # never kill the session: history is saved below, so the user can
            # fix .env (expired key, SSL, …) and pick up where they left off.
            console.print(f"\n[red]💥 {type(exc).__name__}:[/] {exc}")
            console.print(
                "[dim]conversation saved — fix the issue (e.g. refresh "
                "TALOS_API_KEY in .env) then resume with: talos chat -r latest[/]"
            )
        finally:
            close_stream()
            self.status.stop()

        self.messages.extend(collected)
        save_session(self.session_id, self.messages)

        final = ""
        for msg in reversed(collected):
            if isinstance(msg, AIMessage):
                final = get_message_text(msg)
                break

        # 🧜 mermaid can't render in a terminal — offer the browser instead.
        self.last_mermaid = extract_mermaid(final)
        if self.last_mermaid:
            console.print(
                f"[dim]🧜 {len(self.last_mermaid)} mermaid diagram(s) — "
                "type /mermaid to open in your browser[/]"
            )

        # 📊 per-turn usage footer
        if settings.show_usage and self._turn_usage["total"]:
            t, s = self._turn_usage, self.usage
            console.print(
                f"[dim]📊 ↑{t['input']:,} ↓{t['output']:,} tok"
                f"  ·  session {s['total']:,}[/]"
            )
        return final

    def _track_usage(self, msg: BaseMessage) -> None:
        """📊 Each AIMessage carries normalized usage_metadata; sum it.

        Note: input_tokens counts the WHOLE context every call, so a turn
        with several think→act steps re-bills the conversation prefix each
        step — that's why context discipline (subagents, lazy skills)
        saves real money.
        """
        um = getattr(msg, "usage_metadata", None)
        if not um:
            return
        for ours, theirs in (("input", "input_tokens"),
                             ("output", "output_tokens"),
                             ("total", "total_tokens")):
            amount = um.get(theirs) or 0
            self._turn_usage[ours] += amount
            self.usage[ours] += amount

    # ── 🔧 render tool activity ──────────────────────────────────────────
    def _render_side_effects(self, msg: BaseMessage) -> None:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            print(flush=True)
            for call in msg.tool_calls:
                emoji = TOOL_EMOJI.get(call["name"], "🔧")
                console.print(
                    f"[dim]{emoji} {call['name']}({_args_preview(call['args'])})[/]"
                )
                self.status.set(f"⚙️  running {call['name']}…")
        elif isinstance(msg, ToolMessage):
            lines = get_message_text(msg).strip().splitlines()
            first = lines[0] if lines else ""
            icon = "✗" if first.lower().startswith(("error", "permission denied")) else "✓"
            more = f" (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
            console.print(f"[dim]   ↳ {icon} {first[:120]}{more}[/]")


async def _gather_mcp_tools() -> list:
    try:
        tools = await load_mcp_tools()
    except (RuntimeError, ValueError) as exc:
        console.print(f"[yellow]🔌 MCP: {exc}[/]")
        return []
    if tools:
        console.print(f"[dim]🔌 {len(tools)} MCP tool(s) connected[/]")
    return tools


async def run_once(prompt: str, model: str | None = None, yolo: bool = False) -> None:
    """⚡ One-shot mode: single turn, then exit (good for scripts/pipes).

    Non-interactive runs can't ask for approval, so mutating tools are
    denied unless ``--yolo`` is passed.
    """
    extra = await _gather_mcp_tools()
    await Runtime(model, yolo=yolo, interactive=False, extra_tools=extra).turn(prompt)


async def repl(
    model: str | None = None,
    initial_prompt: str | None = None,
    yolo: bool = False,
    resume: str | None = None,
) -> None:
    """💬 Interactive mode."""
    rt = Runtime(model, yolo=yolo, resume=resume, extra_tools=await _gather_mcp_tools())

    subtitle = "[dim]/exit quits · Ctrl-C interrupts a turn[/]"
    body = (
        f"model [magenta]{model or settings.model}[/] · "
        f"session [cyan]{rt.session_id}[/]"
    )
    if yolo or settings.yolo:
        body += " · [bold red]⚡ yolo[/]"
    if resume and rt.messages:
        body += f"\n[dim]💾 resumed with {len(rt.messages)} messages[/]"
    console.print(Panel(body, title="🤖 talos", subtitle=subtitle, border_style="cyan"))

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

        # ⌨️ slash commands are handled client-side, the model never sees them
        action, payload = dispatch(stripped)
        if action == "builtin":
            if payload == "/exit":
                console.print("[dim]bye 👋[/]")
                break
            _run_builtin(payload, rt)
            continue
        if action == "unknown":
            console.print(f"[red]unknown command {payload}[/] — try /help")
            continue
        prompt_text = payload  # "chat" → raw line, "prompt" → expanded template
        if action == "prompt":
            console.print(f"[dim]⌨️  expanded custom command[/]")

        try:
            await rt.turn(prompt_text)
        except KeyboardInterrupt:
            rt.status.stop()
            console.print("\n[yellow]⏹  turn interrupted[/]")


def _run_builtin(name: str, rt: Runtime) -> None:
    if name == "/help":
        console.print(help_text())
    elif name == "/clear":
        rt.messages = []
        console.print("[dim]🧹 conversation cleared[/]")
    elif name == "/tools":
        for t in get_tools():
            console.print(f"  {TOOL_EMOJI.get(t.name, '🔧')} [cyan]{t.name}[/] — {t.description.splitlines()[0]}")
    elif name == "/memory":
        from talos.memory import load_memory

        console.print(load_memory() or "[dim](memory is empty)[/]")
    elif name == "/usage":
        u = rt.usage
        console.print(
            f"📊 session usage — [cyan]{u['turns']}[/] turn(s) · "
            f"↑ [cyan]{u['input']:,}[/] in · ↓ [cyan]{u['output']:,}[/] out · "
            f"[bold cyan]{u['total']:,}[/] total tokens"
        )
    elif name == "/mermaid":
        if rt.last_mermaid:
            path = open_in_browser(rt.last_mermaid)
            console.print(f"[dim]🧜 opened {path}[/]")
        else:
            console.print("[dim]no mermaid blocks in the last reply[/]")
