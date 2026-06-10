"""🗺️ /plan — planning mode, AI-DLC style.

AWS's AI-DLC (AI-Driven Development Lifecycle) reframes how agents and
humans split the work: the AI *drives* — proposing plans and asking
clarifying questions — and the human *approves at every gate*. Work is
expressed as **Units of Work** executed in short "bolts", not sprints.

Talos bakes in a lightweight version:

1. 🔍 **Elaborate** (read-only): the planner inspects the project and, if
   requirements are fuzzy, asks up to 3 sharp questions first — that's
   AI-DLC's "mob elaboration" in single-player form.
2. 🗺️ The plan arrives as Units of Work with acceptance criteria, saved
   to ``.talos/plans/`` for the record.
3. 🚦 **Human gate**: you approve, revise, or park it.
4. 🔨 **Construct**: the approved plan runs as a normal agent turn — full
   tools, permission gate, interjections all apply.
"""

from datetime import datetime
from pathlib import Path

READY_MARKER = "PLAN READY"

ELABORATION_PROMPT = f"""You are in PLANNING MODE (AI-DLC style). You are the
planner: investigate, clarify, and design — but execute nothing.

Rules:
- You may only use read-only tools (read files, search, list). Never modify
  anything in planning mode.
- First decide whether the requirements are clear enough to plan.
  * If NOT: ask the user AT MOST 3 sharp clarifying questions and stop.
  * If clear (or once answered): produce the plan.
- Plan format (markdown):
  # Plan: <short title>
  ## Intent          — the outcome in one or two sentences
  ## Units of Work   — for each: '### UoW <n>: <name>' with steps,
                       acceptance criteria, and risks
  ## Out of scope    — what you are deliberately not doing
- End the finished plan with this exact line: {READY_MARKER}
"""


def is_ready(text: str) -> bool:
    return READY_MARKER in (text or "")


def plans_dir() -> Path:
    return Path(".talos") / "plans"


def save_plan(text: str) -> Path:
    d = plans_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(text.replace(READY_MARKER, "").strip() + "\n", encoding="utf-8")
    return path


def construct_prompt(plan: str) -> str:
    """The 🔨 construct-phase kickoff message."""
    return (
        "The plan below is APPROVED. Execute it unit of work by unit of "
        "work, in order. After each unit, verify its acceptance criteria "
        "and state ✅/❌ before moving on. Stop and ask if reality "
        "contradicts the plan.\n\n" + plan
    )
