"""Tests for the shell escape (M59).

Three layers:
1. dispatch() correctly classifies !cmd vs !!cmd vs plain text
2. run_shell_escape executes the command end-to-end (real subprocess,
   simple safe commands like `echo`)
3. vault substitution + scrubbing integration so a secret in a `!`
   command's output gets redacted before landing in history
"""

from __future__ import annotations

from pathlib import Path

import pytest

from talos.tools.shell_escape import EscapeResult, run_shell_escape
from talos.ui.commands import dispatch


# ── 🧭 dispatch classification ────────────────────────────────────────


def test_dispatch_single_bang_returns_shell():
    assert dispatch("!ls -la") == ("shell", "ls -la")


def test_dispatch_double_bang_returns_silent():
    assert dispatch("!!git status") == ("shell-silent", "git status")


def test_dispatch_strips_leading_whitespace_after_bang():
    """`! ls` should work the same as `!ls` — be forgiving about a space."""
    assert dispatch("! ls") == ("shell", "ls")
    assert dispatch("!!   git status") == ("shell-silent", "git status")


def test_dispatch_lone_bang_is_unknown():
    """A bare `!` with nothing after is invalid syntax — surface as
    unknown so the user gets a hint rather than a silent no-op."""
    assert dispatch("!")[0] == "unknown"
    assert dispatch("!!")[0] == "unknown"


def test_dispatch_bang_must_be_at_start():
    """The bang prefix only matters at the very start; `echo !` is a
    normal chat line."""
    assert dispatch("echo !")[0] == "chat"
    assert dispatch("hello world")[0] == "chat"


def test_dispatch_does_not_confuse_bang_with_slash():
    """Slash commands stay slash commands; we only routed the bang
    prefix, not anything else."""
    assert dispatch("/help") == ("builtin", "/help")


# ── 🐚 run_shell_escape ───────────────────────────────────────────────


def test_run_shell_escape_executes_and_captures_output():
    r = run_shell_escape("echo hello-shell-escape")
    assert isinstance(r, EscapeResult)
    assert r.exit_code == 0
    assert "hello-shell-escape" in r.output


def test_silent_mode_skips_history_message():
    r = run_shell_escape("echo hi", silent=True)
    assert r.history_message is None
    assert "hi" in r.output


def test_shared_mode_returns_history_message_with_command_and_output():
    r = run_shell_escape("echo shared-test", silent=False)
    assert r.history_message is not None
    content = str(r.history_message.content)
    assert "[shell]" in content
    assert "echo shared-test" in content
    assert "shared-test" in content


def test_history_message_carries_timestamp_from_m58():
    """The shared-mode HumanMessage should be stamped so M58's
    time-awareness sees it."""
    from talos.agent.time_awareness import timestamp_of

    r = run_shell_escape("echo stamped", silent=False)
    assert r.history_message is not None
    assert timestamp_of(r.history_message) is not None


def test_failed_command_returns_nonzero_exit():
    """A command that exits non-zero must surface that — silently
    swallowing failures would be a footgun."""
    r = run_shell_escape("false")
    assert r.exit_code != 0


# ── 🔐 vault integration (substitution + scrubbing) ──────────────────


def test_vault_substitution_happens_for_user_typed_commands(tmp_path, monkeypatch):
    """`!echo {{value:greeting}}` should substitute the placeholder
    just like the agent's shell tool does — the model isn't in the
    loop so there's no opacity violation."""
    from talos.infra import vault
    from talos.infra.vault import InMemoryBackend

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    vault.configure(persistent=InMemoryBackend(), session=InMemoryBackend())
    vault._session_index.clear()

    vault.add_entry("greeting", "hello-from-vault", kind="value", scope="project")
    r = run_shell_escape("echo {{value:greeting}}")
    assert "hello-from-vault" in r.output
    assert "{{value:greeting}}" not in r.output


def test_scrubber_redacts_secret_in_shared_history(tmp_path, monkeypatch):
    """When `!cmd` happens to echo a known secret value, the scrubber
    redacts it from the message that lands in history — the agent
    never sees the plaintext."""
    from talos.infra import vault
    from talos.infra.vault import InMemoryBackend, RevealedSecrets

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    vault.configure(persistent=InMemoryBackend(), session=InMemoryBackend())
    vault._session_index.clear()
    RevealedSecrets.reset()

    vault.add_entry("leak", "supersecret-value-xyz", kind="secret", scope="project")
    # Trigger registration by substituting once (simulates the agent
    # having used the secret earlier in the session)
    vault.substitute("use {{secret:leak}}")

    # Now the user types a command that echoes the value
    r = run_shell_escape("echo supersecret-value-xyz", silent=False)
    # Raw output (printed to terminal) still has it — the user typed it,
    # they get to see it
    assert "supersecret-value-xyz" in r.output
    # But the version that lands in history must be scrubbed
    history_text = str(r.history_message.content)
    assert "supersecret-value-xyz" not in history_text
    assert "[REDACTED:leak]" in history_text


def test_unresolved_placeholder_is_reported():
    r = run_shell_escape("echo {{secret:does_not_exist}}")
    # Command runs with literal placeholder; missing list flags it
    assert r.missing_handles == ["secret:does_not_exist"]


# ── 📖 /help text mentions shell escape ──────────────────────────────


def test_help_text_documents_shell_escape():
    from talos.ui.commands import help_text
    text = help_text()
    assert "!cmd" in text and "!!cmd" in text
    assert "shell" in text.lower()
