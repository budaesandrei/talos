"""🔐 Vault — secrets and scoped values, by handle, never plaintext to the LLM.

A typical interaction:

    talos vault add prod_mongo_uri --description "MongoDB prod cluster URI"
    # (you're prompted to paste the value; it never appears in argv)

    # later, in chat:
    # > query prod for active user count
    # the agent writes: mongo "{{secret:prod_mongo_uri}}" --eval "db.users.count()"
    # the shell tool substitutes the placeholder before exec, the LLM
    # never sees the plaintext

Three scopes — first-hit wins:

* **session**   — in-memory, this REPL only. Lost on exit.
                  Use case: a one-time PAT you're testing with, or an override.
* **project**   — `.talos/vault/index.json` in cwd + OS keyring (or fallback).
                  Travels with the repo (gitignored). Use case: "this app's
                  prod connection string".
* **global**    — `~/.talos/vault/index.json` + keyring.
                  Use case: "my personal GitHub PAT, available in every repo".

Two kinds:

* **secret** — value lives in the keyring (or AES-encrypted file fallback);
               the LLM only sees the handle name + description. Never inlined
               in the system prompt.
* **value**  — non-sensitive (URL, account id, env name). Inlined directly
               in the system prompt next to skills/agents, so the LLM can
               quote or reference it without a tool call.

The keyring is the primary backend (macOS Keychain / Windows Credential
Locker / Linux Secret Service via the `keyring` package). On broken-keyring
systems an AES-encrypted file fallback exists, gated behind explicit
`talos vault setup`. M55 ships keyring + an injectable in-memory backend
for tests; the file-backend cryptography path is wired into the same
interface so M57 can polish it.
"""

from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


# ── 🧱 schema ─────────────────────────────────────────────────────────


Kind = Literal["secret", "value"]
Scope = Literal["session", "project", "global"]


class VaultEntry(BaseModel):
    """One handle's metadata. For VALUE kind, ``body`` holds the plaintext.
    For SECRET kind, ``body`` is None and the real value lives in the
    backend (keyring) keyed by the entry's storage key."""

    handle: str
    kind: Kind
    description: str = ""
    body: str | None = None  # only for kind="value"
    created_at: str = Field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    scope: Scope = "project"  # filled at write/read time


# ── 🗂️ storage paths ─────────────────────────────────────────────────


def global_dir() -> Path:
    """``~/.talos/`` — first time vault establishes this as the global config
    dir. M57 may add XDG / APPDATA cross-platform polish."""
    return Path.home() / ".talos"


def project_dir() -> Path:
    """``.talos/`` in the current working directory."""
    return Path(".talos")


def _vault_dir(scope: Scope) -> Path | None:
    if scope == "session":
        return None
    if scope == "global":
        return global_dir() / "vault"
    return project_dir() / "vault"


def _index_file(scope: Scope) -> Path | None:
    d = _vault_dir(scope)
    return None if d is None else d / "index.json"


# ── 🆔 scope-namespaced storage keys ──────────────────────────────────


def _project_scope_id() -> str:
    """Per-project namespace for keyring entries — a hash of the absolute
    project path. Prevents handle name collisions between two repos that
    both define ``github_pat``."""
    return hashlib.sha256(str(Path.cwd().resolve()).encode()).hexdigest()[:12]


def _storage_key(scope: Scope, handle: str) -> str:
    if scope == "global":
        return f"talos-global:{handle}"
    if scope == "project":
        return f"talos-project-{_project_scope_id()}:{handle}"
    return f"talos-session:{handle}"


SERVICE_NAME = "talos-vault"


# ── 🔌 backend interface ──────────────────────────────────────────────


class VaultBackend(ABC):
    """Where SECRET *values* live. The index.json files store metadata only;
    the backend stores the actual secret strings keyed by storage_key."""

    @abstractmethod
    def get(self, storage_key: str) -> str | None: ...

    @abstractmethod
    def set(self, storage_key: str, value: str) -> None: ...

    @abstractmethod
    def delete(self, storage_key: str) -> None: ...


