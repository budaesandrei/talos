"""🐚 Shell tool — run a command, capture stdout+stderr.

The single most powerful (and dangerous) tool an agent can have.
Milestone M7 wraps it in a permission gate; here it's raw capability.
"""

import subprocess

from langchain_core.tools import tool

from talos.environment import detect_shell, shell_command
from talos.sandbox import wrap_command

TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 8_000


@tool
def shell(command: str) -> str:
    """Run a shell command and return its output (stdout + stderr, exit code).
    The executing shell and its syntax rules are listed in your Environment
    section — use that syntax."""
    import os

    cmd = shell_command(command)
    # 📦 optionally wrap for sandboxed execution (identity when off)
    wrapped = wrap_command(command, os.getcwd())
    if isinstance(wrapped, list):
        cmd = wrapped
    try:
        proc = subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {TIMEOUT_SECONDS}s"

    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + f"\n… [truncated, {len(out) - MAX_OUTPUT_CHARS} more chars]"
    return f"exit code: {proc.returncode} (shell: {detect_shell()})\n{out}".strip()
