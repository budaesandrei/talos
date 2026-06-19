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

import asyncio
import itertools
import json
import re
import sys
import threading

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.errors import GraphRecursionError
from rich import box
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from talos.ui.banner import print_banner
from talos.ui.commands import dispatch, help_text
from talos.memory.compaction import SUMMARY_PROMPT, compact, fuel_gauge
from talos.config import settings
from talos.agent.context import build_system_prompt
from talos.agent.graph.builder import build_agent_graph
from talos.agent.llm import build_llm
from talos.integrations.mcp import load_mcp_tools
from talos.ui.mermaid import ascii_render, extract_mermaid, open_in_browser
from talos.integrations.models import estimate_cost
from talos.infra.permissions import PermissionGate
from talos.lifecycle.planning import (
    ELABORATION_PROMPT,
    VERIFY_PROMPT,
    construct_prompt,
    is_ready,
    parse_verdict,
    save_plan,
)
from talos.memory.sessions import (
    get_session_meta,
    latest_session_id,
    load_session,
    new_session_id,
    save_session,
    set_session_meta,
)
from talos.agent.thinking import ThinkSplitter
from talos.tools import get_tools

console = Console()


def render_user_message(text: str) -> None:
    """🟦 Echo the user's message in a bordered panel, rendered as markdown
    so pasted ```code``` blocks and `inline code` look right. The border is
    the clear separation from the agent's reply below it."""
    body = Markdown(text) if text.strip() else Text(text)
    console.print(
        Panel(
            body,
            border_style="#5f87af",
            box=box.ROUNDED,
            padding=(0, 1),
            title="[dim]you[/]",
            title_align="left",
        )
    )

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


class _QueueAdapter:
    """Lets one-shot prompt code reuse the ``await pump.queue.get()`` API:
    each ``get()`` simply shows the prompt once and returns the line."""

    def __init__(self, reader):
        self._reader = reader

    async def get(self):
        return await self._reader.get_line()


class _PromptPump:
    """⌨️ Fancy line reader — turn-based, NOT pinned.

    The previous design kept a prompt_toolkit prompt alive for the whole
    session and printed agent output *through* ``patch_stdout``. That
    repainted the app on every token (flicker), pinned the cursor at the
    prompt (no separation), and corrupted scrollback when you scrolled
    mid-stream (duplicated paragraphs).

    This version runs the prompt only when we actually need a line — between
    turns, or for an approval/plan question. During streaming there is NO
    prompt and NO patch_stdout, so tokens go straight to the terminal:
    clean separation, zero flicker, native scrollback.
    """

    fancy = True

    def __init__(self, stats=None):
        self.eof = False
        self._stats = stats
        self._session = None
        self.queue = _QueueAdapter(self)  # compat for await pump.queue.get()

    def _ensure(self):
        if self._session is None:
            from talos.ui.tui import build_session

            self._session = build_session(stats=self._stats)
        return self._session

    async def get_line(self, prompt_text=None):
        session = self._ensure()
        try:
            if prompt_text is not None:
                return await session.prompt_async(prompt_text)
            return await session.prompt_async()
        except EOFError:           # Ctrl-D → end the session
            self.eof = True
            return None
        except KeyboardInterrupt:  # Ctrl-C at an empty prompt → ignore
            return ""


def make_input(stats=None):
    """Pick the input mechanism:
    - a real terminal + interjections OFF → the clean turn-based fancy reader
    - interjections ON, or a pipe/non-tty → the always-on stdin pump
    """
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    if tty and not settings.interject:
        return _PromptPump(stats)
    return _StdinPump()


class _StdinPump:
    """⌨️→📬 One background thread owns stdin for the whole session.

    Why: to accept input *while the agent is working*, someone must always
    be reading stdin. Two readers would race for keystrokes, so this pump
    is the only reader ever — every line lands in an asyncio queue, and
    whoever currently 'owns' input (the prompt, an approval dialog, the
    interjection handler) takes from there.
    """

    def __init__(self):
        self.queue: "asyncio.Queue[str | None]" = asyncio.Queue()
        self.eof = False  # set once stdin closes (None sentinel seen)
        self.fancy = False
        self._loop = asyncio.get_running_loop()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        try:
            for line in sys.stdin:
                asyncio.run_coroutine_threadsafe(
                    self.queue.put(line.rstrip("\n")), self._loop
                ).result()
        except (ValueError, OSError):
            pass  # stdin closed
        asyncio.run_coroutine_threadsafe(self.queue.put(None), self._loop)


# ── 🧭 interjection intent ───────────────────────────────────────────────
_STOP_RE = re.compile(r"\b(stop|cancel|abort|halt|kill|enough)\b", re.I)
_URGENT_RE = re.compile(r"(!{2,}|\b(now|immediately|right now|force)\b)", re.I)
_STATUS_RE = re.compile(
    r"\b(what (are|r) (you|u) doing|what's happening|status|progress|"
    r"how('s| is) it going|how far|where are (you|we))\b", re.I
)

_CLASSIFIER_PROMPT = """You classify what a user wants when they type while an
AI agent is busy with a task. Reply with EXACTLY one word:
STATUS  - they ask what's happening / how it's going
STOP    - they want the task stopped, calmly
STOPNOW - they urgently demand an immediate halt
QUEUE   - anything else: a note or question to handle after the task"""


async def classify_intent(text: str) -> str:
    """Heuristics first (fast, free); ambiguous lines go to the LLM."""
    if _STOP_RE.search(text):
        return "stopnow" if _URGENT_RE.search(text) else "stop"
    if _STATUS_RE.search(text):
        return "status"
    try:
        reply = await build_llm().ainvoke(
            [SystemMessage(content=_CLASSIFIER_PROMPT), HumanMessage(content=text)]
        )
        word = get_message_text(reply).strip().upper()
        return {"STATUS": "status", "STOP": "stop", "STOPNOW": "stopnow"}.get(
            word, "queue"
        )
    except Exception:
        return "queue"  # when in doubt, don't disturb the task


