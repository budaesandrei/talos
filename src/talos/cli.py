import typer
from langchain_core.messages import BaseMessage, HumanMessage

from talos.runtime.runner import get_message_text, run_agent, run_chat_turn

app = typer.Typer(no_args_is_help=True)


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
