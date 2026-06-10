"""🧠 Long-term memory — facts that survive across sessions.

The simplest possible implementation that demonstrates the real pattern:
one markdown file (``.talos/memory.md``) whose entire content is injected
into the system prompt, plus a ``save_memory`` tool the model calls when
it learns something worth keeping ("user prefers tabs", "API lives at…").

Rules vs memory: **rules** (TALOS.md) are written by the *human*;
**memory** is written by the *agent*.
"""

from datetime import datetime
from pathlib import Path


def memory_file() -> Path:
    return Path(".talos") / "memory.md"


def load_memory() -> str:
    f = memory_file()
    return f.read_text(encoding="utf-8").strip() if f.is_file() else ""


def append_memory(fact: str) -> str:
    f = memory_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    with f.open("a", encoding="utf-8") as fh:
        fh.write(f"- ({stamp}) {fact.strip()}\n")
    return f"Remembered: {fact.strip()}"
