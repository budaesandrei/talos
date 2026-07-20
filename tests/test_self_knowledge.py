"""Tests for self-knowledge (M52).

The tricky part: ``walk_source`` walks Talos's *real* source tree by
default. That's deliberate — the manifest must reflect reality. But for
focused unit tests we point ``walk_source`` at a tiny synthetic tree
inside ``tmp_path`` so assertions are bounded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from talos.lifecycle import self_knowledge as sk
from talos.tools.self_tool import read_self


# ── 🧪 helpers ─────────────────────────────────────────────────────────


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _build_synthetic_tree(root: Path) -> None:
    """A miniature talos/ source tree to walk in tests."""
    _write(root / "__init__.py", '"""🧠 Top-level package."""\n')
    _write(root / "cli.py", '"""🖥️ CLI front door."""\nimport sys\n')
    _write(
        root / "memory" / "__init__.py",
        '"""💾 Memory subpackage. The long-term store."""\n',
    )
    _write(
        root / "memory" / "sessions.py",
        '"""💾 Conversations that survive a restart.\n\nMore detail follows."""\n',
    )
    _write(
        root / "tools" / "files.py",
        '"""🔧 File tools — read, write, edit. Battle-tested."""\n',
    )
    # A file with no docstring → should be skipped.
    _write(root / "no_doc.py", "x = 1\n")
    # A file with a syntax error → also skipped (no crash).
    _write(root / "broken.py", "def(:\n")
    # __pycache__ contents → ignored
    _write(root / "__pycache__" / "junk.py", '"""junk"""')


# ── 🔍 walk_source ─────────────────────────────────────────────────────


def test_walk_extracts_first_sentence_and_skips_noise(tmp_path):
    root = tmp_path / "talos"
    _build_synthetic_tree(root)
    facts = sk.walk_source(root)
    paths = {f.path for f in facts}
    # docstring'd files appear
    assert "src/talos/__init__.py" in paths
    assert "src/talos/cli.py" in paths
    assert "src/talos/memory/sessions.py" in paths
    assert "src/talos/tools/files.py" in paths
    # files without a docstring or with a syntax error are skipped
    assert "src/talos/no_doc.py" not in paths
    assert "src/talos/broken.py" not in paths
    # pycache is ignored
    assert not any("__pycache__" in p for p in paths)


def test_first_sentence_trims_to_one_line():
    sessions = next(
        f for f in sk.walk_source(_make_tree(_temp_root()))
        if f.path.endswith("sessions.py")
    )
    assert sessions.purpose == "💾 Conversations that survive a restart."


def _temp_root() -> Path:
    import tempfile
    d = Path(tempfile.mkdtemp()) / "talos"
    return d


def _make_tree(root: Path) -> Path:
    _build_synthetic_tree(root)
    return root


def test_package_attribution(tmp_path):
    root = tmp_path / "talos"
    _build_synthetic_tree(root)
    by_path = {f.path: f for f in sk.walk_source(root)}
    assert by_path["src/talos/cli.py"].package == "core"
    assert by_path["src/talos/memory/sessions.py"].package == "memory"
    assert by_path["src/talos/tools/files.py"].package == "tools"


def test_dotted_module_names(tmp_path):
    root = tmp_path / "talos"
    _build_synthetic_tree(root)
    by_path = {f.path: f for f in sk.walk_source(root)}
    assert by_path["src/talos/cli.py"].module == "talos.cli"
    assert by_path["src/talos/__init__.py"].module == "talos"
    assert by_path["src/talos/memory/__init__.py"].module == "talos.memory"
    assert by_path["src/talos/memory/sessions.py"].module == "talos.memory.sessions"


# ── 💾 manifest cache ──────────────────────────────────────────────────


def test_manifest_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    facts = [
        sk.ModuleFact(path="src/talos/x.py", package="core",
                      purpose="🪞 a fact.", module="talos.x"),
    ]
    sk.save_manifest(facts)
    assert sk.manifest_file().is_file()
    loaded = sk.load_manifest_cached()
    assert loaded == facts


def test_manifest_uses_cache_when_fresh(tmp_path, monkeypatch):
    """The on-disk cache survives, and a stale check on a tmp dir returns
    True (no source) — so manifest() regenerates against the real tree."""
    monkeypatch.chdir(tmp_path)
    f1 = sk.manifest()
    assert len(f1) > 0
    # Second call with cache present and source unchanged: returns the
    # same facts without re-walking (we can't observe directly, but the
    # output is identical and the cache file exists).
    assert sk.manifest_file().is_file()
    f2 = sk.manifest()
    assert [x.path for x in f1] == [x.path for x in f2]


def test_force_refresh_overwrites_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sk.save_manifest([
        sk.ModuleFact(path="src/talos/fake.py", package="core",
                      purpose="stale.", module="talos.fake")
    ])
    facts = sk.manifest(force_refresh=True)
    paths = {f.path for f in facts}
    assert "src/talos/fake.py" not in paths
    assert any(p.startswith("src/talos/lifecycle/") for p in paths)


# ── 🪞 manifest_summary (system-prompt projection) ────────────────────


def test_manifest_summary_groups_by_package(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    summary = sk.manifest_summary()
    assert "## Self-knowledge" in summary
    # The packages we know exist should all be headed
    assert "**core/**" in summary
    assert "**agent/**" in summary
    assert "**memory/**" in summary
    assert "**lifecycle/**" in summary


def test_manifest_summary_respects_budget(tmp_path, monkeypatch):
    """A tight budget should produce a short string with truncation
    markers rather than blowing past the limit."""
    monkeypatch.chdir(tmp_path)
    summary = sk.manifest_summary(max_chars=800)
    # Soft budget: we add some headers/truncation overhead, but the
    # output should stay roughly in range — well under double.
    assert len(summary) < 1600, f"summary too large under tight budget: {len(summary)}"


# ── 📖 deep_read + read_self tool ─────────────────────────────────────


def test_deep_read_accepts_short_form():
    text = sk.deep_read("lifecycle/scheduling.py")
    assert "scheduling" in text.lower()


def test_deep_read_accepts_full_form():
    text = sk.deep_read("src/talos/lifecycle/scheduling.py")
    assert "scheduling" in text.lower()


def test_deep_read_refuses_path_outside_tree():
    with pytest.raises(ValueError, match="outside"):
        sk.deep_read("../../etc/passwd")
    with pytest.raises(ValueError, match="outside"):
        sk.deep_read("/etc/passwd")


def test_deep_read_missing_file_raises():
    with pytest.raises(ValueError, match="no such file"):
        sk.deep_read("memory/does_not_exist.py")


def test_read_self_tool_returns_text():
    result = read_self.invoke({"file_path": "lifecycle/scheduling.py"})
    assert "scheduling" in result.lower()
    assert not result.startswith("Error")


def test_read_self_tool_handles_bad_path_gracefully():
    """The tool must return an error string, never raise — the model
    gets a readable failure instead of a crashed tool call."""
    result = read_self.invoke({"file_path": "/etc/passwd"})
    assert result.startswith("Error")


# ── 🖥️ context.py wiring ──────────────────────────────────────────────


def test_system_prompt_includes_self_knowledge(tmp_path, monkeypatch):
    """The build_system_prompt path must pick up manifest_summary."""
    monkeypatch.chdir(tmp_path)
    from talos.agent.context import build_system_prompt
    prompt = build_system_prompt()
    assert "Self-knowledge" in prompt
    # And it should reference at least one real Talos file path
    assert "src/talos/" in prompt