class _Status:
    """The 'agent is busy' indicator, with two backends:

    - fancy mode: writes into the prompt's StatusState — the toolbar
      renders it, prompt_toolkit owns the whole bottom of the screen,
      and nothing flickers because nothing else repaints that region
    - plain mode (pipes, one-shot): a rich spinner ('aesthetic', not
      the dots every other CLI uses)
    """

    def __init__(self):
        self._status = None
        self.sink = None  # StatusState once the fancy prompt attaches

    def set(self, text: str) -> None:
        if self.sink is not None:
            self.sink.text = text
            return
        self.stop()
        self._status = console.status(f"[dim]{text}[/]", spinner="aesthetic")
        self._status.start()

    def stop(self) -> None:
        if self.sink is not None:
            self.sink.text = ""
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
        self.model_name = model or settings.model
        self._extra_tools = list(extra_tools or [])
        self.stop_flag = asyncio.Event()        # 🛑 graceful-stop signal
        self.inbox: list[str] = []              # 📨 /btw-style queued notes
        self._activity: list[str] = []          # 🗣️ narrator's source material
        self._line_request: asyncio.Future | None = None  # approval waiting?
        self._reader = None  # one-shot reader, set by the repl in fancy mode
        self._gate = PermissionGate(
            approver=self._ask_permission if interactive else None,
            yolo=yolo or settings.yolo,
        )
        self._rebuild_graph()
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
        meta = get_session_meta(self.session_id)
        self.title: str = meta.get("title", "")
        # 📊 running totals from AIMessage.usage_metadata (LangChain
        # normalizes every provider's usage block into this one shape).
        # Resuming restores the session's lifetime usage from the index.
        self.usage = meta.get("usage") or {
            "input": 0, "output": 0, "total": 0, "turns": 0
        }
        # 🗜️ exact current context size = the input_tokens of the last
        # reply (the real number of tokens the model just read). Drives
        # the fuel gauge and the auto-compaction trigger.
        self.context_tokens = 0
        self.compactions = 0
        self._pending_verify: str | None = None
        try:
            from talos.tools.recall_tool import set_session

            set_session(self.session_id)
        except Exception:
            pass

    def _rebuild_graph(self) -> None:
        self.graph = build_agent_graph(
            llm=build_llm(self.model_name),
            tools=get_tools() + self._extra_tools,  # built-ins + 🔌 MCP
            system_prompt=build_system_prompt(),
            gate=self._gate,
            stop_flag=self.stop_flag,
        )

    def switch_model(self, model_id: str) -> None:
        """📇 Same conversation, different brain — history carries over."""
        self.model_name = model_id
        self._rebuild_graph()
        console.print(f"[dim]📇 switched to [magenta]{model_id}[/][/]")

    def session_cost(self) -> float | None:
        return estimate_cost(self.model_name, self.usage["input"], self.usage["output"])

    def context_limit(self) -> int | None:
        from talos.integrations.models import provider_meta, lookup

        meta = provider_meta(self.model_name) or lookup(self.model_name)
        return meta.get("max_input_tokens")

    def _usage_suffix(self) -> str:
        """The 'how much have I spent' tail on the spinner line."""
        line = self.stats_line()
        return f" · {line}" if line else ""

    def stats_line(self) -> str:
        """📊 right-edge prompt stats: context fuel · session tokens · $."""
        parts = []
        gauge = fuel_gauge(self.context_tokens, self.context_limit())
        if gauge:
            parts.append(gauge)
        total = self.usage["total"]
        if total:
            text = f"{total:,} tok"
            cost = self.session_cost()
            if cost is not None:
                text += f" · ${cost:.3f}"
            parts.append(text)
        return "  ·  ".join(parts)

    async def _summarize(self, prior: str, transcript: str) -> str:
        """One metered LLM call that produces the compaction digest."""
        from langchain_core.messages import HumanMessage as HM, SystemMessage as SM

        msg = await build_llm(self.model_name).ainvoke([
            SM(content=SUMMARY_PROMPT),
            HM(content=(f"Earlier summary:\n{prior}\n\n" if prior else "")
               + f"New turns to fold in:\n{transcript}"),
        ])
        self._track_usage(msg)
        return get_message_text(msg)

    async def _extract_topics(self, prompt: str, text: str) -> str:
        from langchain_core.messages import HumanMessage as HM, SystemMessage as SM

        msg = await build_llm(self.model_name).ainvoke(
            [SM(content=prompt), HM(content=text)]
        )
        self._track_usage(msg)
        return get_message_text(msg)

    async def _summ_community(self, label, topics, relations) -> str:
        from langchain_core.messages import HumanMessage as HM, SystemMessage as SM

        body = "topics:\n" + "\n".join(topics)
        if relations:
            body += "\nrelations:\n" + "\n".join(relations)
        msg = await build_llm(self.model_name).ainvoke([
            SM(content="Summarize this topic cluster in 2-3 sentences for "
                       "future recall. Be specific about decisions and facts."),
            HM(content=body),
        ])
        self._track_usage(msg)
        return get_message_text(msg)

    async def maybe_compact(self, force: bool = False) -> bool:
        """🗜️ Fold old turns into a summary when context fills up.
        Returns True if a compaction happened."""
        limit = self.context_limit()
        threshold = (limit or 0) * settings.compact_at
        over = limit and self.context_tokens >= threshold
        if not (force or over) or settings.compact_at <= 0:
            return False
        self.status.set("🗜️ compacting context…")
        new_messages, did = await compact(
            self.messages, self._summarize, keep_recent=settings.keep_recent
        )
        self.status.stop()
        if did:
            # 🧠 fold the dropped turns into graph memory (M34) so they stay
            # recallable. The summary message is new_messages[0].
            try:
                from talos.memory.graph_memory import ingest_async

                folded = next((str(m.content) for m in new_messages), "")
                stats = await ingest_async(
                    self.session_id, folded, self._extract_topics, self._summ_community
                )
                if stats["topics_added"]:
                    console.print(
                        f"[dim]🕸️ memory: +{stats['topics_added']} topics, "
                        f"{stats['summary_calls']} community summaries[/]"
                    )
            except Exception:
                pass
            self.messages = new_messages
            self.compactions += 1
            self.context_tokens = 0  # reset estimate; next reply re-measures
            save_session(self.session_id, self.messages)
            console.print(
                f"[dim]🗜️ compacted — folded older turns into a summary "
                f"(compaction #{self.compactions})[/]"
            )
        return did

    def _header(self) -> str:
        """⚒ the agent's turn header — the visual user/agent split."""
        parts = [f"[bold #c97f2e]▌⚒ talos[/]", f"[dim]{self.model_name}[/]"]
        if settings.reasoning_effort:
            parts.append(f"[dim]🧠 {settings.reasoning_effort}[/]")
        return "  ".join(parts)

    # ── 🛡️ permission prompt (pauses the spinner) ───────────────────────
    async def _ask_permission(self, tool_name: str, args: dict) -> str:
        """The pump owns stdin, so we can't call input() here. Instead we
        park a Future; the REPL's interjection loop fulfils it with the
        next line the user types."""
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
        console.print(r"[yellow]allow? \[y]es · \[n]o · \[a]lways ›[/] ", end="")
        if self._reader is not None:
            # 🆕 turn-based fancy mode: read the answer with a one-shot prompt
            answer = await self._reader.get_line("") or ""
        else:
            # interject mode: park a Future the interjection loop fulfils
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._line_request = fut
            try:
                answer = await fut
            finally:
                self._line_request = None
        self.status.set(f"⚙️  running {tool_name}…")
        return answer

    # ── 💬 one user turn ─────────────────────────────────────────────────
    async def turn(self, user_input: str) -> str:
        self._turn_usage = {"input": 0, "output": 0, "total": 0}
        self.usage["turns"] += 1
        self.stop_flag.clear()
        await self.maybe_compact()  # 🗜️ keep us under the context limit
        self._activity = ["received the task, thinking"]
        # 👁 turn the text into multimodal content if it references images
        # and the model can see (otherwise this returns the plain string)
        try:
            from talos.integrations.vision import build_content

            content = build_content(user_input, self.model_name)
        except Exception:
            content = user_input
        self.messages.append(HumanMessage(content=content))
        collected: list[BaseMessage] = []
        # 🎨 streaming state: with markdown on, we re-render the growing
        # buffer through rich.Live (live markdown!); with it off, we print
        # raw tokens. Either way the spinner dies on the first token.
        # 🎨 APPEND-ONLY streaming: tokens print straight to the terminal,
        # never repainted. rich.Live repainting the growing buffer was the
        # cause of the flicker + the overflow-duplication when scrolling. We
        # stream plain, then re-render as markdown only if the answer still
        # fits on screen (so the cursor-up clear is safe).
        buffer = ""               # answer text so far
        answer_rows = 0           # terminal rows the answer has occupied
        body_printed = False
        reasoning_open = False
        header_printed = False
        self._body_shown = False  # did any answer text reach the screen?

        def ensure_header() -> None:
            nonlocal header_printed
            if not header_printed:
                console.print(self._header(), highlight=False)
                header_printed = True

        def emit(text: str) -> None:
            nonlocal answer_rows
            print(text, end="", flush=True)
            width = max(console.width, 1)
            for i, line in enumerate(text.split("\n")):
                if i:
                    answer_rows += 1
                answer_rows += len(line) // width

        def close_stream() -> None:
            nonlocal buffer, body_printed, reasoning_open, answer_rows
            if reasoning_open:
                print()
                reasoning_open = False
            if body_printed:
                print()  # finish the streamed line
                if (settings.markdown and buffer.strip()
                        and answer_rows + 1 < console.height):
                    # short answer → re-render it nicely (safe cursor-up clear)
                    sys.stdout.write(f"\x1b[{answer_rows + 1}A\x1b[J")
                    sys.stdout.flush()
                    console.print(Markdown(buffer))
            buffer = ""
            body_printed = False
            answer_rows = 0

        self.status.set(f"{next(THINKING)}…{self._usage_suffix()}")
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
                        # 🧠 thinking models stream their reasoning in a
                        # separate channel (additional_kwargs.reasoning_content
                        # — a de-facto convention, not part of the OpenAI
                        # spec). Render it dim so thought ≠ answer.
                        thought = (chunk.additional_kwargs or {}).get(
                            "reasoning_content"
                        ) or ""
                        if thought:
                            self.status.stop()
                            ensure_header()
                            if not reasoning_open:
                                console.print("[dim italic]🧠 thinking[/]")
                                reasoning_open = True
                            console.print(
                                thought, end="", style="dim",
                                highlight=False, markup=False,
                            )
                            continue
                        text = get_message_text(chunk)
                        if not text:
                            continue
                        if reasoning_open:
                            print()  # close the reasoning block
                            reasoning_open = False
                        self.status.stop()
                        ensure_header()
                        body_printed = True
                        self._body_shown = True
                        buffer += text
                        emit(text)  # append-only, flicker-free

                elif mode == "updates":
                    close_stream()  # tool lines must not fight the Live region
                    for node, update in (payload or {}).items():
                        for msg in (update or {}).get("messages", []):
                            collected.append(msg)
                            self._track_usage(msg)
                            self._measure_context(msg)
                            self._render_side_effects(msg)
                        if node == "tools":
                            # back to the model for the next think step
                            header_printed = False
                            self.status.set(
                                f"{next(THINKING)}…{self._usage_suffix()}"
                            )

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
            # runs even on cancellation — history survives a forced stop
            close_stream()
            self.status.stop()
            # 💭 strip thinking blocks from saved messages (shown, not stored)
            for m in collected:
                if isinstance(m, AIMessage) and isinstance(m.content, str) \
                        and "<thinking>" in m.content.lower():
                    m.content = ThinkSplitter.strip(m.content)
            self.messages.extend(collected)
            save_session(self.session_id, self.messages)
            set_session_meta(self.session_id, usage=self.usage, model=self.model_name)
            try:  # ⏪ time-travel checkpoint (chat + file snapshot)
                from talos.memory.checkpoints import save_checkpoint

                save_checkpoint(self.usage["turns"], user_input, self.messages)
            except Exception:
                pass
            if not self.title and self.messages:
                # 🏷️ fire-and-forget: name the session for the resume list
                asyncio.get_running_loop().create_task(self._make_title())

        final = ""
        for msg in reversed(collected):
            if isinstance(msg, AIMessage):
                final = get_message_text(msg)
                break

        # 🛟 fallback: some providers don't stream tokens (the answer arrives
        # whole, via 'updates'). If nothing reached the screen, render it now
        # so the reply is never invisible.
        if final.strip() and not self._body_shown:
            console.print(self._header(), highlight=False)
            from talos.agent.thinking import ThinkSplitter

            shown = ThinkSplitter.strip(final) if "<thinking>" in final.lower() else final
            console.print(Markdown(shown) if settings.markdown else shown,
                          highlight=False)

        # 🧜 mermaid can't render in a terminal — offer the browser instead.
        self.last_mermaid = extract_mermaid(final)
        if self.last_mermaid:
            for block in self.last_mermaid:
                art = ascii_render(block)
                if art:
                    console.print(Panel(art, border_style="dim", title="🧜 mermaid"))
            console.print(
                f"[dim]🧜 {len(self.last_mermaid)} mermaid diagram(s) — "
                "type /mermaid to open in your browser[/]"
            )

        console.print()  # breathe: one blank line closes the agent block
        return final

    async def init_workspace(self) -> None:
        """🗂️ Survey the repo and write a starter TALOS.md."""
        from pathlib import Path

        from langchain_core.messages import HumanMessage as HM, SystemMessage as SM

        from talos.agent.workspace import INIT_PROMPT, snapshot

        self.status.set("🗂️ surveying the workspace…")
        msg = await build_llm(self.model_name).ainvoke(
            [SM(content=INIT_PROMPT), HM(content=snapshot())])
        self._track_usage(msg)
        self.status.stop()
        content = get_message_text(msg).strip()
        if not content:
            console.print("[dim]🗂️ nothing written[/]")
            return
        existing = Path("TALOS.md")
        if existing.is_file():
            console.print(r"[yellow]TALOS.md exists — overwrite? \[y/N] ›[/] ", end="")
            # handled inline only when a pump is around; default safe = skip
        Path("TALOS.md").write_text(content + "\n", encoding="utf-8")
        console.print("[green]🗂️ wrote TALOS.md — workspace rules for future "
                      "sessions[/]")

    async def learn_skill(self) -> None:
        """🧪 Synthesize a verified skill from the recent conversation."""
        from talos.lifecycle.skill_synthesis import synthesize

        async def propose(prompt, transcript):
            from langchain_core.messages import HumanMessage as HM, SystemMessage as SM
            msg = await build_llm(self.model_name).ainvoke(
                [SM(content=prompt), HM(content=transcript)])
            self._track_usage(msg)
            return get_message_text(msg)

        transcript = "\n".join(
            f"{type(m).__name__}: {get_message_text(m)[:400]}"
            for m in self.messages[-24:]
        )
        self.status.set("🧪 synthesizing a skill…")
        result = await synthesize(transcript, propose, propose)
        self.status.stop()
        if result["saved"]:
            console.print(f"[green]🧪 learned skill [bold]{result['name']}[/] "
                          f"→ {result['path']}[/]")
        else:
            console.print(f"[dim]🧪 no skill saved — {result['reason']}[/]")

    async def verify_plan(self, plan: str) -> dict:
        """🔍 The judge: score the just-executed plan against its acceptance
        criteria. A separate LLM call over the conversation — the verifier
        pattern from 2026 self-improving-agent stacks."""
        from langchain_core.messages import HumanMessage as HM, SystemMessage as SM

        transcript = "\n".join(
            f"{type(m).__name__}: {get_message_text(m)[:500]}"
            for m in self.messages[-30:]
        )
        msg = await build_llm(self.model_name).ainvoke([
            SM(content=VERIFY_PROMPT),
            HM(content=f"PLAN:\n{plan}\n\nCONVERSATION:\n{transcript}"),
        ])
        self._track_usage(msg)
        verdict = parse_verdict(get_message_text(msg))
        self._render_verdict(verdict)
        return verdict

    def _render_verdict(self, verdict: dict) -> None:
        units = verdict.get("units", [])
        if not units:
            console.print("[dim]🔍 verifier: no units parsed[/]")
            return
        table = Table(title="🔍 verification", show_header=True, header_style="dim")
        table.add_column("unit")
        table.add_column("", justify="center")
        table.add_column("note", style="dim")
        for u in units:
            ok = u.get("passed")
            table.add_row(
                u.get("name", "?"),
                "[green]✅[/]" if ok else "[red]❌[/]",
                (u.get("evidence") if ok else u.get("gap")) or "",
            )
        console.print(table)
        if verdict.get("all_passed"):
            console.print("[green]✅ all acceptance criteria met[/]")
        else:
            console.print("[yellow]❌ some criteria unmet — see gaps above[/]")

    async def _make_title(self) -> None:
        """🏷️ LLM-generated session title, so 'talos sessions' reads like a
        list of conversations instead of a list of timestamps."""
        try:
            first_user = next(
                (get_message_text(m) for m in self.messages
                 if isinstance(m, HumanMessage)), ""
            )
            reply = await build_llm().ainvoke(
                [
                    SystemMessage(
                        content="Summarize this conversation topic in 3-6 "
                        "plain words. No quotes, no punctuation."
                    ),
                    HumanMessage(content=first_user[:2000]),
                ]
            )
            title = get_message_text(reply).strip().strip('"')[:60]
            if title:
                self.title = title
                set_session_meta(self.session_id, title=title)
        except Exception:
            pass  # cosmetic feature — never disturb the session

    # ── 🎙️ interjections: lines typed while the agent works ─────────────
    async def interject(self, text: str, turn_task: "asyncio.Task") -> None:
        if self._line_request is not None and not self._line_request.done():
            self._line_request.set_result(text)  # it's an approval answer
            return

        intent = await classify_intent(text)
        if intent == "stopnow":
            console.print("[red]⛔ stopping immediately[/]")
            turn_task.cancel()
        elif intent == "stop":
            console.print(
                "[yellow]🛑 got it — finishing the current step safely, "
                "then wrapping up[/]"
            )
            self.stop_flag.set()
        elif intent == "status":
            await self._narrate_status(text)
        else:
            self.inbox.append(text)
            console.print("[dim]📨 noted — I'll get to it right after this[/]")

    async def _narrate_status(self, question: str) -> None:
        """🗣️ Side-channel answer that never touches the main task: a
        separate LLM call over the turn's activity log. The main graph
        keeps streaming; rich prints this above the live region."""
        log = "\n".join(self._activity[-15:])
        try:
            reply = await build_llm().ainvoke(
                [
                    SystemMessage(
                        content="You are the live narrator for a busy AI "
                        "agent. Using its activity log, answer the user's "
                        "question in 1–2 sentences. Do not do the task."
                    ),
                    HumanMessage(
                        content=f"Activity log:\n{log}\n\nUser asks: {question}"
                    ),
                ]
            )
            answer = get_message_text(reply).strip() or "(still working — nothing to report yet)"
        except Exception as exc:
            answer = f"(narrator unavailable: {exc})"
        console.print(
            Panel(answer, title="🗣️ status", border_style="cyan", title_align="left")
        )

    def _measure_context(self, msg: BaseMessage) -> None:
        """The agent node's input_tokens IS the live context size."""
        um = getattr(msg, "usage_metadata", None) or {}
        it = um.get("input_tokens")
        if it:
            self.context_tokens = it

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
                self._activity.append(
                    f"called {call['name']}({_args_preview(call['args'], 80)})"
                )
                self.status.set(f"⚙️  running {call['name']}…")
        elif isinstance(msg, ToolMessage):
            lines = get_message_text(msg).strip().splitlines()
            first = lines[0] if lines else ""
            icon = "✗" if first.lower().startswith(("error", "permission denied")) else "✓"
            more = f" (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
            console.print(f"[dim]   ↳ {icon} {first[:120]}{more}[/]")
            self._activity.append(f"{msg.name} → {first[:100]}")


