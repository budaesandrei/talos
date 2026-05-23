import typer

from talos.runtime.runner import run_agent

app = typer.Typer(no_args_is_help=True)


@app.command()
def run(prompt: str) -> None:
    """Run Talos with a single prompt."""
    output = run_agent(prompt)
    typer.echo(output)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    prompt: str | None = typer.Argument(None),
) -> None:
    if ctx.invoked_subcommand:
        return

    if prompt is None:
        typer.echo("Usage: talos 'hello'")
        raise typer.Exit(code=1)

    output = run_agent(prompt)
    typer.echo(output)
