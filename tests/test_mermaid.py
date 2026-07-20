"""Tests for markdown/mermaid TUI support (M16)."""

from talos.mermaid import build_html, extract_mermaid


def test_extracts_fenced_mermaid_blocks():
    text = (
        "Here is the flow:\n```mermaid\nflowchart LR\n  A --> B\n```\n"
        "and another\n```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n"
        "```python\nprint('not mermaid')\n```"
    )
    blocks = extract_mermaid(text)
    assert len(blocks) == 2
    assert blocks[0].startswith("flowchart LR")
    assert "sequenceDiagram" in blocks[1]


def test_no_blocks_in_plain_text():
    assert extract_mermaid("no diagrams here") == []
    assert extract_mermaid("") == []


def test_html_page_escapes_content():
    html_page = build_html(["flowchart LR\n  A --> B"])
    assert 'class="mermaid"' in html_page
    assert "A --&gt; B" in html_page          # escaped, not raw
    assert "mermaid.initialize" in html_page
