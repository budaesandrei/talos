"""🐚 Shell tool — every command runs as a tracked background job.

Fast commands still feel synchronous: we wait a short grace period and
return their output inline. Anything slower keeps running as a *job* —
the model gets the job id / pid / log path immediately, the conversation
stays unblocked, and the runtime's callback door (see tools/jobs.py)
announces completion.

Milestone M7 wraps this in a permission gate; policy rules keyed on the
'shell' tool name apply unchanged, because the tool name didn't change.
"""

import asyncio

from langchain_core.tools import tool

from talos.infra.environment import detect_shell
from talos.tools.jobs import manager

FOREGROUND_GRACE = 10.0   # seconds a command may hold the loop
MAX_OUTPUT_CHARS = 8_000


@tool
async def shell(command: str) -> str:
    """Run a shell command and return its output (stdout + stderr, exit code).
    The executing shell and its syntax rules are listed in your Environment
    section — use that syntax. A command still running after ~10s continues
    as a background job: you immediately get its job id + log path, and a
    [job #N finished] note arrives when it completes. Never wait or poll in
    a loop — carry on, and use job_status(id) / job_kill(id) to peek or stop."""
    job = await manager.start(command)
    try:
        rc = await asyncio.wait_for(job.proc.wait(), FOREGROUND_GRACE)
    except asyncio.TimeoutError:
        job.background = True  # 🚪 completion will use the callback door
        return (
            f"⏳ still running after {FOREGROUND_GRACE:.0f}s — continuing in "
            f"the background as job #{job.id} (pid {job.pid}). Log: "
            f"{job.log_path}. A [job #{job.id} finished] note will arrive on "
            f"completion; job_status({job.id}) shows progress, "
            f"job_kill({job.id}) stops it."
        )
    out = job.read_log(MAX_OUTPUT_CHARS)
    return f"exit code: {rc} (shell: {detect_shell()}, job #{job.id})\n{out}".strip()
