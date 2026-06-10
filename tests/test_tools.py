"""Tests for the built-in tools (no network needed)."""

from talos.tools.files import edit_file, glob_files, grep, list_dir, read_file, write_file
from talos.tools.shell import shell


def test_write_then_read(tmp_path):
    target = str(tmp_path / "hello.txt")
    write_file.invoke({"path": target, "content": "line one\nline two"})
    out = read_file.invoke({"path": target})
    assert "line one" in out and "line two" in out


def test_edit_requires_unique_anchor(tmp_path):
    target = str(tmp_path / "code.py")
    write_file.invoke({"path": target, "content": "x = 1\nx = 1\n"})
    out = edit_file.invoke({"path": target, "old_text": "x = 1", "new_text": "x = 2"})
    assert "2 times" in out  # ambiguous -> refused

    out = edit_file.invoke({"path": target, "old_text": "x = 1\nx = 1", "new_text": "x = 2"})
    assert "Edited" in out


def test_glob_and_grep(tmp_path):
    (tmp_path / "a.py").write_text("def needle(): pass\n")
    (tmp_path / "b.txt").write_text("hay\n")
    out = glob_files.invoke({"pattern": "**/*.py", "path": str(tmp_path)})
    assert "a.py" in out and "b.txt" not in out

    out = grep.invoke({"pattern": "needle", "path": str(tmp_path)})
    assert "a.py:1:" in out


def test_list_dir(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "f.txt").write_text("hi")
    out = list_dir.invoke({"path": str(tmp_path)})
    assert "sub/" in out and "f.txt" in out


def test_shell_captures_exit_code():
    out = shell.invoke({"command": "echo hello"})
    assert "exit code: 0" in out and "hello" in out
