"""💾 Sessions — conversations that survive a restart.

Each chat session is one JSON file in ``.talos/sessions/``, serialized
with LangChain's message (de)serializers. ``talos chat -r latest``
rebuilds the message list and carries on where you left off.
"""

import json
from datetime import datetime
from pathlib import Path

from langchain_core.messages import (
    BaseMessage,
    messages_from_dict,
    messages_to_dict,
)


def sessions_dir() -> Path:
    return Path(".talos") / "sessions"


def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def save_session(session_id: str, messages: list[BaseMessage]) -> None:
    d = sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.json").write_text(
        json.dumps(messages_to_dict(messages), indent=1), encoding="utf-8"
    )


def load_session(session_id: str) -> list[BaseMessage]:
    f = sessions_dir() / f"{session_id}.json"
    if not f.is_file():
        raise FileNotFoundError(f"no session '{session_id}'")
    return messages_from_dict(json.loads(f.read_text(encoding="utf-8")))


def latest_session_id() -> str | None:
    d = sessions_dir()
    files = sorted(d.glob("*.json")) if d.is_dir() else []
    return files[-1].stem if files else None


def list_sessions() -> list[dict]:
    d = sessions_dir()
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            n = len(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            n = -1
        out.append({"id": f.stem, "messages": n})
    return out
