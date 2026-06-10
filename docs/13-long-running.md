# 13 · ♾️ Long-running: compaction + graph memory

> Files: `compaction.py`, `graph_memory.py` · Milestones: M33, M34 · Next: [14 — time travel](14-time-travel.md)

The two features that turn Talos from "stateless per session" into an
agent that never runs out of context.

## 🗜️ Compaction (M33)

Every LLM call re-sends the whole history, so a long session eventually
overflows the window and re-bills the prefix each step. Compaction folds
old turns into a summary when context fills up.

The trigger is **exact, not estimated**: the provider reports
`input_tokens` with every reply — the real size of the context it just
read — and we know the model's `max_input_tokens` from `/models`.

```mermaid
flowchart LR
    T["turn N"] --> M{"context ≥ 70%<br/>of max_input_tokens?"}
    M -- no --> G["continue"]
    M -- yes --> S["summarize all but<br/>last keep_recent turns"]
    S --> F["📓 summary + recent → new history"]
    F --> MEM["🕸️ folded turns →<br/>graph memory (M34)"]
```

Tool calls are never split from their results; an existing summary is
merged, not duplicated; the summary call is metered. A `▰▱` fuel gauge in
the rprompt shows how full the context is.

## 🕸️ Graph memory (M34)

Folded turns don't vanish — they flow into a GraphRAG knowledge graph
(Microsoft's *From Local to Global*, 2024):

```mermaid
flowchart TB
    C["compacted chunk"] --> E["🔬 extract: LLM →<br/>topic nodes + relations"]
    E --> K["🕸️ Kuzu graph (Cypher)<br/>+ JSON source of truth"]
    K --> L["🧩 Leiden communities<br/>(igraph)"]
    L --> D{"community<br/>changed?"}
    D -- "dirty only" --> SUM["📝 LLM community summary"]
    D -- clean --> SKIP["skip (cost control)"]
    Q["recall_memory(query)"] --> SUM
    SUM --> LEAF["drill to topics"]
```

The **cost control** is dirty-tracking: a community is re-summarized only
when its membership changes — one LLM call per dirty community per
compaction, all metered. `recall_memory` lets the agent answer about
topics from far behind the compaction horizon; with Kuzu installed it can
also run text2cypher. Everything heavy is optional (`pip install
'talos[memory]'`) and degrades to in-memory logic without it.
