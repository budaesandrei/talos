"""🖥️ Talos CLI — the front door.

Built with Typer: each ``@app.command()`` function becomes a subcommand.

    talos chat                      💬 interactive REPL
    talos chat -n "do the thing"    ⚡ one-shot (like kiro --no-interactive)
    talos run "do the thing"        ⚡ same as above, shorter
    talos config                    ⚙️  show effective settings
    talos version                   🏷️  print version
"""

import asyncio
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from talos import __version__
from talos.config import settings

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")
console = Console()


@app.command()
def chat(
    prompt: Optional[str] = typer.Argument(
        None, help="Optional first message to seed the session."
    ),
    no_interactive: bool = typer.Option(
        False, "--no-interactive", "-n", help="Answer once and exit (no REPL)."
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m", help="Override the configured model."
    ),
) -> None:
    """💬 Chat with Talos (interactive by default)."""
    from talos.runtime.runner import repl, run_once

    if no_interactive:
        if not prompt:
            raise typer.BadParameter("--no-interactive needs a PROMPT argument")
        asyncio.run(run_once(prompt, model))
    else:
        asyncio.run(repl(model, initial_prompt=prompt))


@app.command()
def run(
    prompt: str = typer.Argument(..., help="The task for Talos."),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
) -> None:
    """⚡ One-shot: send a single prompt, stream the answer, exit."""
    from talos.runtime.runner import run_once

    asyncio.run(run_once(prompt, model))


@app.command()
def version() -> None:
    """🏷️  Print the Talos version."""
    console.print(f"🤖 talos [bold cyan]{__version__}[/]")


@app.command()
def config() -> None:
    """⚙️  Show the effective configuration (API key masked)."""
    table = Table(title="⚙️  Talos configuration", show_header=True)
    table.add_column("setting", style="cyan")
    table.add_column("value")

    masked = (
        settings.api_key[:7] + "…" + settings.api_key[-4:]
        if len(settings.api_key) > 14
        else ("<unset>" if not settings.api_key else "•••")
    )
    table.add_row("base_url", settings.base_url or "<default — api.openai.com>")
    table.add_row("api_key", masked)
    table.add_row("model", settings.model)
    table.add_row("temperature", str(settings.temperature))
    table.add_row("max_iterations", str(settings.max_iterations))
    console.print(table)
