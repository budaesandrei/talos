"""🧜 Mermaid preview — terminals can't draw mermaid, browsers can.

There is no real text-mode mermaid renderer: the diagrams need a layout
engine. So Talos does the pragmatic thing every terminal tool does —
show the block as code in the TUI, and offer ``/mermaid`` to render the
real diagram in the default browser via mermaid.js (CDN, no install).
"""

import html
import re
import shutil
import subprocess
import tempfile
import webbrowser
from pathlib import Path

MERMAID_RE = re.compile(r"```mermaid\s+(.*?)```", re.S)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>talos 🧜 mermaid</title></head>
<body style="font-family: sans-serif; background: #fdfdfd;">
{blocks}
<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
mermaid.initialize({{ startOnLoad: true }});
</script>
</body></html>
"""


def ascii_render(block: str) -> str | None:
    """Render via the optional `mermaid-ascii` binary (Go tool:
    github.com/AlexanderGrooff/mermaid-ascii). Returns None when the tool
    is missing or chokes on the diagram type (it supports a subset)."""
    exe = shutil.which("mermaid-ascii")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe], input=block, capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",  # box-drawing chars ≠ cp1252
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout.strip() else None


def extract_mermaid(text: str) -> list[str]:
    """Pull the contents of every ```mermaid fenced block."""
    return [m.strip() for m in MERMAID_RE.findall(text or "")]


def build_html(blocks: list[str]) -> str:
    rendered = "\n<hr>\n".join(
        f'<pre class="mermaid">{html.escape(b)}</pre>' for b in blocks
    )
    return PAGE.format(blocks=rendered)


def open_in_browser(blocks: list[str]) -> str:
    """Write a temp HTML page with the diagrams and open it. Returns the path."""
    f = Path(tempfile.gettempdir()) / "talos-mermaid.html"
    f.write_text(build_html(blocks), encoding="utf-8")
    webbrowser.open(f.as_uri())
    return str(f)
