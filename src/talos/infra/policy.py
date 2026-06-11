"""🚧 Policy layer — deterministic deny rules, checked before the gate.

The 2026 security consensus: permission prompts and even sandboxing aren't
enough on their own; you also want a *deterministic* policy engine that
blocks known-dangerous actions regardless of what the model was convinced
to do (the layer that would have stopped the May-2026 Gemini CLI
supply-chain injection from running `rm -rf` / exfiltrating env vars).

This runs FIRST in the tools node — before the permission prompt — so a
denied command never even reaches the human as an option. Rules are simple
regexes over shell commands and write paths; users extend them in
``.talos/policy.json``.
"""

import json
import re
from pathlib import Path

# Hard denies — destructive or exfiltration-shaped commands.
DEFAULT_SHELL_DENY = [
    r"\brm\s+-rf?\s+[/~]",            # rm -rf / or ~
    r":\(\)\s*\{.*\}\s*;\s*:",        # fork bomb
    r"\bmkfs\b", r"\bdd\s+if=",        # disk wipes
    r">\s*/dev/sd",                    # writing to raw disks
    r"\bcurl\b.*\|\s*(sudo\s+)?(ba)?sh",  # curl | sh
    r"\bwget\b.*\|\s*(ba)?sh",
    r"(cat|cp|scp|curl|tar)\b.*\.(env|pem|key)\b.*(http|nc|curl)",  # exfil creds
    r"\bgit\b.*push.*--force.*\b(main|master)\b",
]

# Paths the agent must never write to.
DEFAULT_WRITE_DENY = [
    r"\.ssh/", r"\.aws/", r"\.env$", r"/etc/", r"id_rsa", r"\.pem$",
]


def policy_file() -> Path:
    return Path(".talos") / "policy.json"


def _load_user_rules() -> dict:
    f = policy_file()
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _compiled(key: str, defaults: list[str]) -> list[re.Pattern]:
    user = _load_user_rules().get(key, [])
    return [re.compile(p, re.I) for p in (defaults + list(user))]


def check_action(tool_name: str, args: dict) -> str | None:
    """Return a denial reason if the action violates policy, else None."""
    if tool_name == "shell":
        cmd = str(args.get("command", ""))
        for rx in _compiled("shell_deny", DEFAULT_SHELL_DENY):
            if rx.search(cmd):
                return (f"🚧 blocked by policy: command matches deny rule "
                        f"/{rx.pattern}/. Edit .talos/policy.json to override.")
    if tool_name in ("write_file", "edit_file"):
        path = str(args.get("path", ""))
        for rx in _compiled("write_deny", DEFAULT_WRITE_DENY):
            if rx.search(path):
                return (f"🚧 blocked by policy: writing to {path} matches "
                        f"/{rx.pattern}/.")
    return None
