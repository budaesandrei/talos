"""🐚 Shell escape — user-typed `!cmd` runs directly, no LLM in the loop.

Distinct from the agent's ``shell`` tool: this is what *you* type into
the REPL. Two differences from the agent's path:

1. **No permission gate.** You typed it; gating your own command is
   silly. The gate exists because the *model* might decide to run
   something dangerous and the human can't review every tool call. The
   user *is* the human reviewing the command.
2. **No sandbox.** Same reasoning — the sandbox isolates *agent-
   initiated* commands. A user who wants `!cat /etc/hosts` to actually
   read their hosts file shouldn't get an empty container.

What stays the same: vault substitution. `!echo {{secret:ghpat}}` does
substitute the placeholder before exec (the model isn't in the loop
so opacity isn't violated). If the result gets shared back to the
agent (default mode, not silent), the scrubber redacts the plaintext
out of the message-history copy on the way in.

Two modes:

* ``!cmd``  — *shared*: runs, prints to your terminal, AND appends
              ``[shell] $ cmd\\n<output>`` to the message history as a
              HumanMessage so the agent sees what just happened.
* ``!!cmd`` — *silent*: runs, prints to your terminal, doesn't touch
              the message history. Pure shortcut.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from langchain_core.messages import BaseMessage, HumanMessage

from talos.infra.environment import detect_shell, shell_command


TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 8_000


@dataclass
class EscapeResult:
    """Outcome of one `!cmd` invocation."""

    command: str
    output: str       # stdout + stderr (scrubbed if silent=False)
    exit_code: int
    missing_handles: list[str]
    history_message: BaseMessage | None  # None when silent=True


def run_shell_escape(command: str, *, silent: bool = False) -> EscapeResult:
    """Run a user-typed shell command, return an EscapeResult.

    No permission gate, no sandbox. Vault substitution is applied so
    `{{secret:..}}` works the same way it does for the agent's shell
    tool. When not silent, the history_message field carries a
    HumanMessage suitable for appending to ``Runtime.messages`` so the
    agent sees the command + (scrubbed) output.
    """
    from talos.infra.vault import RevealedSecrets, substitute

    resolved, missing = substitute(command)
    cmd = shell_command(resolved)
    try:
        proc = subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        raw_output = (proc.stdout or "") + (proc.stderr or "")
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        raw_output = f"Error: command timed out after {TIMEOUT_SECONDS}s"
        exit_code = -1
    if len(raw_output) > MAX_OUTPUT_CHARS:
        raw_output = (
            raw_output[:MAX_OUTPUT_CHARS]
            + f"\n… [truncated, {len(raw_output) - MAX_OUTPUT_CHARS} more chars]"
        )

    history_message: BaseMessage | None = None
    if not silent:
        # 🔐 Scrub the WHOLE content, including the echoed command line —
        # otherwise `!echo {{secret:foo}}` (or just `!echo <pasted value>`)
        # would leak through the `[shell] $ ...` header even if the output
        # was clean. Same defense-in-depth applied to agent tool outputs
        # in graph/builder.py.
        full = (
            f"[shell] $ {command}\n"
            f"(exit {exit_code}, shell={detect_shell()})\n"
            f"{raw_output.strip()}"
        )
        scrubbed = RevealedSecrets.scrub(full)
        history_message = HumanMessage(content=scrubbed)
        # Stamp it so M58 time-awareness picks it up.
        try:
            from talos.agent.time_awareness import stamp as _stamp
            _stamp(history_message)
        except Exception:
            pass

    return EscapeResult(
        command=command,
        output=raw_output,
        exit_code=exit_code,
        missing_handles=missing,
        history_message=history_message,
    )
