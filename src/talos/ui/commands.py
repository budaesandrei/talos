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
    "/usage": "show token usage (session + all-time)",
    "/compact": "🗜️ fold older turns into a summary now",
    "/think": "💭 toggle think mode (reason before answering)",
    "/learn": "🧪 distill the last task into a reusable skill (verified)",
    "/init": "🗂️ survey the project and write a starter TALOS.md",
    "/rewind": "⏪ jump back to a checkpoint (chat/files/both)",
    "/models": "list the provider's models, switch the active one",
    "/plan": "🗺️ plan before doing: /plan <task> (AI-DLC style)",
    "/evolve": "🔄 lifecycle loop: debt → persona research → requirements → plan",
    "/runs": "📬 show recent scheduled-task runs (📅 talos schedule …)",
    "/vault": "🔐 list vault handles · /vault unredact | redact toggles scrubbing",
    "/knowledge": "🗂 list user knowledge bases (manage with `talos knowledge ...`)",
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
    lines.append("")
    lines.append("🐚 shell escape (no LLM):")
    lines.append("  !cmd       run directly; agent sees the output")
    lines.append("  !!cmd      run directly; output NOT shared with the agent")
    customs = custom_commands()
    if customs:
        lines.append("")
        lines.append("custom (.talos/commands/):")
        lines += [f"  {name} <args>" for name in customs]
    return "\n".join(lines)


def dispatch(line: str) -> tuple[str, str]:
    """Classify a user line.

    Returns (action, payload):
      ("chat", line)            → send to the model
      ("prompt", text)          → custom command expanded into a prompt
      ("builtin", name)         → caller handles it
      ("unknown", name)         → unknown slash command
      ("shell", cmd)            → !cmd: run directly, share output with model
      ("shell-silent", cmd)     → !!cmd: run directly, do NOT share with model
    """
    # 🐚 Shell escape — !cmd runs without involving the LLM. !! is silent
    # (output goes to your terminal only); ! is shared (the command + output
    # are appended to the conversation so the agent sees what you saw).
    # Note: !! must be checked BEFORE ! because of the prefix overlap.
    if line.startswith("!!"):
        cmd = line[2:].lstrip()
        return ("shell-silent" if cmd else "unknown"), (cmd or "!!")
    if line.startswith("!"):
        cmd = line[1:].lstrip()
        return ("shell" if cmd else "unknown"), (cmd or "!")

    if not line.startswith("/"):
        return "chat", line

    name, _, args = line.partition(" ")
    if name in {"/exit", "/quit"}:
        return "builtin", "/exit"
    if name == "/plan":
        return "plan", args.strip()
    if name == "/evolve":
        return "evolve", args.strip()
    if name in BUILTINS:
        return "builtin", name

    customs = custom_commands()
    if name in customs:
        return "prompt", expand_custom(customs[name], args.strip())

    return "unknown", name
