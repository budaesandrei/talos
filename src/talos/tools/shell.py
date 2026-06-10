"""🐚 Shell tool — run a command, capture stdout+stderr.

The single most powerful (and dangerous) tool an agent can have.
Milestone M7 wraps it in a permission gate; here it's raw capability.
"""

import subprocess

from langchain_core.tools import tool

TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 8_000


@tool
def shell(command: str) -> str:
    """Run a shell command and return its output (stdout + stderr, exit code)."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {TIMEOUT_SECONDS}s"

    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + f"\n… [truncated, {len(out) - MAX_OUTPUT_CHARS} more chars]"
    return f"exit code: {proc.returncode}\n{out}".strip()
