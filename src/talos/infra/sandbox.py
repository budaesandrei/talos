"""📦 Optional Docker sandbox for the shell tool.

Off by default — when off, ``wrap_command`` is the identity function and
adds ZERO overhead (no Docker calls, no import cost). Turn it on with
``TALOS_SANDBOX=docker`` to run every shell command inside a throwaway
container with the project mounted read-write at /work but the rest of the
host filesystem and network isolated. This is the execution-isolation
layer that holds even when an injection slips past the policy + gate.
"""

import shutil

from talos.config import settings

DEFAULT_IMAGE = "python:3.12-slim"


def enabled() -> bool:
    return settings.sandbox == "docker"


def available() -> bool:
    return shutil.which("docker") is not None


def wrap_command(command: str, cwd: str) -> list[str] | str:
    """Wrap a shell command for sandboxed execution, or return it unchanged.

    Returns a list (argv for docker run) when sandboxing, else the original
    string (which the shell tool runs via the host shell as before).
    """
    if not enabled():
        return command
    image = settings.sandbox_image or DEFAULT_IMAGE
    return [
        "docker", "run", "--rm",
        "--network", "none",                 # no network by default
        "-v", f"{cwd}:/work", "-w", "/work",
        "--memory", "512m", "--cpus", "1",   # resource caps
        image, "sh", "-c", command,
    ]
