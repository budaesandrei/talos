"""💾 Sessions — conversations that survive a restart.

Each chat session is one JSON file in ``~/.talos/sessions/`` (global,
M60 onward — was ``.talos/sessions/`` cwd-local before), serialized
with LangChain's message (de)serializers. ``talos chat -r latest``
rebuilds the message list and carries on where you left off.

Per-project context is preserved as a ``project_path`` field in each
session's metadata (auto-stamped at creation from ``Path.cwd()``).
``list_sessions(project=...)`` filters by project; ``project="all"``
returns the full cross-project list. Use ``talos sessions migrate`` to
move any legacy per-project sessions into the new global home.
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
    """``~/.talos/sessions/`` — global home for every conversation across
    every project. The vault's ``global_dir()`` resolves the cross-platform
    config root (TALOS_HOME / XDG_CONFIG_HOME / APPDATA / ~/.talos)."""
    from talos.infra.vault import global_dir
    return global_dir() / "sessions"


def legacy_sessions_dir() -> Path:
    """The pre-M60 cwd-local sessions dir. Used by `talos sessions migrate`."""
    return Path(".talos") / "sessions"


def current_project_path() -> str:
    """The cwd as an absolute string — stamped onto new sessions so we
    can later filter `list_sessions(project='here')`."""
    return str(Path.cwd().resolve())


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
    """Generate a fresh session id and pre-stamp it with the current
    project_path in the global index, so cross-project filtering works
    from the first save."""
    sid = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        set_session_meta(sid, project_path=current_project_path())
    except Exception:
        # Best effort — never block session creation on metadata failure
        pass
    return sid


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


def list_sessions(project: str | None = "here") -> list[dict]:
    """List sessions, optionally filtered by project.

    ``project="here"`` (default) — only sessions stamped with the current
        cwd's project_path
    ``project="all"``  — every session across every project
    ``project=<str>``  — sessions matching the given absolute path
    ``project=None``   — same as "all"
    """
    d = sessions_dir()
    out = []
    cwd = current_project_path()
    for f in (_session_files(d) if d.is_dir() else []):
        meta = _load_index().get(f.stem, {})
        sess_project = meta.get("project_path")
        if project not in (None, "all"):
            target = cwd if project == "here" else project
            # If we don't know this session's project, include it under
            # "here" so legacy sessions don't disappear from the list.
            if sess_project is not None and sess_project != target:
                continue
        try:
            n = len(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            n = -1
        out.append(
            {
                "id": f.stem,
                "messages": n,
                "title": meta.get("title", ""),
                "project_path": sess_project,
            }
        )
    return out


def migrate_legacy_sessions() -> dict:
    """Copy any sessions found in the legacy cwd-local ``.talos/sessions/``
    into the global home, stamping ``project_path=cwd`` so the
    cross-project filter knows where they originated.

    Idempotent — sessions already present in the global dir are skipped.
    Returns ``{"migrated": N, "skipped": M, "from": <path>, "to": <path>}``.
    """
    src = legacy_sessions_dir()
    dst = sessions_dir()
    if not src.is_dir():
        return {"migrated": 0, "skipped": 0, "from": str(src), "to": str(dst)}

    dst.mkdir(parents=True, exist_ok=True)
    cwd = current_project_path()
    migrated = 0
    skipped = 0
    for f in _session_files(src):
        target = dst / f.name
        if target.exists():
            skipped += 1
            continue
        target.write_bytes(f.read_bytes())
        set_session_meta(f.stem, project_path=cwd)
        migrated += 1
    # Pull the legacy per-project index into the global one too
    legacy_index = src / "index.json"
    if legacy_index.is_file():
        try:
            legacy = json.loads(legacy_index.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            legacy = {}
        for sid, meta in legacy.items():
            target = dst / f"{sid}.json"
            if target.is_file():
                # Don't overwrite if global already has a richer entry
                existing = _load_index().get(sid, {})
                merged = {**meta, **existing, "project_path": cwd}
                set_session_meta(sid, **merged)
    return {"migrated": migrated, "skipped": skipped,
            "from": str(src), "to": str(dst)}
