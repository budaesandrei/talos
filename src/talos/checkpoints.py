"""⏪ Checkpoints — time-travel for the conversation AND the workspace.

After every turn Talos snapshots two things:

1. the **conversation** (the message list), and
2. the **files** it may have touched — captured as a git-stash-style blob
   in a *shadow* git repo under ``.talos/checkpoints/shadow`` so it never
   pollutes your real git history.

``/rewind`` then lets you jump back, and — the bit Kiro gets wrong — you
choose the scope:

- **both**  : restore the chat and roll files back (Kiro's only mode)
- **chat**  : restore the conversation, keep your current files
- **files** : roll files back, keep the current conversation

Files are snapshotted with ``git`` against a separate work-tree so we get
content-addressed dedup for free and never touch the user's index.
"""

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def ck_dir() -> Path:
    return Path(".talos") / "checkpoints"


def shadow_dir() -> Path:
    return ck_dir() / "shadow"


def _index() -> Path:
    return ck_dir() / "index.json"


@dataclass
class Checkpoint:
    id: str
    turn: int
    label: str
    tree: str | None          # shadow-git tree hash (file snapshot), or None
    messages: list            # serialized messages


def _run_git(args: list[str]) -> tuple[int, str]:
    """Run git against the shadow repo with the CWD as the work tree."""
    env_git = [
        "git",
        f"--git-dir={shadow_dir()}",
        f"--work-tree={Path.cwd()}",
    ]
    proc = subprocess.run(
        env_git + args, capture_output=True, text=True, timeout=60,
        # git output is UTF-8; without this, Windows decodes with cp1252
        # and non-ASCII paths/messages raise UnicodeDecodeError
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _ensure_shadow() -> bool:
    """Init the shadow repo once. Returns False if git is unavailable."""
    if shadow_dir().is_dir():
        return True
    ck_dir().mkdir(parents=True, exist_ok=True)
    try:
        rc, _ = _run_git(["init", "-q"])
        return rc == 0
    except (OSError, subprocess.SubprocessError):
        return False


def snapshot_files() -> str | None:
    """Capture the current work-tree as a shadow-git tree object.
    Returns the tree hash, or None if git isn't available."""
    if not _ensure_shadow():
        return None
    # stage everything except the agent's own state, then write a tree
    (shadow_dir() / "info").mkdir(exist_ok=True)
    (shadow_dir() / "info" / "exclude").write_text(
        ".talos/\n.git/\n.venv/\n__pycache__/\n", encoding="utf-8"
    )
    if _run_git(["add", "-A"])[0] != 0:
        return None
    rc, out = _run_git(["write-tree"])
    return out if rc == 0 else None


def restore_files(tree: str) -> bool:
    """Roll the work-tree back to a snapshot tree (content of tracked files)."""
    if not tree or not shadow_dir().is_dir():
        return False
    return _run_git(["checkout", tree, "--", "."])[0] == 0


def _load_index() -> list[dict]:
    p = _index()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def save_checkpoint(turn: int, label: str, messages: list, snapshot: bool = True):
    """Record a checkpoint after a turn."""
    from langchain_core.messages import messages_to_dict

    tree = snapshot_files() if snapshot else None
    ck = {
        "id": datetime.now().strftime("%H%M%S-") + str(turn),
        "turn": turn,
        "label": label[:60],
        "tree": tree,
        "messages": messages_to_dict(messages),
    }
    index = _load_index()
    index.append(ck)
    ck_dir().mkdir(parents=True, exist_ok=True)
    _index().write_text(json.dumps(index, indent=1), encoding="utf-8")
    return ck["id"]


def list_checkpoints() -> list[Checkpoint]:
    out = []
    for c in _load_index():
        out.append(Checkpoint(c["id"], c["turn"], c.get("label", ""),
                              c.get("tree"), c.get("messages", [])))
    return out


def restore(checkpoint_id: str, scope: str = "both"):
    """Restore a checkpoint. scope ∈ {both, chat, files}.

    Returns (messages_or_None, files_restored_bool).
    """
    from langchain_core.messages import messages_from_dict

    ck = next((c for c in list_checkpoints() if c.id == checkpoint_id), None)
    if ck is None:
        raise KeyError(checkpoint_id)

    messages = None
    if scope in ("both", "chat"):
        messages = messages_from_dict(ck.messages)

    files_restored = False
    if scope in ("both", "files") and ck.tree:
        files_restored = restore_files(ck.tree)

    return messages, files_restored
