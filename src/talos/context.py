"""🧠 Context assembly — everything Talos knows *before* you say anything.

The system prompt is assembled fresh for each session from layered parts:

1. the base persona  (``prompts/system.md``)
2. runtime environment info (cwd, date)

Later milestones stack more layers on top (rules file, memory, skills…).
"""

from datetime import datetime
from pathlib import Path

from talos.config import PACKAGE_ROOT

BASE_PROMPT_PATH = PACKAGE_ROOT / "prompts" / "system.md"


def environment_info() -> str:
    return (
        "## Environment\n"
        f"- working directory: {Path.cwd()}\n"
        f"- today's date: {datetime.now():%Y-%m-%d}"
    )


def build_system_prompt() -> str:
    parts = [BASE_PROMPT_PATH.read_text(encoding="utf-8").strip()]
    parts.append(environment_info())
    return "\n\n".join(parts)
