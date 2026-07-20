"""🪞 read_self — let the agent read its own source.

Pairs with ``manifest_summary()`` in the system prompt: the index tells
the agent *what* exists; this tool fetches the *content* of any file in
the tree on demand.

Sanity rails live in ``self_knowledge.deep_read``: paths outside
``src/talos/`` are refused, missing files raise a readable error. This
tool is intentionally read-only — self-*editing* lives in the M53/M54
worktree-sandboxed flow, not here.
"""

from langchain_core.tools import tool


@tool
def read_self(file_path: str) -> str:
    """Read a file from Talos's own source tree.

    Use this when you need to see the full implementation of one of the
    files listed in the "Self-knowledge" section of your system prompt.
    Accepts paths like ``src/talos/memory/sessions.py`` or the short form
    ``memory/sessions.py``. Returns the file's full text.

    Refuses paths outside ``src/talos/`` so you can't accidentally read
    arbitrary files on disk through this tool — for that, ``read_file``
    is the right tool with the right permission gate.
    """
    from talos.lifecycle.self_knowledge import deep_read

    try:
        return deep_read(file_path)
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001 — readable error beats a stack trace
        return f"Error reading {file_path!r}: {type(exc).__name__}: {exc}"
