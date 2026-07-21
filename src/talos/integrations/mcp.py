"""🔌 MCP — plug external tool servers into Talos.

MCP (Model Context Protocol) is the USB-C of agent tooling: any server
that speaks it (GitHub, Slack, databases, filesystems, …) can expose
tools to any client that speaks it — including Talos.

Configure servers in ``.talos/mcp.json`` (same shape as Claude/Cursor):

    {
      "mcpServers": {
        "everything": { "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-everything"] },
        "remote":     { "url": "http://localhost:8000/mcp" }
      }
    }

``command`` entries are spawned as stdio subprocesses; ``url`` entries are
contacted over streamable HTTP. The adapter turns every remote tool into
a regular LangChain tool, so the graph (and the permission gate — MCP
tools are not read-only-listed, so they require approval) treats them
exactly like built-ins.
"""

import asyncio
import json
from pathlib import Path


def mcp_config_file() -> Path:
    return Path(".talos") / "mcp.json"


def load_mcp_config() -> dict:
    f = mcp_config_file()
    servers = {}
    if f.is_file():
        try:
            servers = json.loads(f.read_text(encoding="utf-8")).get("mcpServers", {})
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid {f}: {exc}") from exc
    # 🔗 merge MCP servers linked from other agents (local wins on name)
    try:
        from talos.integrations.linking import discover_linked_mcp

        for name, spec in discover_linked_mcp().items():
            servers.setdefault(name, spec)
    except Exception:
        pass
    return servers


# keys the langchain-mcp-adapters connection constructors actually accept.
# Other agents' configs carry extra fields (Kiro: "disabled", "autoApprove";
# Cursor: "type"; …) that would blow up as unexpected kwargs — strip them.
_ADAPTER_KEYS = {
    "transport", "command", "args", "env", "cwd",
    "encoding", "encoding_error_handler",
    "url", "headers", "timeout", "sse_read_timeout",
}


def _to_adapter_config(servers: dict) -> dict:
    """Fill in the transport field the adapter needs, drop foreign keys,
    and honor a server's own disabled flag (Kiro-style)."""
    out = {}
    for name, spec in servers.items():
        spec = dict(spec)
        if spec.get("disabled") is True:
            continue  # 🔇 disabled in the source agent stays disabled here
        if "transport" not in spec:
            spec["transport"] = "stdio" if "command" in spec else "streamable_http"
        out[name] = {k: v for k, v in spec.items() if k in _ADAPTER_KEYS}
    return out


async def load_mcp_tools(timeout: float | None = None, on_status=None) -> list:
    """Connect to every configured server and return their tools.

    Servers connect CONCURRENTLY, each under its own timeout — one broken
    or hanging server (a command that isn't installed, an npx waiting on a
    prompt, a dead URL) skips with a warning instead of freezing launch.
    ``on_status``: optional callable for progress lines, so startup is
    never a silent hang.
    """
    servers = load_mcp_config()
    if not servers:
        return []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as exc:
        raise RuntimeError(
            ".talos/mcp.json found but MCP support is not installed — "
            "run: pip install 'talos[mcp]'"
        ) from exc

    cfg = _to_adapter_config(servers)
    if not cfg:
        return []
    if timeout is None:
        from talos.config import settings

        timeout = settings.mcp_timeout
    say = on_status or (lambda _msg: None)
    say(f"🔌 connecting {len(cfg)} MCP server(s)… "
        f"(per-server timeout {timeout:.0f}s)")
    client = MultiServerMCPClient(cfg)

    async def one(name: str):
        try:
            tools = await asyncio.wait_for(
                client.get_tools(server_name=name), timeout
            )
            return tools, None
        except asyncio.TimeoutError:
            return [], f"timed out after {timeout:.0f}s"
        except Exception as exc:  # noqa: BLE001 — a broken server must
            # never take the session down with it
            return [], f"{type(exc).__name__}: {str(exc)[:120]}"

    results = await asyncio.gather(*(one(n) for n in cfg))
    tools: list = []
    for name, (server_tools, err) in zip(cfg, results):
        if err:
            say(f"⚠️  {name}: {err} — skipped")
        else:
            say(f"🔌 {name}: {len(server_tools)} tool(s)")
            tools.extend(server_tools)
    return tools