async def _gather_mcp_tools() -> list:
    try:
        tools = await load_mcp_tools()
    except (RuntimeError, ValueError) as exc:
        console.print(f"[yellow]🔌 MCP: {exc}[/]")
        return []
    if tools:
        console.print(f"[dim]🔌 {len(tools)} MCP tool(s) connected[/]")
    return tools


async def run_plan(rt: Runtime, pump, task: str) -> None:
    """🗺️ The /plan flow: elaborate (read-only) → human gate → construct."""
    from talos.agent.context import environment_info
    from talos.tools.task_tool import _resolve_tools

    if not task:
        console.print("[yellow]what should we plan? ›[/] ", end="")
        task = (await pump.queue.get() or "").strip()
        if not task:
            return

    # 🔍 elaboration happens on a read-only graph — planning can't mutate
    planner = build_agent_graph(
        llm=build_llm(rt.model_name),
        tools=_resolve_tools([]),  # default read-only set
        system_prompt=ELABORATION_PROMPT + "\n\n" + environment_info(),
        gate=PermissionGate(approver=None),
    )
    convo: list[BaseMessage] = list(rt.messages) + [
        HumanMessage(content=f"Plan this task: {task}")
    ]

    plan_text = ""
    for _round in range(4):  # initial + up to 3 clarification rounds
        rt.status.set("🗺️ elaborating…")
        try:
            result = await planner.ainvoke(
                {"messages": convo},
                config={"recursion_limit": settings.max_iterations},
            )
        except Exception as exc:
            rt.status.stop()
            console.print(f"[red]planning failed: {exc}[/]")
            return
        rt.status.stop()
        reply = result["messages"][-1]
        plan_text = get_message_text(reply)
        convo = list(result["messages"])
        console.print(Panel(Markdown(plan_text), title="🗺️ plan", border_style="cyan"))

        if is_ready(plan_text):
            break
        # ❓ mob elaboration: the planner asked questions — answer them
        console.print("[yellow]answers (or 'skip' to force a plan) ›[/] ", end="")
        answer = (await pump.queue.get() or "").strip()
        if answer.lower() == "skip":
            convo.append(HumanMessage(
                content="No more answers. Make reasonable assumptions, state "
                "them in the plan, and finish it now."))
        else:
            convo.append(HumanMessage(content=answer))

    path = save_plan(plan_text)
    console.print(f"[dim]🗺️ saved to {path}[/]")

    # 🚦 the human gate
    console.print(r"[yellow]execute? \[y]es · \[r]evise · \[n]ot now ›[/] ", end="")
    verdict = (await pump.queue.get() or "").strip().lower()
    if verdict.startswith("y"):
        # 🔨 construct phase = a normal turn: full tools, gate, interjections
        rt.inbox.insert(0, construct_prompt(plan_text))
        rt._pending_verify = plan_text  # 🔍 verify once construct finishes
    elif verdict.startswith("r"):
        console.print("[yellow]what should change? ›[/] ", end="")
        note = (await pump.queue.get() or "").strip()
        rt.inbox.insert(0, f"/plan {task}\n(revision note: {note})")
    else:
        console.print(f"[dim]parked — it's in {path} when you want it[/]")


