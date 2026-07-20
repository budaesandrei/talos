"""🐚 Shell tool — run a command, capture stdout+stderr.

The single most powerful (and dangerous) tool an agent can have.
Milestone M7 wraps it in a permission gate; here it's raw capability.

Async on purpose: the command runs as an asyncio subprocess, so when the
user cancels the turn (Esc ×2 / Ctrl-C) the CancelledError unwinds through
``communicate()`` and we KILL the process tree instead of orphaning it in
a worker thread — "stop" means the command actually dies.
"""

import asyncio
import os
import subprocess

from langchain_core.tools import tool

from talos.infra.environment import detect_shell, shell_command
from talos.infra.sandbox import wrap_command

TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 8_000


def _kill_tree(pid: int) -> None:
    """Terminate the process and everything it spawned. Best-effort."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10,
            )
        else:
            import signal

            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError,
            subprocess.SubprocessError):
        pass


@tool
async def shell(command: str) -> str:
    """Run a shell command and return its output (stdout + stderr, exit code).
    The executing shell and its syntax rules are listed in your Environment
    section — use that syntax.

    🔐 Vault substitution: if the command contains placeholders like
    ``{{secret:<handle>}}`` or ``{{value:<handle>}}``, the shell tool
    resolves them from the vault before exec — the actual plaintext
    never enters your message history. Missing handles are left as-is
    so the failure is visible in the command output.
    """
    from talos.infra.vault import substitute

    command, missing = substitute(command)

    cmd = shell_command(command)
    # 📦 optionally wrap for sandboxed execution (identity when off)
    wrapped = wrap_command(command, os.getcwd())
    if isinstance(wrapped, list):
        cmd = wrapped

    kwargs: dict = {"stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE}
    if os.name != "nt":
        kwargs["start_new_session"] = True  # own group → killable as a tree
    if isinstance(cmd, str):
        proc = await asyncio.create_subprocess_shell(cmd, **kwargs)
    else:
        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        _kill_tree(proc.pid)
        return f"Error: command timed out after {TIMEOUT_SECONDS}s"
    except asyncio.CancelledError:
        # ⛔ the user stopped the turn — take the command down with it
        _kill_tree(proc.pid)
        raise

    out = ((stdout or b"").decode("utf-8", errors="replace")
           + (stderr or b"").decode("utf-8", errors="replace"))
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + f"\n… [truncated, {len(out) - MAX_OUTPUT_CHARS} more chars]"
    prefix = ""
    if missing:
        prefix = (f"⚠️ unresolved vault placeholders: {', '.join(missing)} — "
                  "left as-is in the command\n")
    return (prefix + f"exit code: {proc.returncode} (shell: {detect_shell()})\n{out}").strip()
