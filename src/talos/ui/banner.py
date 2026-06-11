"""🕹️ The intro banner — every good CLI agent greets you.

Talos (Τάλως) was the bronze automaton forged by Hephaestus to guard
Crete — arguably history's first artificial agent, three millennia
before this repo. The banner leans into that: big half-block pixel
letters, centered, with a molten-bronze gradient that sweeps across
once on startup (the "casting" animation — skipped when stdout isn't
a terminal).

Half-block characters (█ ▀ ▄) are the standard trick for terminal
pixel art: every cell is two stacked pixels.
"""

import random
import time

from rich.console import Console
from rich.text import Text

from talos import __version__

# "ANSI shadow" lettering: solid faces with a thin box-drawing shadow —
# reads as engraved metal once the bronze gradient lands on it.
LOGO = (
    "████████╗ █████╗ ██╗      ██████╗ ███████╗",
    "╚══██╔══╝██╔══██╗██║     ██╔═══██╗██╔════╝",
    "   ██║   ███████║██║     ██║   ██║███████╗",
    "   ██║   ██╔══██║██║     ██║   ██║╚════██║",
    "   ██║   ██║  ██║███████╗╚██████╔╝███████║",
    "   ╚═╝   ╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚══════╝",
)

# molten bronze, dark → bright → dark; the sweep shifts this along columns
PALETTE = ("#6b3f17", "#915425", "#b8702e", "#d98f3a", "#f5b54a",
           "#ffd75f", "#fff3b0", "#ffd75f", "#f5b54a", "#b8702e")

TAGLINE = "⚡ the bronze guardian · forged with LangChain + LangGraph"

TIPS = (
    "type while I work — I'll answer, take notes, or stop, depending on what you say",
    "TAB completes slash commands · /plan <task> for AI-DLC planning mode",
    "/mermaid opens diagrams from my last reply in your browser",
    "rules live in TALOS.md; things I should remember go to /memory",
    "talos chat -r latest resumes exactly where you left off",
    "answer 'a' on a permission prompt to allowlist that tool for the session",
    "/usage shows tokens + cost · /models switches the active model",
    "the original Talos circled Crete three times a day — I just loop think→act",
)


def _frame(offset: int, width: int) -> Text:
    """One animation frame: the gradient shifted by `offset` columns."""
    text = Text()
    pad = max((width - len(LOGO[0])) // 2, 0)
    for row in LOGO:
        text.append(" " * pad)
        for col, ch in enumerate(row):
            text.append(ch, style=PALETTE[(col + offset) // 3 % len(PALETTE)])
        text.append("\n")
    return text


def _centered(console: Console, markup: str) -> None:
    console.print(markup, justify="center", highlight=False)


def print_banner(
    console: Console,
    model: str,
    session_id: str,
    yolo: bool = False,
    resumed: int = 0,
    title: str = "",
) -> None:
    console.print()
    if console.is_terminal:
        # 🫠 the casting sweep: ~0.7s of molten bronze, then settle
        from rich.live import Live

        with Live(console=console, refresh_per_second=24, transient=False) as live:
            for offset in range(0, len(PALETTE) * 3, 2):
                live.update(_frame(offset, console.width))
                time.sleep(0.045)
            live.update(_frame(0, console.width))
    else:
        console.print(_frame(0, console.width))

    _centered(console, f"[dim]{TAGLINE}[/]  [bold #ffd75f]v{__version__}[/]")
    console.print()

    info = f"model [magenta]{model}[/] · session [cyan]{session_id}[/]"
    if title:
        info += f' · [italic]"{title}"[/]'
    if yolo:
        info += " · [bold red]⚡ yolo[/]"
    if resumed:
        info += f" · [dim]💾 {resumed} messages[/]"
    _centered(console, info)
    _centered(console, f"[dim]💡 {random.choice(TIPS)}[/]")
    _centered(console, "[dim]/help for commands · /exit to leave[/]")
    console.print()
