"""🧪 Skill synthesis — Talos writes (and verifies) its own skills.

The Voyager / EvoSkills pattern (2026): after the agent completes a
non-trivial multi-step task, distill the trajectory into a reusable
SKILL.md so next time it's one lazy-loaded lookup instead of rediscovery.

The 2026 lesson that separates this from naive auto-skilling: **verify
before saving**. An unverified synthesized skill is as likely to mislead
as help. So a candidate skill must pass a check — its frontmatter parses,
it has real procedural content, and (when it embeds shell snippets) a
self-review LLM pass judges it sound — before it joins .talos/skills.
"""

import re
from pathlib import Path

SYNTH_PROMPT = """You just completed a task. Write a reusable SKILL.md that
would let you (or another agent) do this class of task faster next time.

Format EXACTLY:
---
name: <short-kebab-case>
description: <one line: when to use this skill>
---
<concise step-by-step procedure: the durable how-to, not this specific
run's details. Include commands/snippets where they generalize.>

Only write a skill if the task taught a GENERALIZABLE procedure. If it was
trivial or one-off, reply with exactly: NO SKILL"""

REVIEW_PROMPT = """Review this candidate SKILL.md. Is it a sound, safe,
generalizable procedure (not hallucinated, no destructive commands, not
overfit to one run)? Reply STRICT JSON: {"ok": true/false, "reason": "..."}"""


def _parse_candidate(text: str) -> tuple[str, str, str] | None:
    """Return (name, description, body) or None if it isn't a valid skill."""
    if "NO SKILL" in text.upper():
        return None
    from talos.lifecycle.skills import _parse_frontmatter

    meta, body = _parse_frontmatter(text.strip())
    name = meta.get("name", "").strip()
    desc = meta.get("description", "").strip()
    if not name or not desc or len(body.strip()) < 40:
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        return None
    return name, desc, body


def parse_review(raw: str) -> dict:
    import json

    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e <= s:
        return {"ok": False, "reason": "unparseable review"}
    try:
        return json.loads(raw[s : e + 1])
    except json.JSONDecodeError:
        return {"ok": False, "reason": "unparseable review"}


async def synthesize(transcript: str, propose, review) -> dict:
    """Produce + verify a skill from a finished task.

    ``propose`` : async (prompt, transcript) -> candidate SKILL.md text
    ``review``  : async (prompt, candidate)  -> JSON verdict
    Returns {saved: bool, name|reason}.
    """
    candidate = await propose(SYNTH_PROMPT, transcript)
    parsed = _parse_candidate(candidate)
    if parsed is None:
        return {"saved": False, "reason": "no generalizable skill in this task"}
    name, desc, _body = parsed

    verdict = parse_review(await review(REVIEW_PROMPT, candidate))
    if not verdict.get("ok"):
        return {"saved": False, "reason": f"failed review: {verdict.get('reason')}"}

    from talos.lifecycle.skills import skills_dir

    dest = skills_dir() / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(candidate.strip() + "\n", encoding="utf-8")
    return {"saved": True, "name": name, "path": str(dest / "SKILL.md")}
