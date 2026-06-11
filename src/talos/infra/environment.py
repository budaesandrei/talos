"""🖥️ Environment detection — the agent should know where it lives.

An agent that doesn't know its shell writes `cmd1 && cmd2` for
PowerShell 5 (no && there) or backslash paths for bash. This module
detects the OS / shell / WSL situation once and reports it in the
system prompt, including shell-specific syntax warnings.

The shell *tool* executor is also made explicit (TALOS_SHELL=auto|cmd|
powershell|bash|...): "whatever shell=True picks" is exactly the kind
of ambiguity that bites agents.
"""

import os
import platform
import shutil
import sys

from talos.config import settings

SYNTAX_HINTS = {
    "powershell": "PowerShell: chain with ';' (NOT '&&' on PS5), quote with \"...\", env vars are $env:NAME",
    "pwsh": "PowerShell 7+: '&&' works, but ';' is safest; env vars are $env:NAME",
    "cmd": "cmd.exe: chain with '&&', env vars are %NAME%, quote paths with spaces",
    "bash": "bash: '&&' chains, env vars are $NAME",
    "sh": "POSIX sh: '&&' chains, env vars are $NAME",
    "zsh": "zsh: '&&' chains, env vars are $NAME",
}


def detect_shell() -> str:
    """Which shell will the `shell` tool execute commands with?"""
    if settings.shell != "auto":
        return settings.shell
    if os.name == "nt":
        # We launch PowerShell explicitly on Windows: it's what most
        # Windows devs use interactively, so examples translate 1:1.
        return "powershell"
    return os.path.basename(os.environ.get("SHELL", "sh"))


def shell_command(command: str) -> list[str] | str:
    """Build the subprocess invocation for the chosen shell."""
    shell = detect_shell()
    if shell in ("powershell", "pwsh"):
        exe = "pwsh" if shell == "pwsh" or (
            not shutil.which("powershell") and shutil.which("pwsh")
        ) else "powershell"
        return [exe, "-NoProfile", "-Command", command]
    if shell == "cmd":
        return ["cmd", "/c", command]
    if os.name != "nt" and shutil.which(shell):
        return [shell, "-c", command]
    return command  # fall back to shell=True semantics


def is_wsl() -> bool:
    return "microsoft" in platform.release().lower() or bool(
        os.environ.get("WSL_DISTRO_NAME")
    )


def describe() -> str:
    """One block for the system prompt: OS, shell, syntax rules."""
    shell = detect_shell()
    os_name = f"{platform.system()} {platform.release()}"
    if is_wsl():
        os_name += f" (WSL: {os.environ.get('WSL_DISTRO_NAME', 'unknown distro')})"
    lines = [
        f"- OS: {os_name} ({platform.machine()})",
        f"- python: {sys.version.split()[0]}",
        f"- the `shell` tool runs commands via: {shell}",
        f"- shell syntax: {SYNTAX_HINTS.get(shell, 'POSIX-ish')}",
    ]
    return "\n".join(lines)
