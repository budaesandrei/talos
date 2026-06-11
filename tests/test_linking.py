"""Tests for cross-agent linking (M39)."""

import json

from talos.integrations import linking


def _make_kiro(tmp_path):
    kiro = tmp_path / ".kiro"
    sk = kiro / "skills"
    (sk / "playwright-cli").mkdir(parents=True)
    (sk / "playwright-cli" / "SKILL.md").write_text(
        "---\nname: playwright-cli\ndescription: browser automation\n---\nbody",
        encoding="utf-8")
    # junk folder Kiro would choke on — no SKILL.md
    (sk / ".git").mkdir()
    (sk / ".git" / "config").write_text("[core]", encoding="utf-8")
    (kiro / "settings").mkdir()
    (kiro / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}), encoding="utf-8")
    return kiro


def test_link_and_discover_skips_junk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kiro = _make_kiro(tmp_path)
    linking.add_link(str(kiro))
    skills = linking.discover_linked_skills()
    names = [s["name"] for s in skills]
    assert "playwright-cli" in names
    assert ".git" not in names              # 🧹 junk silently skipped
    assert skills[0]["source"] == ".kiro"


def test_dedup_first_link_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kiro = _make_kiro(tmp_path)
    # a second agent with the same skill name
    cursor = tmp_path / ".cursor" / "skills" / "playwright-cli"
    cursor.mkdir(parents=True)
    (cursor / "SKILL.md").write_text(
        "---\nname: playwright-cli\ndescription: cursor version\n---\n", encoding="utf-8")
    linking.add_link(str(kiro))
    linking.add_link(str(tmp_path / ".cursor"))
    skills = linking.discover_linked_skills()
    pw = [s for s in skills if s["name"] == "playwright-cli"]
    assert len(pw) == 1 and pw[0]["source"] == ".kiro"  # first link wins


def test_linked_mcp_merges(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kiro = _make_kiro(tmp_path)
    linking.add_link(str(kiro))
    mcp = linking.discover_linked_mcp()
    assert "github" in mcp


def test_link_rejects_nondir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "not a directory" in linking.add_link(str(tmp_path / "nope"))
