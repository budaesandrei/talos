"""⌨️ Slash commands — typed by the *human*, never called by the model.

Two kinds:

- **built-ins** (``/help``, ``/clear``, ``/tools``, ``/memory``) handled
  right here in Python, and
- **custom commands**: markdown prompt templates in
  ``.talos/commands/<name>.md``. ``/review src/`` expands the template,
  substituting ``$ARGUMENTS`` — a reusable prompt, not new code.
"""

from pathlib import Path

BUILTINS = {
    "/help": "show this help",
    "/clear": "forget the conversation so far",
    "/tools": "list the agent's tools",
    "/memory": "show long-term memory",
    "/mermaid": "open mermaid diagrams from the last reply in the browser",
    "/exit": "quit (also /quit)",
}


def commands_dir() -> Path:
    return Path(".talos") / "commands"


def custom_commands() -> dict[str, Path]:
    d = commands_dir()
    return {f"/{f.stem}": f for f in sorted(d.glob("*.md"))} if d.is_dir() else {}


def expand_custom(path: Path, arguments: str) -> str:
    template = path.read_text(encoding="utf-8")
    return template.replace("$ARGUMENTS", arguments).strip()


def help_text() -> str:
    lines = [f"  {name:<10} {desc}" for name, desc in BUILTINS.items()]
    customs = custom_commands()
    if customs:
        lines.append("")
        lines.append("custom (.talos/commands/):")
        lines += [f"  {name} <args>" for name in customs]
    return "\n".join(lines)


def dispatch(line: str) -> tuple[str, str]:
    """Classify a user line.

    Returns (action, payload):
      ("chat", line)     → send to the model
      ("prompt", text)   → custom command expanded into a prompt
      ("builtin", name)  → caller handles it
      ("unknown", name)  → unknown slash command
    """
    if not line.startswith("/"):
        return "chat", line

    name, _, args = line.partition(" ")
    if name in {"/exit", "/quit"}:
        return "builtin", "/exit"
    if name in BUILTINS:
        return "builtin", name

    customs = custom_commands()
    if name in customs:
        return "prompt", expand_custom(customs[name], args.strip())

    return "unknown", name
