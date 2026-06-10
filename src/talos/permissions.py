"""🛡️ Permissions — the gate between "the model wants to" and "it happens".

Every serious agent has one of these. The rules Talos uses:

- **read-only tools** (read_file, grep, …) run freely — they can't break
  anything.
- **mutating tools** (write_file, shell, …) need a human "yes" in
  interactive mode, and are **denied by default** in one-shot mode.
- ``--yolo`` switches the gate off entirely (the classic
  "dangerously-skip-permissions" flag).
- answering **a**lways in the prompt allowlists that tool for the session.

The gate is *injected into the tools node* (see graph/builder.py), so the
model itself never gets to bypass it — denial just becomes a ToolMessage
the model can read and react to.
"""

from collections.abc import Callable

# Tools that can't mutate anything — always allowed.
READ_ONLY_TOOLS = {"read_file", "list_dir", "glob_files", "grep", "web_fetch"}

# An approver looks at (tool_name, args) and answers:
#   "y" → allow once   "a" → allow for the whole session   anything else → deny
Approver = Callable[[str, dict], str]


class PermissionGate:
    def __init__(self, approver: Approver | None = None, yolo: bool = False):
        self.approver = approver  # None = non-interactive (can't ask)
        self.yolo = yolo
        self.session_allowed: set[str] = set()

    def check(self, tool_name: str, args: dict) -> tuple[bool, str]:
        """Return (allowed, reason_if_denied)."""
        if self.yolo:
            return True, ""
        if tool_name in READ_ONLY_TOOLS or tool_name in self.session_allowed:
            return True, ""
        if self.approver is None:
            return False, (
                f"Permission denied: '{tool_name}' mutates state and this is a "
                "non-interactive run. Re-run with --yolo to allow, or use "
                "interactive chat."
            )

        answer = self.approver(tool_name, args).strip().lower()
        if answer == "a":
            self.session_allowed.add(tool_name)
            return True, ""
        if answer == "y":
            return True, ""
        return False, f"Permission denied by user for '{tool_name}'."
