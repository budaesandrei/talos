"""🔄 /evolve — the product-lifecycle ouroboros on top of AI-DLC.

Research has the pieces but nobody published the closed loop (see the
docs): ship → assess debt → grounded market/persona research → new
requirements → feed AI-DLC inception → repeat. /evolve is that loop.

Four phases, each gated by a human (AI-DLC's discipline applied to the
WHOLE lifecycle, not one feature):

1. 🧹 **Debt phase** — analyze tech debt: code smells & complexity (via a
   SonarQube MCP if linked, else lint/complexity tools), plus the
   AI-aftermath sweep: dead code, orphan files, AI-generated cruft.
   (Grounded by the 2026 finding that >90% of issues in AI-written code
   are code smells.)
2. 🔬 **Research phase** — put on different hats. Persona subagents
   (user, stakeholder, marketer, PM) critique the product, GROUNDED in
   fetched evidence — reviews, competitor pages (web_fetch, not
   imagination; the PersonaCite lesson) — not hallucinated agreement.
3. 📋 **Requirements phase** — compile each persona's pain points into a
   requirements brief; human approves/edits.
4. ➡️ **Handoff** — approved requirements become the brief for /plan,
   restarting AI-DLC. The snake eats its tail.

This module defines the phase prompts + the default persona hats; the
runtime drives the gates (it owns the LLM + the human prompt channel).
"""

DEBT_PROMPT = """You are in the DEBT-ASSESSMENT phase. Using read-only tools,
survey this codebase for technical debt and AI-aftermath cruft:

- code smells & high cognitive/cyclomatic complexity (if a SonarQube or
  lint MCP/tool is available, use it; otherwise inspect representative
  files)
- dead code: functions/files not referenced anywhere
- orphan/AI-generated artifacts: stray scratch files, duplicated logic,
  TODO graveyards, generated files that drifted

Produce a prioritized debt report (markdown): each item with severity,
location, and a one-line remediation. End with: DEBT REPORT READY"""

# The personas — each a "hat" the agent wears to find different pain.
PERSONAS = {
    "end-user": "a hands-on daily user who cares about friction, speed, "
                "confusing flows, and missing conveniences",
    "stakeholder": "an exec who cares about ROI, risk, differentiation, "
                   "and whether this moves business metrics",
    "marketer": "a growth/marketing lead who cares about positioning, the "
                "competitive gap, and what reviews say about us vs rivals",
    "product": "a PM who cares about the roadmap, user retention, and the "
               "highest-leverage next features",
}

RESEARCH_PROMPT = """You are wearing the hat of {hat}: {desc}.

GROUND your analysis in evidence — use web_fetch to read real reviews,
competitor product pages, and market signals relevant to this product.
Do NOT invent agreeable feedback. From this persona's perspective, list
the top pain points and the most valuable improvements, each with a
citation or concrete observation. Be specific and a little harsh."""

REQUIREMENTS_PROMPT = """Synthesize the debt report and all persona analyses
below into a prioritized REQUIREMENTS brief for the next development cycle.

Format (markdown):
# Evolution requirements
## Must-fix (debt)        — from the debt phase, highest severity first
## Should-build (value)   — from persona research, highest leverage first
## Could-explore          — promising but unproven
Each item: a crisp requirement + why (which persona/debt it addresses).
End with: REQUIREMENTS READY"""


def is_debt_ready(text: str) -> bool:
    return "DEBT REPORT READY" in (text or "")


def is_requirements_ready(text: str) -> bool:
    return "REQUIREMENTS READY" in (text or "")


def research_prompt(hat: str) -> str:
    return RESEARCH_PROMPT.format(hat=hat, desc=PERSONAS.get(hat, "a user"))
