# 15 · 🔄 /evolve — the AI-DLC ouroboros

> Files: `evolve.py` + `run_evolve()` · Milestone: M44 (capstone)

The research has every piece but nobody published the closed loop. `/evolve`
is the whole product lifecycle as a gated cycle on top of AI-DLC:

```mermaid
flowchart TB
    SHIP["shipped product"] --> D["🧹 debt phase<br/>code smells · complexity · dead code · AI cruft"]
    D --> G1{"human gate"}
    G1 --> R["🔬 persona research (parallel)<br/>end-user · stakeholder · marketer · PM<br/>grounded in real reviews/competitors (web_fetch)"]
    R --> REQ["📋 requirements brief<br/>must-fix · should-build · could-explore"]
    REQ --> G2{"human gate"}
    G2 --> PLAN["➡️ /plan (AI-DLC inception)"]
    PLAN --> SHIP
```

**Grounding is the design's spine.** Tech-debt analysis leans on the 2026
finding that >90% of issues in AI-written code are smells ([ACE](https://arxiv.org/html/2507.03536v1),
[RefAgent](https://arxiv.org/pdf/2511.03153)). Persona research is
GROUNDED in fetched evidence — real reviews and competitor pages, never
hallucinated agreement — the [PersonaCite](https://arxiv.org/pdf/2601.22288)/
[Elicitron](https://www.researchgate.net/publication/386895033) lesson.
Every phase is human-gated: AI-DLC's approval discipline applied to the
whole lifecycle, not one feature.

It reuses everything: parallel personas via M41 teams, SonarQube via an
M39-linked MCP, the M24 planner and M36 verifier on the handoff.
