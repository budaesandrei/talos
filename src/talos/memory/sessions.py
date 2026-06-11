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


# ── 🏷️ session metadata (titles, usage) lives in one index file ─────────
def index_file() -> Path:
    return sessions_dir() / "index.json"


def _load_index() -> dict:
    f = index_file()
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except json.JSONDecodeError:
        return {}


def set_session_meta(session_id: str, **fields) -> None:
    index = _load_index()
    index.setdefault(session_id, {}).update(fields)
    sessions_dir().mkdir(parents=True, exist_ok=True)
    index_file().write_text(json.dumps(index, indent=1), encoding="utf-8")


def get_session_meta(session_id: str) -> dict:
    return _load_index().get(session_id, {})


def all_time_usage() -> dict:
    """Sum usage (and estimated cost) across every recorded session."""
    from talos.integrations.models import estimate_cost  # late import: avoids a cycle

    totals = {"input": 0, "output": 0, "total": 0, "turns": 0,
              "sessions": 0, "cost": 0.0}
    for meta in _load_index().values():
        usage = meta.get("usage") or {}
        if usage:
            totals["sessions"] += 1
        for key in ("input", "output", "total", "turns"):
            totals[key] += usage.get(key, 0)
        if meta.get("model"):
            cost = estimate_cost(
                meta["model"], usage.get("input", 0), usage.get("output", 0)
            )
            if cost:
                totals["cost"] += cost
    return totals


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


def _session_files(d: Path) -> list[Path]:
    # index.json holds metadata, not messages — never treat it as a session
    return sorted(f for f in d.glob("*.json") if f.name != "index.json")


def latest_session_id() -> str | None:
    d = sessions_dir()
    files = _session_files(d) if d.is_dir() else []
    return files[-1].stem if files else None


def list_sessions() -> list[dict]:
    d = sessions_dir()
    out = []
    for f in (_session_files(d) if d.is_dir() else []):
        try:
            n = len(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            n = -1
        meta = _load_index().get(f.stem, {})
        out.append(
            {
                "id": f.stem,
                "messages": n,
                "title": meta.get("title", ""),
            }
        )
    return out
