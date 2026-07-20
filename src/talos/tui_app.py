"""🖼️ talos tui — the Textual edition (experimental).

Two frontends, one brain. ``talos chat`` is the prompt-plus-stream CLI:
native scrollback, pipe-friendly, minimal magic. ``talos tui`` is this —
a full-screen Textual application that takes over the terminal and gives
back what the stream model can't:

- a real right **sidebar** (model · tokens · cost, always visible)
- a scrollable chat pane with styled user/agent blocks
- **modal dialogs** for permission prompts
- Esc = graceful stop (the same stop_flag the CLI uses)

The point for the learning course: every module this imports
(graph, sessions, models, permissions, context) is UI-free — the same
brain renders two completely different faces. If your rendering layer
can't be swapped, it was never a layer.
"""

import asyncio

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from rich.markdown import Markdown as RichMarkdown
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from talos.config import settings
from talos.context import build_system_prompt
from talos.graph.builder import build_agent_graph
from talos.llm import build_llm
from talos.models import estimate_cost
from talos.permissions import PermissionGate
from talos.sessions import new_session_id, save_session
from talos.tools import get_tools

BRONZE = "#c97f2e"
GOLD = "#ffd75f"


class PermissionScreen(ModalScreen[str]):
    """🛡️ The approval dialog — a real modal instead of an inline prompt."""

    CSS = f"""
    PermissionScreen {{ align: center middle; }}
    #dialog {{ width: 70; max-height: 18; border: round {GOLD};
               background: $surface; padding: 1 2; }}
    #buttons {{ height: 3; align: center middle; }}
    Button {{ margin: 0 2; }}
    """

    def __init__(self, tool_name: str, args_preview: str):
        super().__init__()
        self._title = f"🛡️  allow {tool_name}?"
        self._body = args_preview

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"[bold]{self._title}[/]\n\n{self._body}")
            with Horizontal(id="buttons"):
                yield Button("yes", id="y", variant="success")
                yield Button("always", id="a", variant="warning")
                yield Button("no", id="n", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "n")


