"""Tests for workspace awareness (M43)."""

from talos import workspace


def test_snapshot_includes_tree_and_readme(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Cool Project\nDoes things.", encoding="utf-8")
    snap = workspace.snapshot()
    assert "Workspace" in snap
    assert "src/" in snap and "pyproject.toml" in snap
    assert "Cool Project" in snap


def test_snapshot_ignores_noise(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".venv").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    snap = workspace.snapshot()
    assert "app.py" in snap
    assert ".venv" not in snap and "node_modules" not in snap


def test_snapshot_in_system_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("# MyApp", encoding="utf-8")
    from talos.context import build_system_prompt

    prompt = build_system_prompt()
    assert "Workspace" in prompt and "MyApp" in prompt
