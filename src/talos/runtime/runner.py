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
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from talos.banner import print_banner
from talos.commands import dispatch, help_text
from talos.compaction import SUMMARY_PROMPT, compact, fuel_gauge
from talos.config import settings
from talos.context import build_system_prompt
from talos.graph.builder import build_agent_graph
from talos.llm import build_llm
from talos.mcp import load_mcp_tools
from talos.mermaid import ascii_render, extract_mermaid, open_in_browser
from talos.models import estimate_cost
from talos.permissions import PermissionGate
from talos.planning import (
    ELABORATION_PROMPT,
    VERIFY_PROMPT,
    construct_prompt,
    is_ready,
    parse_verdict,
    save_plan,
)
from talos.sessions import (
    get_session_meta,
    latest_session_id,
    load_session,
    new_session_id,
    save_session,
    set_session_meta,
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


class _PromptPump:
    """⌨️→📬 prompt_toolkit edition of the stdin pump (real terminals).

    Same contract as _StdinPump (lines land in .queue, EOF → None), but
    the user gets a persistent editable prompt at the bottom with TAB
    completion for slash commands, while agent output prints above it
    (prompt_toolkit's patch_stdout does that part).
    """

    def __init__(self, stats=None):
        from talos.tui import StatusState

        self.queue: "asyncio.Queue[str | None]" = asyncio.Queue()
        self.eof = False
        self.fancy = True
        self.status_state = StatusState()  # ⚒ rendered in the prompt's toolbar
        self._stats = stats
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        from talos.tui import build_session

        session = build_session(stats=self._stats, status=self.status_state)
        while True:
            try:
                line = await session.prompt_async()
            except EOFError:
                await self.queue.put(None)
                return
            except KeyboardInterrupt:
                continue  # clear the line, keep the session
            await self.queue.put(line)


def make_pump(stats=None):
    """Fancy prompt on real terminals; plain thread pump for pipes/tests."""
    if sys.stdin.isatty() and sys.stdout.isatty():
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
        from talos.models import provider_meta, lookup

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
                from talos.graph_memory import ingest_async

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
        console.print("[yellow]allow? \\[y]es · \\[n]o · \\[a]lways ›[/] ", end="")
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
        self.messages.append(HumanMessage(content=user_input))
        collected: list[BaseMessage] = []
        # 🎨 streaming state: with markdown on, we re-render the growing
        # buffer through rich.Live (live markdown!); with it off, we print
        # raw tokens. Either way the spinner dies on the first token.
        buffer = ""
        live: Live | None = None
        prefix_printed = False
        reasoning_open = False
        header_printed = False

        def ensure_header() -> None:
            nonlocal header_printed
            if not header_printed:
                console.print(self._header(), highlight=False)
                header_printed = True

        def close_stream() -> None:
            nonlocal live, buffer, prefix_printed, reasoning_open
            if live is not None:
                live.stop()
                live = None
            if prefix_printed or reasoning_open:
                print()  # finish the streamed line
            buffer = ""
            prefix_printed = False
            reasoning_open = False

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
                        if settings.markdown:
                            buffer += text
                            if live is None:
                                ensure_header()
                                live = Live(
                                    console=console,
                                    refresh_per_second=8,
                                    vertical_overflow="visible",
                                )
                                live.start()
                            live.update(Markdown(buffer))
                        else:
                            if not prefix_printed:
                                ensure_header()
                                prefix_printed = True
                            print(text, end="", flush=True)

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
            self.messages.extend(collected)
            save_session(self.session_id, self.messages)
            set_session_meta(self.session_id, usage=self.usage, model=self.model_name)
            try:  # ⏪ time-travel checkpoint (chat + file snapshot)
                from talos.checkpoints import save_checkpoint

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

    async def learn_skill(self) -> None:
        """🧪 Synthesize a verified skill from the recent conversation."""
        from talos.skill_synthesis import synthesize

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
    from talos.context import environment_info
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
    console.print("[yellow]execute? \[y]es · \[r]evise · \[n]ot now ›[/] ", end="")
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

    # 🕹️ banner FIRST — its Live animation needs the raw terminal. Only
    # then start the prompt + patch_stdout, or every animation frame
    # would be re-printed above the prompt as a separate block.
    print_banner(
        console,
        model=rt.model_name,
        session_id=rt.session_id,
        yolo=yolo or settings.yolo,
        resumed=len(rt.messages) if resume else 0,
        title=rt.title,
    )

    # 🔥 warm /models in the background: one round trip gives the picker
    # its list AND the cost engine the provider's own per-token prices
    from talos.models import prime_models_cache

    asyncio.get_running_loop().run_in_executor(None, prime_models_cache)

    pump = make_pump(stats=rt.stats_line)  # sole stdin reader + 📊 rprompt
    if pump.fancy:
        rt.status.sink = pump.status_state  # ⚒ status renders in the toolbar
    if pump.fancy:
        # output prints ABOVE the persistent bottom prompt
        from prompt_toolkit.patch_stdout import patch_stdout

        stdout_ctx = patch_stdout(raw=True)
        stdout_ctx.__enter__()
    else:
        stdout_ctx = None

    async def run_turn(text: str) -> None:
        """One turn, listening for interjections the whole time."""
        turn_task = asyncio.create_task(rt.turn(text))
        try:
            while not turn_task.done():
                if pump.eof:
                    # stdin is closed (piped input): nobody can interject —
                    # just let the turn finish.
                    await turn_task
                    break
                getter = asyncio.create_task(pump.queue.get())
                await asyncio.wait(
                    {turn_task, getter}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter.done():
                    line = getter.result()
                    if line is None:
                        pump.eof = True  # remember; don't kill the turn
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
        if action == "prompt":
            console.print("[dim]⌨️  expanded custom command[/]")
        await run_turn(payload)
        return True

    if initial_prompt:
        console.print(f"[bold #ffd75f]→[/] {initial_prompt}")
        await run_turn(initial_prompt)

    while True:
        # 📨 first, anything the user queued while the agent was busy
        if rt.inbox:
            note = rt.inbox.pop(0)
            console.print(f"[bold #ffd75f]→[/] [dim](queued)[/] {note}")
            if not await handle_line(note):
                break
            if rt._pending_verify:  # 🔍 the construct turn just finished
                plan, rt._pending_verify = rt._pending_verify, None
                await rt.verify_plan(plan)
            continue

        if pump.eof:
            console.print("[dim]bye 👋[/]")
            break

        if not pump.fancy:  # the fancy pump draws its own prompt
            console.print("[bold #ffd75f]→[/] ", end="")
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

    if stdout_ctx is not None:
        stdout_ctx.__exit__(None, None, None)

async def _do_rewind(rt: Runtime, pump) -> None:
    """⏪ Interactive checkpoint restore with a scope choice."""
    from talos.checkpoints import list_checkpoints, restore

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
        "[yellow]restore what? \[b]oth · \[c]hat only · \[f]iles only ›[/] ",
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
        from talos.memory import load_memory

        console.print(load_memory() or "[dim](memory is empty)[/]")
    elif name == "/rewind":
        await _do_rewind(rt, pump)
    elif name == "/learn":
        await rt.learn_skill()
    elif name == "/compact":
        did = await rt.maybe_compact(force=True)
        if not did:
            console.print("[dim]nothing to compact yet[/]")
    elif name == "/usage":
        from rich.table import Table as RichTable

        from talos.sessions import all_time_usage

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
        from talos.models import list_models

        rt.status.set("📇 fetching /models…")
        try:
            found = sorted(await asyncio.to_thread(list_models), key=lambda m: m.id)
        except Exception as exc:
            rt.status.stop()
            console.print(f"[red]could not fetch models: {exc}[/]")
            return
        rt.status.stop()
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