class TalosApp(App):
    """The full-screen face of the same agent graph."""

    TITLE = "⚒ talos"
    BINDINGS = [
        ("escape", "stop_turn", "stop (graceful)"),
        ("ctrl+q", "quit", "quit"),
    ]
    CSS = f"""
    #log {{ padding: 0 1; }}
    #sidebar {{ dock: right; width: 26; border-left: solid {BRONZE};
                padding: 1 1; color: $text-muted; }}
    Input {{ dock: bottom; border: tall {BRONZE}; }}
    .user {{ border-left: thick {GOLD}; padding: 0 1; margin: 1 0 0 0; }}
    .agent {{ border-left: thick {BRONZE}; padding: 0 1; margin: 1 0 0 0; }}
    .tool {{ color: $text-muted; }}
    """

    def __init__(self, model: str | None = None, yolo: bool = False):
        super().__init__()
        self.model_name = model or settings.model
        self.session_id = new_session_id()
        self.messages: list[BaseMessage] = []
        self.usage = {"input": 0, "output": 0, "total": 0, "turns": 0}
        self.busy = False
        self.inbox: list[str] = []
        self.stop_flag = asyncio.Event()
        gate = PermissionGate(approver=self._ask_permission, yolo=yolo or settings.yolo)
        self.graph = build_agent_graph(
            llm=build_llm(self.model_name),
            tools=get_tools(),
            system_prompt=build_system_prompt(),
            gate=gate,
            stop_flag=self.stop_flag,
        )

    # ── layout ───────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical():
                yield VerticalScroll(id="log")
                yield Input(placeholder="ask talos…  (Esc stops a running turn)")
            yield Static(id="sidebar")

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self._refresh_sidebar()
        # 🔥 warm /models for provider-accurate costs in the sidebar
        from talos.models import prime_models_cache

        self.run_worker(asyncio.to_thread(prime_models_cache), exclusive=False)

    # ── helpers ──────────────────────────────────────────────────────────
    def _append(self, widget: Static) -> Static:
        log = self.query_one("#log", VerticalScroll)
        log.mount(widget)
        widget.scroll_visible()
        return widget

    def _refresh_sidebar(self) -> None:
        cost = estimate_cost(self.model_name, self.usage["input"], self.usage["output"])
        lines = [
            f"[bold {BRONZE}]⚒ talos[/]",
            "",
            f"[bold]{self.model_name}[/]",
            f"session {self.session_id}",
            "",
            f"turns   {self.usage['turns']}",
            f"↑ in    {self.usage['input']:,}",
            f"↓ out   {self.usage['output']:,}",
            f"total   {self.usage['total']:,}",
            f"cost    {f'${cost:.3f}' if cost is not None else '·'}",
            "",
            "[dim]Esc stop · ^q quit[/]",
        ]
        self.query_one("#sidebar", Static).update("\n".join(lines))

    async def _ask_permission(self, tool_name: str, args: dict) -> str:
        import json

        preview = json.dumps(args, indent=2, default=str)[:500]
        return await self.push_screen_wait(PermissionScreen(tool_name, preview)) or "n"

    # ── events ───────────────────────────────────────────────────────────
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        self.query_one(Input).value = ""
        if self.busy:
            self.inbox.append(text)
            self._append(Static(f"[dim]📨 queued: {text}[/]", classes="tool"))
            return
        self.run_worker(self._turn(text), exclusive=False)

    def action_stop_turn(self) -> None:
        if self.busy:
            self.stop_flag.set()
            self._append(Static("[yellow]🛑 stopping at the next safe point…[/]", classes="tool"))

    # ── the turn loop (same dance as runtime/runner.py, new face) ───────
    async def _turn(self, text: str) -> None:
        self.busy = True
        self.stop_flag.clear()
        self.usage["turns"] += 1
        self._append(Static(f"[bold {GOLD}]→[/] {text}", classes="user"))
        self.messages.append(HumanMessage(content=text))

        agent_block: Static | None = None
        buffer = ""
        collected: list[BaseMessage] = []
        try:
            async for mode, payload in self.graph.astream(
                {"messages": self.messages},
                config={"recursion_limit": settings.max_iterations},
                stream_mode=["messages", "updates"],
            ):
                if mode == "messages":
                    chunk, meta = payload
                    if isinstance(chunk, AIMessageChunk) and meta.get("langgraph_node") == "agent":
                        token = chunk.content if isinstance(chunk.content, str) else ""
                        if token:
                            buffer += token
                            if agent_block is None:
                                agent_block = self._append(Static(classes="agent"))
                            agent_block.update(RichMarkdown(buffer))
                            agent_block.scroll_visible()
                elif mode == "updates":
                    streamed = agent_block is not None
                    agent_block, buffer = None, ""
                    for _node, update in (payload or {}).items():
                        for msg in (update or {}).get("messages", []):
                            collected.append(msg)
                            # non-streaming models deliver the answer here
                            # instead of as chunks — render it once
                            self._track(msg, render_answer=not streamed)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the app
            self._append(Static(f"[red]💥 {type(exc).__name__}: {exc}[/]", classes="tool"))
        finally:
            self.messages.extend(collected)
            save_session(self.session_id, self.messages)
            self.busy = False
            self._refresh_sidebar()

        if self.inbox:
            self.run_worker(self._turn(self.inbox.pop(0)), exclusive=False)

    def _track(self, msg: BaseMessage, render_answer: bool = False) -> None:
        if isinstance(msg, AIMessage):
            if render_answer and not msg.tool_calls:
                text = msg.content if isinstance(msg.content, str) else ""
                if text.strip():
                    self._append(Static(RichMarkdown(text), classes="agent"))
            um = getattr(msg, "usage_metadata", None) or {}
            self.usage["input"] += um.get("input_tokens", 0)
            self.usage["output"] += um.get("output_tokens", 0)
            self.usage["total"] += um.get("total_tokens", 0)
            for call in msg.tool_calls or []:
                self._append(Static(f"🔧 {call['name']}({str(call['args'])[:80]})", classes="tool"))
            self._refresh_sidebar()
        elif isinstance(msg, ToolMessage):
            first = str(msg.content).strip().splitlines()[0][:100] if msg.content else ""
            self._append(Static(f"   ↳ {first}", classes="tool"))


def run_tui(model: str | None = None, yolo: bool = False) -> None:
    TalosApp(model=model, yolo=yolo).run()