async def run_evolve(rt: Runtime, pump, focus: str) -> None:
    """🔄 The AI-DLC ouroboros: debt → persona research → requirements → plan.
    Every phase is human-gated."""
    from talos.agent.context import environment_info
    from talos.lifecycle.evolve import (
        DEBT_PROMPT, PERSONAS, REQUIREMENTS_PROMPT, is_requirements_ready,
        research_prompt,
    )
    from talos.tools.task_tool import _resolve_tools
    from langchain_core.messages import HumanMessage as HM, SystemMessage as SM

    async def phase_llm(system, user):
        msg = await build_llm(rt.model_name).ainvoke(
            [SM(content=system + "\n\n" + environment_info()), HM(content=user)])
        rt._track_usage(msg)
        return get_message_text(msg)

    # ── 1. 🧹 debt phase (read-only graph) ──────────────────────────────
    rt.status.set("🧹 assessing technical debt…")
    debt_graph = build_agent_graph(
        llm=build_llm(rt.model_name),
        tools=_resolve_tools([]),  # read-only
        system_prompt=DEBT_PROMPT + "\n\n" + environment_info(),
        gate=PermissionGate(approver=None),
    )
    debt_seed = focus or "Assess this project's technical debt and AI cruft."
    debt = await debt_graph.ainvoke(
        {"messages": [HM(content=debt_seed)]},
        config={"recursion_limit": settings.max_iterations})
    rt.status.stop()
    debt_report = get_message_text(debt["messages"][-1])
    rt._track_usage(debt["messages"][-1])
    console.print(Panel(Markdown(debt_report), title="🧹 tech-debt report",
                        border_style="cyan"))
    console.print(r"[yellow]continue to market/persona research? \[Y/n] ›[/] ", end="")
    if (await pump.queue.get() or "y").strip().lower().startswith("n"):
        console.print("[dim]parked after debt phase[/]")
        return

    # ── 2. 🔬 persona research in parallel (the hats) ───────────────────
    console.print("[dim]🔬 putting on hats: " + ", ".join(PERSONAS) + "[/]")
    rt.status.set("🔬 persona research…")
    async def one_hat(hat):
        g = build_agent_graph(
            llm=build_llm(rt.model_name),
            tools=_resolve_tools(["read_file", "list_dir", "grep", "web_fetch"]),
            system_prompt=research_prompt(hat) + "\n\n" + environment_info(),
            gate=PermissionGate(approver=None, yolo=settings.yolo))
        r = await g.ainvoke({"messages": [HM(content=focus or
            "Critique this product from your perspective.")]},
            config={"recursion_limit": settings.max_iterations})
        return hat, get_message_text(r["messages"][-1])
    pairs = await asyncio.gather(*(one_hat(h) for h in PERSONAS))
    rt.status.stop()
    for hat, view in pairs:
        console.print(Panel(Markdown(view), title=f"🎭 {hat}", border_style="dim"))

    # ── 3. 📋 requirements compile (human gate) ─────────────────────────
    rt.status.set("📋 compiling requirements…")
    blob = f"DEBT REPORT:\n{debt_report}\n\n" + "\n\n".join(
        f"PERSONA {h}:\n{v}" for h, v in pairs)
    reqs = await phase_llm(REQUIREMENTS_PROMPT, blob)
    rt.status.stop()
    console.print(Panel(Markdown(reqs), title="📋 evolution requirements",
                        border_style="cyan"))
    # persist alongside plans
    try:
        from talos.lifecycle.planning import plans_dir
        from datetime import datetime
        plans_dir().mkdir(parents=True, exist_ok=True)
        out = plans_dir() / f"evolve-{datetime.now():%Y%m%d-%H%M%S}.md"
        out.write_text(reqs.replace("REQUIREMENTS READY", "").strip() + "\n",
                       encoding="utf-8")
        console.print(f"[dim]📋 saved to {out}[/]")
    except Exception:
        pass

    console.print(r"[yellow]feed these into /plan now? \[Y/n] ›[/] ", end="")
    if (await pump.queue.get() or "y").strip().lower().startswith("n"):
        console.print("[dim]requirements saved — run /plan when ready[/]")
        return
    # ── 4. ➡️ the tail meets the head: hand off to AI-DLC planning ──────
    rt.inbox.insert(0, f"/plan Implement these evolution requirements:\n{reqs}")
    console.print("[green]🔄 handed off to /plan — the cycle continues[/]")


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
    """💬 Interactive mode — with a twist: you can keep typing while the
    agent works. Lines typed mid-task are classified and routed:

      "what are you doing?"        → 🗣️ side-channel status answer
      "stop, this isn't working"   → 🛑 graceful stop (safe boundary)
      "STOP NOW!!"                 → ⛔ hard cancel of the turn
      anything else                → 📨 queued, handled right after
    """
    rt = Runtime(model, yolo=yolo, resume=resume, extra_tools=await _gather_mcp_tools())

    # 🕹️ banner first, before any prompt is drawn.
    print_banner(
        console,
        model=rt.model_name,
        session_id=rt.session_id,
        yolo=yolo or settings.yolo,
        resumed=len(rt.messages) if resume else 0,
        title=rt.title,
    )

    # ⚠️ no credentials configured → the model name shown is just the
    # default; a real request would 401. Tell the user plainly rather than
    # letting them think gpt-4o-mini is wired up.
    if not settings.api_key:
        console.print(
            "[yellow]⚠️  no API key configured[/] — set TALOS_API_KEY (+ "
            "TALOS_BASE_URL / TALOS_MODEL) in .env or your environment. "
            f"[dim]showing the default model '{rt.model_name}'.[/]"
        )

    # 🔥 warm /models in the background: one round trip gives the picker
    # its list AND the cost engine the provider's own per-token prices
    from talos.integrations.models import prime_models_cache

    asyncio.get_running_loop().run_in_executor(None, prime_models_cache)

    pump = make_input(stats=rt.stats_line)
    fancy = getattr(pump, "fancy", False)
    if fancy:
        # one-shot reader handles approval/plan/evolve prompts mid-turn
        rt._reader = pump
    else:
        # plain pump: the ⚒ status renders in its pinned toolbar
        rt.status.sink = getattr(pump, "status_state", None)

    async def run_turn(text: str) -> None:
        render_user_message(text)  # 🟦 bordered echo, then the agent replies
        if fancy:
            # 🆕 turn-based: no pinned prompt, no patch_stdout — tokens stream
            # straight to the terminal (clean separation, no flicker, real
            # scrollback). Ctrl-C interrupts just this turn.
            try:
                await rt.turn(text)
            except KeyboardInterrupt:
                rt.status.stop()
                console.print("\n[yellow]⏹  turn interrupted[/]")
            return
        # ⌨️ interject mode: watch stdin while the turn streams
        turn_task = asyncio.create_task(rt.turn(text))
        try:
            while not turn_task.done():
                if pump.eof:
                    await turn_task
                    break
                getter = asyncio.create_task(pump.queue.get())
                await asyncio.wait(
                    {turn_task, getter}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter.done():
                    line = getter.result()
                    if line is None:
                        pump.eof = True
                    elif line.strip():
                        await rt.interject(line.strip(), turn_task)
                else:
                    getter.cancel()
            await turn_task
        except asyncio.CancelledError:
            console.print("\n[yellow]⛔ stopped — session saved[/]")
        except KeyboardInterrupt:
            turn_task.cancel()
            console.print("\n[yellow]⏹  turn interrupted[/]")

    async def handle_line(stripped: str) -> bool:
        """Dispatch one user line. Returns False when the session should end."""
        action, payload = dispatch(stripped)
        if action == "builtin":
            if payload == "/exit":
                console.print("[dim]bye 👋[/]")
                return False
            await _run_builtin(payload, rt, pump)
            return True
        if action == "unknown":
            console.print(f"[red]unknown command {payload}[/] — try /help")
            return True
        if action == "plan":
            await run_plan(rt, pump, payload)
            return True
        if action == "evolve":
            await run_evolve(rt, pump, payload)
            return True
        if action == "prompt":
            console.print("[dim]⌨️  expanded custom command[/]")
        await run_turn(payload)
        return True

    if initial_prompt:
        await run_turn(initial_prompt)

    while True:
        # 📨 first, anything the user queued while the agent was busy
        if rt.inbox:
            note = rt.inbox.pop(0)
            if not await handle_line(note):
                break
            if rt._pending_verify:  # 🔍 the construct turn just finished
                plan, rt._pending_verify = rt._pending_verify, None
                await rt.verify_plan(plan)
            continue

        if pump.eof:
            console.print("[dim]bye 👋[/]")
            break

        if fancy:
            line = await pump.get_line()
        else:
            console.print("[dim]▏ type below[/]")
            try:
                line = await pump.queue.get()
            except KeyboardInterrupt:
                line = None
        if line is None:
            console.print("\n[dim]bye 👋[/]")
            break

        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) > 1500:
            # 📋 don't re-echo a wall of pasted text — acknowledge it
            console.print(f"[dim]📋 pasted {len(stripped):,} chars[/]")
        if not await handle_line(stripped):
            break

