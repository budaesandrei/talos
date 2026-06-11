"""Tests for environment detection (M21)."""

from talos.infra import environment


def test_shell_detection_respects_override(monkeypatch):
    monkeypatch.setattr(environment.settings, "shell", "powershell")
    assert environment.detect_shell() == "powershell"
    cmd = environment.shell_command("ls")
    assert cmd[0] in ("powershell", "pwsh") and cmd[-1] == "ls"


def test_describe_mentions_shell_syntax(monkeypatch):
    monkeypatch.setattr(environment.settings, "shell", "powershell")
    info = environment.describe()
    assert "powershell" in info
    assert "NOT '&&'" in info  # the syntax hint that started all this


def test_bash_command_shape(monkeypatch):
    monkeypatch.setattr(environment.settings, "shell", "bash")
    cmd = environment.shell_command("echo hi && echo there")
    assert cmd == ["bash", "-c", "echo hi && echo there"]
