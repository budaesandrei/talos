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
    yolo: bool = typer.Option(
        False, "--yolo", help="🛡️  Skip all permission prompts (dangerous)."
    ),
    resume: Optional[str] = typer.Option(
        None, "--resume", "-r",
        help="💾 Resume a saved session: an ID from 'talos sessions', or 'latest'.",
    ),
) -> None:
    """💬 Chat with Talos (interactive by default)."""
    from talos.runtime.runner import repl, run_once

    if no_interactive:
        if not prompt:
            raise typer.BadParameter("--no-interactive needs a PROMPT argument")
        asyncio.run(run_once(prompt, model, yolo=yolo))
    else:
        asyncio.run(repl(model, initial_prompt=prompt, yolo=yolo, resume=resume))


@app.command()
def run(
    prompt: str = typer.Argument(..., help="The task for Talos."),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    yolo: bool = typer.Option(
        False, "--yolo", help="🛡️  Skip all permission prompts (dangerous)."
    ),
) -> None:
    """⚡ One-shot: send a single prompt, stream the answer, exit."""
    from talos.runtime.runner import run_once

    asyncio.run(run_once(prompt, model, yolo=yolo))


@app.command()
def sessions() -> None:
    """💾 List saved chat sessions."""
    from talos.sessions import list_sessions

    rows = list_sessions()
    if not rows:
        console.print("[dim]no saved sessions yet — run 'talos chat'[/]")
        return
    table = Table(title="💾 Sessions")
    table.add_column("id", style="cyan")
    table.add_column("title")
    table.add_column("messages", justify="right")
    for row in rows:
        table.add_row(row["id"], row.get("title") or "[dim]…[/]", str(row["messages"]))
    console.print(table)
    console.print("[dim]resume with: talos chat -r <id>   (or -r latest)[/]")


@app.command()
def skills() -> None:
    """🎒 List discovered skills (.talos/skills/*/SKILL.md)."""
    from talos.skills import discover_skills

    found = discover_skills()
    if not found:
        console.print("[dim]no skills yet — create .talos/skills/<name>/SKILL.md[/]")
        return
    table = Table(title="🎒 Skills")
    table.add_column("name", style="cyan")
    table.add_column("description")
    for s in found:
        table.add_row(s.name, s.description)
    console.print(table)


@app.command()
def agents() -> None:
    """🤖 List subagent definitions (.talos/agents/*.md)."""
    from talos.agents import discover_agents

    found = discover_agents()
    if not found:
        console.print("[dim]no subagents yet — create .talos/agents/<name>.md[/]")
        return
    table = Table(title="🤖 Subagents")
    table.add_column("name", style="cyan")
    table.add_column("description")
    table.add_column("tools", style="dim")
    for a in found:
        table.add_row(a.name, a.description, ", ".join(a.tools) or "(default read-only)")
    console.print(table)


@app.command()
def commands() -> None:
    """⌨️  List custom slash commands (.talos/commands/*.md)."""
    from talos.commands import custom_commands

    found = custom_commands()
    if not found:
        console.print("[dim]no custom commands yet — create .talos/commands/<name>.md[/]")
        return
    for name, path in found.items():
        console.print(f"  [cyan]{name}[/] — {path}")


@app.command()
def mcp() -> None:
    """🔌 Show configured MCP servers and the tools they expose."""
    from talos.mcp import load_mcp_config, load_mcp_tools, mcp_config_file

    servers = load_mcp_config()
    if not servers:
        console.print(f"[dim]no MCP servers — create {mcp_config_file()}[/]")
        return
    for name, spec in servers.items():
        target = spec.get("command", spec.get("url", "?"))
        console.print(f"  [cyan]{name}[/] → {target}")
    try:
        tools = asyncio.run(load_mcp_tools())
    except (RuntimeError, ValueError) as exc:
        console.print(f"[yellow]{exc}[/]")
        return
    table = Table(title="🔌 MCP tools")
    table.add_column("tool", style="cyan")
    table.add_column("description")
    for t in tools:
        table.add_row(t.name, (t.description or "").splitlines()[0][:80])
    console.print(table)


@app.command()
def models() -> None:
    """📇 List the provider's models with context/pricing/vision info."""
    from talos.models import list_models

    try:
        found = sorted(list_models(), key=lambda m: m.id)
    except Exception as exc:
        console.print(f"[red]could not fetch models: {exc}[/]")
        raise typer.Exit(1)
    table = Table(title="📇 models")
    table.add_column("id", style="cyan")
    table.add_column("ctx", justify="right")
    table.add_column("$/M in", justify="right")
    table.add_column("$/M out", justify="right")
    table.add_column("👁", justify="center")
    for m in found:
        table.add_row(
            m.id,
            f"{m.context:,}" if m.context else "·",
            f"{m.input_per_m:.2f}" if m.input_per_m is not None else "·",
            f"{m.output_per_m:.2f}" if m.output_per_m is not None else "·",
            "👁" if m.vision else "·",
        )
    console.print(table)


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
    table.add_row("yolo", str(settings.yolo))
    console.print(table)