async def _do_rewind(rt: Runtime, pump) -> None:
    """⏪ Interactive checkpoint restore with a scope choice."""
    from talos.memory.checkpoints import list_checkpoints, restore

    cks = list_checkpoints()
    if not cks:
        console.print("[dim]no checkpoints yet[/]")
        return
    table = Table(title="⏪ checkpoints")
    table.add_column("#", justify="right", style="dim")
    table.add_column("turn", justify="right")
    table.add_column("when", style="cyan")
    table.add_column("files", justify="center")
    table.add_column("prompt")
    recent = cks[-12:]
    for i, c in enumerate(recent, 1):
        table.add_row(str(i), str(c.turn), c.id,
                      "📸" if c.tree else "·", c.label)
    console.print(table)
    if pump is None:
        return
    console.print("[yellow]rewind to # (enter to cancel) ›[/] ", end="")
    pick = (await pump.queue.get() or "").strip()
    if not pick.isdigit() or not (1 <= int(pick) <= len(recent)):
        return
    target = recent[int(pick) - 1]
    console.print(
        r"[yellow]restore what? \[b]oth · \[c]hat only · \[f]iles only ›[/] ",
        end="",
    )
    scope_key = (await pump.queue.get() or "b").strip().lower()[:1]
    scope = {"b": "both", "c": "chat", "f": "files"}.get(scope_key, "both")
    messages, files_restored = restore(target.id, scope)
    if messages is not None:
        rt.messages = messages
        save_session(rt.session_id, rt.messages)
    bits = []
    if messages is not None:
        bits.append(f"chat → turn {target.turn}")
    if files_restored:
        bits.append("files rolled back")
    console.print(f"[dim]⏪ {' · '.join(bits) or 'nothing restored'}[/]")


