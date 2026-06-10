"""🕹️ The intro banner — every good CLI agent greets you.

Talos (Τάλως) was the bronze automaton forged by Hephaestus to guard
Crete — arguably history's first artificial agent, three millennia
before this repo. The banner leans into that: 8-bit half-block pixel
letters in a bronze-to-gold gradient, plus a rotating tip of the day.

Half-block characters (▀ █ ▄) are the standard trick for "pixel art"
in a terminal: each cell becomes two stacked pixels.
"""

import random

from rich.console import Console

from talos import __version__

# 2-row half-block pixel font ("two pixels per terminal row")
LOGO = (
    "▀█▀ ▄▀█ █   █▀█ █▀▀",
    " █  █▀█ █▄▄ █▄█ ▄██",
)

# bronze → gold, one colour per row (the 8-bit palette of a sunlit statue)
ROW_COLOURS = ("#ffd75f", "#c97f2e")

TAGLINE = "⚡ the bronze guardian · forged with LangChain + LangGraph"

TIPS = (
    "type while I work — I'll answer, take notes, or stop, depending on what you say",
    "/mermaid opens diagrams from my last reply in your browser",
    "rules live in TALOS.md; things I should remember go to /memory",
    "talos chat -r latest resumes exactly where you left off",
    "answer 'a' on a permission prompt to allowlist that tool for the session",
    "/usage shows what this conversation costs in tokens",
    "the original Talos circled Crete three times a day — I just loop think→act",
)


def print_banner(
    console: Console,
    model: str,
    session_id: str,
    yolo: bool = False,
    resumed: int = 0,
) -> None:
    console.print()
    for row, colour in zip(LOGO, ROW_COLOURS):
        console.print(f"  [bold {colour}]{row}[/]", highlight=False)
    console.print(f"  [dim]{TAGLINE}[/]  [bold #ffd75f]v{__version__}[/]")
    console.print()

    info = f"  model [magenta]{model}[/] · session [cyan]{session_id}[/]"
    if yolo:
        info += " · [bold red]⚡ yolo[/]"
    if resumed:
        info += f" · [dim]💾 resumed {resumed} messages[/]"
    console.print(info, highlight=False)
    console.print(f"  [dim]💡 {random.choice(TIPS)}[/]", highlight=False)
    console.print(f"  [dim]/help for commands · /exit to leave[/]\n", highlight=False)
