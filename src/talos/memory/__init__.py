"""🧠 Persistence & recall: sessions, notes, compaction, graph memory, checkpoints."""

# Legacy top-level API — `from talos.memory import load_memory` is used by the
# legacy mirrors (context.py, runtime/runner.py). This package shadows the old
# talos/memory.py module, so the name must be re-exported here to stay in sync.
from talos.memory.notes import append_memory, load_memory, memory_file

__all__ = ["append_memory", "load_memory", "memory_file"]
