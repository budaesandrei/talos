import typer
from langchain_core.messages import BaseMessage
from rich.console import Console
from rich.table import Table

from talos import __version__
from talos.config import settings
from talos.runtime.runner import get_message_text, run_agent, run_chat_turn

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")
console = Console()


@app.command()
def run(prompt: str) -> None:
    """Run Talos with a single prompt."""
    output = run_agent(prompt)
    typer.echo(output)


@app.command()
def chat() -> None:
    """Start an interactive Talos chat session."""
    typer.echo("Talos chat started. Type /exit to quit.")

    messages: list[BaseMessage] = []

    while True:
        user_input = typer.prompt("you")

        if user_input.strip() in {"/exit", "/quit"}:
            typer.echo("bye")
            raise typer.Exit()

        messages = run_chat_turn(messages, user_input)
        last_message = messages[-1]

        typer.echo(f"talos: {get_message_text(last_message)}")


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