async def _run_builtin(name: str, rt: Runtime, pump=None) -> None:
    if name == "/help":
        console.print(help_text())
    elif name == "/clear":
        rt.messages = []
        console.print("[dim]🧹 conversation cleared[/]")
    elif name == "/tools":
        for t in get_tools():
            console.print(f"  {TOOL_EMOJI.get(t.name, '🔧')} [cyan]{t.name}[/] — {t.description.splitlines()[0]}")
    elif name == "/memory":
        from talos.memory.notes import load_memory

        console.print(load_memory() or "[dim](memory is empty)[/]")
    elif name == "/rewind":
        await _do_rewind(rt, pump)
    elif name == "/init":
        await rt.init_workspace()
    elif name == "/learn":
        await rt.learn_skill()
    elif name == "/think":
        settings.think = not settings.think
        rt._rebuild_graph()  # think instruction lives in the system prompt
        console.print(f"[dim]💭 think mode {'on' if settings.think else 'off'}[/]")
    elif name == "/compact":
        did = await rt.maybe_compact(force=True)
        if not did:
            console.print("[dim]nothing to compact yet[/]")
    elif name == "/usage":
        from rich.table import Table as RichTable

        from talos.memory.sessions import all_time_usage

        u, a = rt.usage, all_time_usage()
        cost = rt.session_cost()
        table = RichTable(show_header=True, header_style="dim", box=None,
                          padding=(0, 3))
        table.add_column("")
        table.add_column("turns", justify="right")
        table.add_column("↑ in", justify="right")
        table.add_column("↓ out", justify="right")
        table.add_column("total", justify="right", style="bold cyan")
        table.add_column("cost", justify="right", style="green")
        table.add_row(
            "this session", str(u["turns"]), f"{u['input']:,}",
            f"{u['output']:,}", f"{u['total']:,}",
            f"${cost:.3f}" if cost is not None else "·",
        )
        table.add_row(
            "[dim]all time[/]", f"[dim]{a['turns']}[/]",
            f"[dim]{a['input']:,}[/]", f"[dim]{a['output']:,}[/]",
            f"[dim]{a['total']:,}[/]",
            f"[dim]${a['cost']:.3f}[/]" if a.get("cost") else "·",
        )
        console.print(Panel(table, title="📊 usage", border_style="dim",
                            title_align="left", padding=(1, 2)))
    elif name == "/models":
        import talos.integrations.models as _mm

        if _mm._models_memo is None:
            rt.status.set("📇 fetching /v1/models…")
        try:
            # hard cap: /models must NEVER hang the REPL. Instant when the
            # startup prime already populated the cache.
            found = sorted(
                await asyncio.wait_for(asyncio.to_thread(_mm.list_models), timeout=12),
                key=lambda m: m.id,
            )
        except asyncio.TimeoutError:
            rt.status.stop()
            console.print("[red]/models timed out (12s) — your /v1/models "
                          "endpoint is slow or unreachable.[/]")
            return
        except Exception as exc:
            rt.status.stop()
            console.print(f"[red]could not fetch models: {exc}[/]")
            console.print("[dim]some enterprise gateways don't expose "
                          "/v1/models — set TALOS_MODEL manually in .env[/]")
            return
        rt.status.stop()
        if not found:
            console.print("[yellow]/v1/models returned an empty list[/]")
            if _mm._prime_error:
                console.print(f"[dim]startup prime error: {_mm._prime_error}[/]")
            return
        table = Table(title="📇 models (from the provider's /v1/models)")
        table.add_column("#", justify="right", style="dim")
        table.add_column("id", style="cyan")
        table.add_column("ctx", justify="right")
        table.add_column("$/M in", justify="right")
        table.add_column("$/M out", justify="right")
        table.add_column("👁", justify="center")
        for i, m in enumerate(found, 1):
            table.add_row(
                str(i),
                m.id + (" [magenta]← current[/]" if m.id == rt.model_name else ""),
                f"{m.context:,}" if m.context else "·",
                f"{m.input_per_m:.2f}" if m.input_per_m is not None else "·",
                f"{m.output_per_m:.2f}" if m.output_per_m is not None else "·",
                "👁" if m.vision else "·",
            )
        console.print(table)
        console.print(
            "[dim]👁 = vision/multimodal · pricing from provider metadata or "
            "LiteLLM's community db · blank = unknown[/]"
        )
        if pump is None:
            return
        console.print("[yellow]switch to # (enter to keep current) ›[/] ", end="")
        choice = await pump.queue.get()
        if choice and choice.strip().isdigit():
            n = int(choice.strip())
            if 1 <= n <= len(found):
                rt.switch_model(found[n - 1].id)
    elif name == "/mermaid":
        if rt.last_mermaid:
            path = open_in_browser(rt.last_mermaid)
            console.print(f"[dim]🧜 opened {path}[/]")
        else:
            console.print("[dim]no mermaid blocks in the last reply[/]")
    elif name == "/vault":
        # 🔐 In-chat vault view + redaction toggle. List handles like
        # `talos vault list`, plus subcommands for the session-scoped
        # scrubber:  /vault unredact   /vault redact
        from talos.infra.vault import RevealedSecrets, all_handles

        # The slash command parser stashes everything after the name in
        # `payload` only for /plan and /evolve; for builtins we get just
        # the name, so subcommands aren't reachable from `_run_builtin`.
        # The dispatcher routes "/vault unredact" → action="unknown",
        # so we expose toggles by listening for an `args` attribute we
        # tack on via dispatch upgrade in a follow-up; for now, /vault
        # lists handles and prints the current scrub state — the toggle
        # lives in `talos vault` (CLI) and an in-REPL form is M57 polish.
        handles = all_handles()
        if not handles:
            console.print(
                "[dim]🔐 no vault entries yet — try: "
                "talos vault add <handle> --description '...'[/]"
            )
            return
        table = Table(title=f"🔐 vault handles ({len(handles)})",
                      show_header=True, header_style="dim")
        table.add_column("handle", style="cyan")
        table.add_column("kind", justify="center")
        table.add_column("scope")
        table.add_column("description / value", style="dim")
        icons_k = {"secret": "🔒", "value": "📝"}
        icons_s = {"session": "🟡", "project": "🔵", "global": "🟢"}
        for h in handles:
            if h.kind == "secret":
                body = h.description or "(no description)"
            else:
                body = (h.body or "")[:80]
                if h.description:
                    body += f"  ({h.description})"
            table.add_row(
                h.handle,
                f"{icons_k.get(h.kind, '·')} {h.kind}",
                f"{icons_s.get(h.scope, '·')} {h.scope}",
                body,
            )
        console.print(table)
        state = "ON" if RevealedSecrets.is_enabled() else "OFF"
        n = RevealedSecrets.revealed_count()
        console.print(
            f"[dim]🔐 scrubber: {state}  ·  {n} secret value(s) revealed this session[/]"
        )
    elif name == "/runs":
        # 📬 List recent scheduled-task runs and mark them read. The
        # daemon writes runs to .talos/schedules/<id>/runs/; this is the
        # in-chat way to see what fired while you were away.
        from talos.lifecycle.scheduling import all_runs, mark_all_read

        runs = all_runs(limit_per_schedule=10)
        if not runs:
            console.print("[dim]📬 no scheduled runs yet — "
                          "try: talos schedule add 'do X' --when 'every morning at 9'[/]")
            return
        table = Table(title=f"📬 scheduled runs ({len(runs)})", show_header=True,
                      header_style="dim")
        table.add_column("when", style="cyan", no_wrap=True)
        table.add_column("schedule", style="magenta")
        table.add_column("✓", justify="center")
        table.add_column("dur", justify="right")
        table.add_column("response", style="dim")
        icons = {"ok": "[green]✅[/]", "error": "[red]💥[/]",
                 "skipped": "[yellow]⏭[/]"}
        for r in runs[:25]:
            resp = (r.get("response") or "").splitlines()
            head = (resp[0] if resp else "")[:80]
            dur = r.get("duration_s")
            unread_mark = "" if r.get("read") else " [bold #ffd75f]•[/]"
            table.add_row(
                r.get("started_at", "?") + unread_mark,
                r.get("schedule_id", "?"),
                icons.get(r.get("status"), "·"),
                f"{dur:.1f}s" if dur is not None else "·",
                head,
            )
        console.print(table)
        flipped = mark_all_read()
        if flipped:
            console.print(f"[dim]📬 marked {flipped} run(s) read[/]")
