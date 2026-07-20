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
    """The global config dir, with cross-platform discovery.

    Resolution order:

    1. ``TALOS_HOME`` env var if set (escape hatch for tests + power users)
    2. ``%APPDATA%/talos`` on Windows
    3. ``$XDG_CONFIG_HOME/talos`` if XDG_CONFIG_HOME is set (POSIX)
    4. ``~/.talos`` (the universal default fallback)
    """
    import os as _os
    import sys as _sys

    override = _os.environ.get("TALOS_HOME")
    if override:
        return Path(override)
    if _sys.platform == "win32":
        appdata = _os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "talos"
    else:
        xdg = _os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            return Path(xdg) / "talos"
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


# ── 🔁 substitution + scrubbing (M56) ─────────────────────────────────


import re as _re


class RevealedSecrets:
    """Process-scoped registry of secret values that have been read in this
    session. The scrubber consults this to redact accidental leaks in
    tool output.

    Lives only in memory; never written to disk. Cleared when the process
    exits or when ``reset()`` is called from tests."""

    _values: dict[str, str] = {}  # handle -> plaintext
    _enabled: bool = True

    @classmethod
    def remember(cls, handle: str, value: str) -> None:
        """Called by ``substitute()`` whenever a secret placeholder resolves.
        Empty values aren't registered (would match everything)."""
        if value and len(value) >= 4:  # don't redact 1-3 char "secrets" (false positives)
            cls._values[handle] = value

    @classmethod
    def set_enabled(cls, enabled: bool) -> None:
        cls._enabled = enabled

    @classmethod
    def is_enabled(cls) -> bool:
        return cls._enabled

    @classmethod
    def scrub(cls, text: str) -> str:
        """Replace any known secret value in ``text`` with ``[REDACTED:handle]``.

        Sorted by length descending so longer matches replace first —
        prevents partial-overlap weirdness when one secret is a substring
        of another. Honest-leak defense only: a model could trivially
        base64-encode the value to bypass this. The docs say so plainly.
        """
        if not cls._enabled or not text or not cls._values:
            return text
        out = text
        for handle, value in sorted(cls._values.items(), key=lambda kv: -len(kv[1])):
            if value and value in out:
                out = out.replace(value, f"[REDACTED:{handle}]")
        return out

    @classmethod
    def reset(cls) -> None:
        """Test helper — fully clear state."""
        cls._values.clear()
        cls._enabled = True

    @classmethod
    def revealed_count(cls) -> int:
        return len(cls._values)


_PLACEHOLDER_RE = _re.compile(r"\{\{(secret|value):([a-zA-Z0-9_.\-]+)\}\}")


def substitute(text: str) -> tuple[str, list[str]]:
    """Replace ``{{secret:name}}`` / ``{{value:name}}`` placeholders in
    ``text`` with the corresponding vault values.

    Returns ``(substituted_text, missing_placeholders)``. Missing handles
    are left as-is in the text (so the failure is visible in the
    executed command rather than silently becoming an empty string).

    Side effect: every successfully-resolved SECRET is registered with
    ``RevealedSecrets`` so the scrubber can redact it from future tool
    output. VALUE substitutions are not registered (not sensitive)."""
    missing: list[str] = []

    def _sub(m: "_re.Match[str]") -> str:
        kind = m.group(1)
        handle = m.group(2)
        resolved = resolve(handle)
        if resolved is None:
            missing.append(f"{kind}:{handle}")
            return m.group(0)
        if resolved.entry.kind != kind:
            # Asked for SECRET but it's a VALUE (or vice versa) — leave it
            # so the command fails visibly with the placeholder still in it.
            missing.append(f"{kind}:{handle} (is {resolved.entry.kind})")
            return m.group(0)
        if kind == "secret":
            RevealedSecrets.remember(handle, resolved.value)
        return resolved.value

    return _PLACEHOLDER_RE.sub(_sub, text), missing


# ── 🪞 system-prompt projection ───────────────────────────────────────


def vault_summary(max_chars: int = 4000) -> str:
    """The compact handle index for the system prompt — similar to
    ``skills_summary``. Lists every handle (deduped by scope shadowing).

    For SECRET handles, shows the description only — the value never
    leaves the keyring. For VALUE handles, inlines the value so the
    model can reference it directly without a tool call. Soft size
    budget enforced; truncates long lists with a `(N more)` tail.
    """
    handles = all_handles()
    if not handles:
        return ""
    lines = [
        "## Vault (handles you can use)",
        "_For SECRETs, write `{{secret:<handle>}}` inside a shell command "
        "or code block — the shell tool substitutes the plaintext at exec "
        "time, so the value never enters your context. For VALUEs, you can "
        "reference the value below directly. `vault_get(handle)` is also "
        "available for VALUE reads._",
        "",
    ]
    budget = max_chars - sum(len(l) + 1 for l in lines)
    for h in handles:
        if h.kind == "secret":
            line = (f"- 🔒 SECRET `{h.handle}` "
                    f"({h.scope}) — {h.description or '(no description)'}")
        else:
            preview = (h.body or "")
            if len(preview) > 100:
                preview = preview[:97] + "…"
            tail = f"  ({h.description})" if h.description else ""
            line = f"- 📝 VALUE `{h.handle}` ({h.scope}) = `{preview}`{tail}"
        if budget - len(line) - 1 < 100:
            remaining = len(handles) - handles.index(h)
            lines.append(f"  (… {remaining} more handle(s) elided)")
            break
        lines.append(line)
        budget -= len(line) + 1
    return "\n".join(lines).rstrip() + "\n"
