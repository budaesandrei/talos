"""Tests for the vault (M55).

The persistent backend is replaced with an InMemoryBackend so no real
keyring is touched. Each test isolates state via monkeypatch.chdir for
project scope and a tmp HOME override for global scope.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from talos.infra import vault
from talos.infra.vault import InMemoryBackend, VaultEntry


@pytest.fixture(autouse=True)
def _isolated_vault(tmp_path, monkeypatch):
    """Run every test against a fresh in-memory backend and an isolated
    HOME (so global scope writes don't escape into the real ~/.talos/)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    # Reset module state
    vault.configure(persistent=InMemoryBackend(), session=InMemoryBackend())
    vault._session_index.clear()
    yield


# ── 🧱 add / list / remove ────────────────────────────────────────────


def test_add_and_list_value_entry():
    e = vault.add_entry(
        "prod_url", "https://example.com", kind="value",
        description="prod base URL", scope="project",
    )
    assert e.handle == "prod_url"
    assert e.body == "https://example.com"
    entries = vault.list_entries("project")
    assert len(entries) == 1 and entries[0].handle == "prod_url"


def test_add_secret_does_not_store_value_in_index():
    """Critical: the on-disk index must never contain a secret value."""
    vault.add_entry("api_key", "sk-supersecret", kind="secret", scope="project")
    index_path = vault._index_file("project")
    raw = index_path.read_text(encoding="utf-8")
    assert "sk-supersecret" not in raw, "secret leaked into index.json!"
    # But the metadata IS in the index
    assert "api_key" in raw


def test_add_refuses_duplicate_handle_in_same_scope():
    vault.add_entry("k", "v1", kind="secret", scope="project")
    with pytest.raises(ValueError, match="already exists"):
        vault.add_entry("k", "v2", kind="secret", scope="project")


def test_remove_entry():
    vault.add_entry("k", "v", kind="secret", scope="project")
    assert vault.remove_entry("k", "project") is True
    assert vault.list_entries("project") == []
    assert vault.remove_entry("k", "project") is False


# ── 🔍 scope chain ────────────────────────────────────────────────────


def test_resolve_walks_session_project_global():
    """First-hit wins, in order session → project → global."""
    vault.add_entry("k", "global-value", kind="value", scope="global")
    r = vault.resolve("k")
    assert r is not None and r.value == "global-value" and r.scope == "global"

    vault.add_entry("k", "project-value", kind="value", scope="project")
    r = vault.resolve("k")
    assert r.value == "project-value" and r.scope == "project"

    vault.add_entry("k", "session-value", kind="value", scope="session")
    r = vault.resolve("k")
    assert r.value == "session-value" and r.scope == "session"


def test_resolve_returns_none_for_unknown():
    assert vault.resolve("nope") is None


def test_resolve_secret_pulls_from_backend():
    vault.add_entry("api_key", "sk-real-value", kind="secret", scope="project")
    r = vault.resolve("api_key")
    assert r is not None
    assert r.value == "sk-real-value"
    assert r.entry.kind == "secret"
    assert r.entry.body is None  # bodies stay None for secrets


def test_resolve_returns_none_if_index_orphans_backend():
    """If the index references a handle but the backend forgot it (keyring
    cleared), we should treat the handle as missing rather than returning
    empty string."""
    vault.add_entry("orphan", "value", kind="secret", scope="project")
    # Simulate keyring loss
    vault.get_persistent_backend().delete(vault._storage_key("project", "orphan"))
    assert vault.resolve("orphan") is None


# ── 🆔 project namespacing ────────────────────────────────────────────


def test_project_scope_id_changes_with_cwd(tmp_path, monkeypatch):
    """Two repos at different paths get different namespaces, so handles
    don't collide in the keyring."""
    a = tmp_path / "repo_a"
    b = tmp_path / "repo_b"
    a.mkdir()
    b.mkdir()

    monkeypatch.chdir(a)
    id_a = vault._project_scope_id()
    monkeypatch.chdir(b)
    id_b = vault._project_scope_id()
    assert id_a != id_b


# ── 🌐 all_handles (system-prompt projection helper) ─────────────────


def test_all_handles_dedupes_by_shadowing():
    vault.add_entry("k", "global", kind="value", scope="global")
    vault.add_entry("k", "project", kind="value", scope="project")
    vault.add_entry("other", "only-here", kind="value", scope="global")
    handles = vault.all_handles()
    handle_names = [h.handle for h in handles]
    assert handle_names == ["k", "other"]
    # The project-scope k wins over the global-scope k
    k = next(h for h in handles if h.handle == "k")
    assert k.scope == "project"
    assert k.body == "project"


# ── 🔧 vault_get tool ────────────────────────────────────────────────


def test_vault_get_returns_values():
    from talos.tools.vault_tool import vault_get
    vault.add_entry("url", "https://x.example.com", kind="value", scope="project")
    assert vault_get.invoke({"handle": "url"}) == "https://x.example.com"


def test_vault_get_refuses_secrets():
    from talos.tools.vault_tool import vault_get
    vault.add_entry("api_key", "sk-secret", kind="secret", scope="project")
    result = vault_get.invoke({"handle": "api_key"})
    assert result.startswith("Error")
    assert "SECRET" in result and "sk-secret" not in result


def test_vault_get_handles_unknown_handle():
    from talos.tools.vault_tool import vault_get
    result = vault_get.invoke({"handle": "nope"})
    assert result.startswith("Error") and "nope" in result


# ── 💾 persistence round-trip on disk ────────────────────────────────


def test_project_index_persists_across_calls(tmp_path, monkeypatch):
    vault.add_entry(
        "h", "v", kind="value", description="d", scope="project",
    )
    # Re-read from disk (the function always re-reads, but be explicit)
    entries = vault._read_index("project")
    assert [e.handle for e in entries] == ["h"]
    assert entries[0].description == "d"
    assert entries[0].body == "v"


def test_global_index_persists_in_home(tmp_path, monkeypatch):
    vault.add_entry("ghpat", "secret-value", kind="secret",
                    description="pat", scope="global")
    # The global index file should land in our isolated HOME
    expected = tmp_path / "home" / ".talos" / "vault" / "index.json"
    assert expected.is_file()
    # And it must NOT contain the plaintext
    assert "secret-value" not in expected.read_text(encoding="utf-8")
    # But the metadata IS there
    assert "ghpat" in expected.read_text(encoding="utf-8")


# ── 🧩 session scope behavior ────────────────────────────────────────


def test_session_scope_is_in_memory_only(tmp_path):
    vault.add_entry("only-session", "v", kind="secret", scope="session")
    # No project/global dirs should have been created
    assert not (tmp_path / ".talos" / "vault").exists()
    assert not (tmp_path / "home" / ".talos" / "vault").exists()
    # But the handle resolves
    r = vault.resolve("only-session")
    assert r is not None and r.value == "v" and r.scope == "session"


# ── 🔁 substitution (M56) ─────────────────────────────────────────────


def test_substitute_resolves_secret_placeholder():
    vault.add_entry("api_key", "sk-12345-real", kind="secret", scope="project")
    out, missing = vault.substitute(
        "curl -H 'Authorization: Bearer {{secret:api_key}}' x"
    )
    assert "sk-12345-real" in out
    assert "{{secret:api_key}}" not in out
    assert missing == []


def test_substitute_resolves_value_placeholder():
    vault.add_entry("url", "https://x.example.com", kind="value", scope="project")
    out, missing = vault.substitute("curl {{value:url}}/api")
    assert out == "curl https://x.example.com/api"
    assert missing == []


def test_substitute_leaves_unknown_handle_in_place():
    """A missing handle stays as the literal placeholder so the failure
    is visible in the executed command, not silently empty."""
    out, missing = vault.substitute("psql {{secret:nope}}")
    assert "{{secret:nope}}" in out
    assert missing == ["secret:nope"]


def test_substitute_refuses_kind_mismatch():
    """Asking for SECRET when the handle is a VALUE (or vice versa)
    must leave the placeholder in and flag missing — never silently
    return the wrong-kind value."""
    vault.add_entry("url", "https://x.example.com", kind="value", scope="project")
    out, missing = vault.substitute("curl {{secret:url}}")
    assert "{{secret:url}}" in out
    assert any("url" in m and "is value" in m for m in missing)


def test_substitute_handles_multiple_placeholders():
    vault.add_entry("k1", "alpha", kind="secret", scope="project")
    vault.add_entry("k2", "beta", kind="secret", scope="project")
    out, missing = vault.substitute("{{secret:k1}} and {{secret:k2}}")
    assert out == "alpha and beta"
    assert missing == []


def test_substitute_registers_revealed_secrets_for_scrubbing():
    """Resolving a secret must register it with RevealedSecrets so the
    scrubber can redact it from future tool outputs."""
    vault.RevealedSecrets.reset()
    vault.add_entry("ghpat", "ghp_RealishToken123", kind="secret", scope="project")
    vault.substitute("git push {{secret:ghpat}}")
    assert vault.RevealedSecrets.revealed_count() == 1


def test_substitute_does_not_register_value_handles():
    """Non-secret values aren't registered — they're not sensitive."""
    vault.RevealedSecrets.reset()
    vault.add_entry("url", "https://x.example.com", kind="value", scope="project")
    vault.substitute("curl {{value:url}}")
    assert vault.RevealedSecrets.revealed_count() == 0


def test_substitute_returns_string_unchanged_when_no_placeholders():
    out, missing = vault.substitute("plain old text")
    assert out == "plain old text"
    assert missing == []


# ── 🧼 scrubbing ──────────────────────────────────────────────────────


def test_scrub_replaces_revealed_secret_with_handle_placeholder():
    vault.RevealedSecrets.reset()
    vault.add_entry("api_key", "sk-leaky-secret", kind="secret", scope="project")
    vault.substitute("use {{secret:api_key}}")  # registers the secret
    text = "Result: sk-leaky-secret found in output"
    assert vault.RevealedSecrets.scrub(text) == "Result: [REDACTED:api_key] found in output"


def test_scrub_is_noop_when_no_match():
    vault.RevealedSecrets.reset()
    vault.add_entry("api_key", "sk-not-here", kind="secret", scope="project")
    vault.substitute("use {{secret:api_key}}")
    text = "nothing sensitive in here"
    assert vault.RevealedSecrets.scrub(text) == text


def test_scrub_respects_disabled_flag():
    vault.RevealedSecrets.reset()
    vault.add_entry("api_key", "sk-leaky", kind="secret", scope="project")
    vault.substitute("use {{secret:api_key}}")
    vault.RevealedSecrets.set_enabled(False)
    text = "leaking sk-leaky here"
    assert vault.RevealedSecrets.scrub(text) == text
    vault.RevealedSecrets.set_enabled(True)
    assert "[REDACTED" in vault.RevealedSecrets.scrub(text)


def test_scrub_replaces_longer_secrets_first():
    """When one secret value is a substring of another, the longer one
    must replace first — otherwise we'd partially redact and corrupt
    the second match."""
    vault.RevealedSecrets.reset()
    vault.add_entry("short", "abcdef", kind="secret", scope="project")
    vault.add_entry("long", "abcdefghij", kind="secret", scope="project")
    vault.substitute("{{secret:short}} {{secret:long}}")
    out = vault.RevealedSecrets.scrub("xxx abcdefghij yyy abcdef zzz")
    assert "[REDACTED:long]" in out
    assert "[REDACTED:short]" in out


def test_scrub_ignores_short_secrets():
    """Length floor prevents 3-char "secrets" like 'abc' from matching
    every other word in tool output."""
    vault.RevealedSecrets.reset()
    vault.add_entry("tiny", "abc", kind="secret", scope="project")
    vault.substitute("use {{secret:tiny}}")
    text = "abc and abc and abc"
    assert vault.RevealedSecrets.scrub(text) == text  # not registered


# ── 🪞 system-prompt projection ──────────────────────────────────────


def test_vault_summary_lists_handles_with_kind_markers():
    vault.add_entry("api_key", "sk-real", kind="secret", description="prod key",
                    scope="project")
    vault.add_entry("url", "https://x.example.com", kind="value",
                    description="dashboard", scope="project")
    summary = vault.vault_summary()
    assert "Vault" in summary
    assert "api_key" in summary
    assert "url" in summary
    # SECRET value MUST NOT appear in the summary
    assert "sk-real" not in summary
    # VALUE body IS inlined
    assert "https://x.example.com" in summary
    # Kind markers
    assert "SECRET" in summary
    assert "VALUE" in summary


def test_vault_summary_empty_when_no_handles():
    assert vault.vault_summary() == ""


def test_vault_summary_includes_scope():
    vault.add_entry("g", "v", kind="value", scope="global")
    vault.add_entry("p", "v", kind="value", scope="project")
    summary = vault.vault_summary()
    assert "(project)" in summary
    assert "(global)" in summary


# ── 🐚 shell tool integration ────────────────────────────────────────


def test_shell_substitutes_placeholders_before_exec():
    """Smoke test: a shell command with a {{value:..}} placeholder should
    have the value substituted before exec."""
    from talos.tools.shell import shell
    vault.add_entry("greeting", "hello-vault", kind="value", scope="project")
    out = shell.invoke({"command": "echo {{value:greeting}}"})
    assert "hello-vault" in out


def test_shell_warns_on_unresolved_placeholder():
    """When a placeholder doesn't resolve, the command runs anyway with
    the literal placeholder, and the output is prefixed with a warning."""
    from talos.tools.shell import shell
    out = shell.invoke({"command": "echo {{secret:does_not_exist}}"})
    assert "unresolved vault placeholders" in out
    assert "secret:does_not_exist" in out


def test_shell_output_is_scrubbed():
    """If a shell command happens to echo a known secret value, the
    scrubber (called from tools_node, but the substitution side-effect
    means future invocations would scrub) replaces it.

    Note: this test is at the substitution level, since the scrubber
    runs at graph-builder layer not in the tool itself."""
    vault.RevealedSecrets.reset()
    vault.add_entry("leak_me", "abc12345leaky", kind="secret", scope="project")
    # Trigger registration
    vault.substitute("use {{secret:leak_me}}")
    # Now imagine some other tool returned the value verbatim
    leaked_output = "Error: connection failed for abc12345leaky"
    scrubbed = vault.RevealedSecrets.scrub(leaked_output)
    assert "abc12345leaky" not in scrubbed
    assert "[REDACTED:leak_me]" in scrubbed
