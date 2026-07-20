"""Tests for MCP config handling (M13). No real server needed."""

import json

import pytest

from talos.mcp import _to_adapter_config, load_mcp_config, load_mcp_tools


def test_missing_config_means_no_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_mcp_config() == {}


async def test_no_config_loads_no_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert await load_mcp_tools() == []


def test_transport_inference(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = {
        "mcpServers": {
            "local": {"command": "npx", "args": ["-y", "some-server"]},
            "remote": {"url": "http://localhost:8000/mcp"},
        }
    }
    d = tmp_path / ".talos"
    d.mkdir()
    (d / "mcp.json").write_text(json.dumps(cfg), encoding="utf-8")

    servers = load_mcp_config()
    adapted = _to_adapter_config(servers)
    assert adapted["local"]["transport"] == "stdio"
    assert adapted["remote"]["transport"] == "streamable_http"


def test_broken_json_raises_helpfully(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / ".talos"
    d.mkdir()
    (d / "mcp.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        load_mcp_config()