class InMemoryBackend(VaultBackend):
    """Test stub + session-scope storage. Never touches disk."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, storage_key: str) -> str | None:
        return self._store.get(storage_key)

    def set(self, storage_key: str, value: str) -> None:
        self._store[storage_key] = value

    def delete(self, storage_key: str) -> None:
        self._store.pop(storage_key, None)


class KeyringBackend(VaultBackend):
    """Wraps the OS keychain via the ``keyring`` package."""

    def __init__(self) -> None:
        try:
            import keyring  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "🔐 vault needs the keyring package — install with: "
                'pip install -e ".[vault]"'
            ) from exc
        self._kr = keyring

    def get(self, storage_key: str) -> str | None:
        return self._kr.get_password(SERVICE_NAME, storage_key)

    def set(self, storage_key: str, value: str) -> None:
        self._kr.set_password(SERVICE_NAME, storage_key, value)

    def delete(self, storage_key: str) -> None:
        try:
            self._kr.delete_password(SERVICE_NAME, storage_key)
        except Exception:
            # Backend differences mean "doesn't exist" raises different
            # exception types; treating delete-of-missing as a no-op is
            # the standard convention.
            pass


# ── 🌐 default backend selection ──────────────────────────────────────


# Module-level singletons. Session backend is per-process; the persistent
# backend is keyring by default but injectable for tests via configure().
_session_backend = InMemoryBackend()
_persistent_backend: VaultBackend | None = None


def get_persistent_backend() -> VaultBackend:
    """Lazy-init the keyring backend. Failures bubble up as RuntimeError
    so callers can present a helpful message rather than mysterious
    AttributeErrors deep in keyring code."""
    global _persistent_backend
    if _persistent_backend is None:
        _persistent_backend = KeyringBackend()
    return _persistent_backend


def configure(*, persistent: VaultBackend | None = None,
              session: VaultBackend | None = None) -> None:
    """Inject backends — primarily for tests. Pass None to leave a backend
    unchanged; call ``configure(persistent=None, session=InMemoryBackend())``
    to reset session storage between tests."""
    global _persistent_backend, _session_backend
    if persistent is not None:
        _persistent_backend = persistent
    if session is not None:
        _session_backend = session


def _backend_for(scope: Scope) -> VaultBackend:
    return _session_backend if scope == "session" else get_persistent_backend()


# ── 📒 index (metadata) read/write ────────────────────────────────────


def _read_index(scope: Scope) -> list[VaultEntry]:
    if scope == "session":
        return [e.model_copy(update={"scope": "session"})
                for e in _session_index]
    f = _index_file(scope)
    if f is None or not f.is_file():
        return []
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out: list[VaultEntry] = []
    for item in raw.get("entries", []):
        try:
            entry = VaultEntry(**item)
        except Exception:
            continue
        entry.scope = scope
        out.append(entry)
    return out


def _write_index(scope: Scope, entries: list[VaultEntry]) -> None:
    if scope == "session":
        global _session_index
        _session_index = [e.model_copy() for e in entries]
        return
    f = _index_file(scope)
    if f is None:
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    payload = {"entries": [e.model_dump(exclude={"scope"}) for e in entries]}
    f.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# Session-scope index lives only in memory.
_session_index: list[VaultEntry] = []


# ── ✍️ CRUD ──────────────────────────────────────────────────────────


def add_entry(handle: str, value: str, *, kind: Kind = "secret",
              description: str = "", scope: Scope = "project") -> VaultEntry:
    """Add a handle. For kind='secret', value goes to the backend; for
    kind='value', value is stored in the index. Refuses to overwrite an
    existing handle in the same scope (use ``remove_entry`` first)."""
    entries = _read_index(scope)
    if any(e.handle == handle for e in entries):
        raise ValueError(
            f"handle {handle!r} already exists in scope={scope!r} — "
            "remove it first to overwrite"
        )
    if kind == "secret":
        _backend_for(scope).set(_storage_key(scope, handle), value)
        body = None
    else:
        body = value
    entry = VaultEntry(
        handle=handle, kind=kind, description=description, body=body,
        scope=scope,
    )
    entries.append(entry)
    _write_index(scope, entries)
    return entry


def remove_entry(handle: str, scope: Scope) -> bool:
    entries = _read_index(scope)
    new = [e for e in entries if e.handle != handle]
    if len(new) == len(entries):
        return False
    # Delete the backend value too (no-op for kind=value)
    _backend_for(scope).delete(_storage_key(scope, handle))
    _write_index(scope, new)
    return True


def list_entries(scope: Scope | None = None) -> list[VaultEntry]:
    """List entries in one scope, or across all three (session/project/global)
    when ``scope`` is None."""
    if scope is not None:
        return _read_index(scope)
    out: list[VaultEntry] = []
    for s in ("session", "project", "global"):
        out.extend(_read_index(s))
    return out


# ── 🔍 lookup with scope chain ────────────────────────────────────────


@dataclass
class Resolved:
    """The result of a successful lookup: the entry, the resolved value,
    and which scope it came from."""

    entry: VaultEntry
    value: str
    scope: Scope


def resolve(handle: str) -> Resolved | None:
    """Look up a handle, walking session → project → global. First-hit wins
    so a session-level handle shadows project, which shadows global.

    Returns ``None`` if the handle isn't defined anywhere. Returns a
    ``Resolved`` with the plaintext value otherwise (which the caller is
    responsible for *not* logging or exposing to the LLM)."""
    for scope in ("session", "project", "global"):
        for entry in _read_index(scope):
            if entry.handle != handle:
                continue
            if entry.kind == "value":
                return Resolved(entry=entry, value=entry.body or "", scope=scope)
            value = _backend_for(scope).get(_storage_key(scope, handle))
            if value is None:
                # Index says it exists but backend doesn't have it — likely
                # the keyring was cleared. Treat as not found rather than
                # silently returning empty string.
                continue
            return Resolved(entry=entry, value=value, scope=scope)
    return None


def all_handles() -> list[VaultEntry]:
    """Every entry across all scopes, with later scopes filtered when an
    earlier scope shadows the same handle (for system-prompt projection).
    """
    seen: set[str] = set()
    out: list[VaultEntry] = []
    for scope in ("session", "project", "global"):
        for entry in _read_index(scope):
            if entry.handle in seen:
                continue
            seen.add(entry.handle)
            out.append(entry)
    return out
