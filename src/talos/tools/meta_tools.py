"""🛠 Meta-tools — agent-facing access to every non-safety CLI verb.

The "agent can drive Talos in natural language" feature (M66). Each
tool wraps an existing CLI command's underlying logic and returns
JSON-shaped output so the agent can parse and reason about it.

Safety scope: this module ONLY contains things the user already
approved as "the agent can do this." The vault's add/remove/reveal,
self-edit apply, checkpoint restore, and any settings/policy mutations
deliberately stay user-only — they live in their CLI sub-typers and
have no agent tool equivalent.

The convention for tool outputs is JSON whenever the data is
structured; plain strings for "do this and tell me the result." This
makes downstream agent reasoning (chained tool calls, filters) easy.
"""

import json

from langchain_core.tools import tool


# ── 📅 schedules ──────────────────────────────────────────────────────


@tool
def list_schedules_tool() -> str:
    """List all scheduled tasks. Returns JSON [{id, cron, prompt,
    action_kind, kb_id, last_fire, fire_count, ...}]."""
    from talos.lifecycle.scheduling import list_schedules

    try:
        scheds = list_schedules()
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    return json.dumps([s.model_dump() for s in scheds], indent=2, default=str)


@tool
def schedule_show_tool(schedule_id: str) -> str:
    """Show one scheduled task's full definition + recent runs (up to 5)."""
    from talos.lifecycle.scheduling import get_schedule, list_runs

    sched = get_schedule(schedule_id)
    if sched is None:
        return f"Error: no schedule named {schedule_id!r}"
    return json.dumps({
        "schedule": sched.model_dump(),
        "recent_runs": list_runs(schedule_id, limit=5),
    }, indent=2, default=str)


@tool
def schedule_add_tool(prompt: str, cron: str, name: str = "",
                      model: str = "", yolo: bool = False,
                      resume: bool = False) -> str:
    """Add a scheduled task that runs ``prompt`` on the ``cron`` schedule.

    cron: 5-field cron expression (e.g. "0 9 * * *" for 9am daily)
    name: optional id; defaults to a slug of the prompt
    model: optional model override for this schedule
    yolo: required if the prompt uses mutating tools (no human to approve)
    resume: use one rolling session that grows across fires

    For NL like "every morning at 9", convert to cron with knowledge
    of the documentation patterns yourself, or use the CLI which has
    LLM-backed NL parsing. Returns the resulting schedule as JSON.
    """
    from talos.lifecycle.scheduling import (
        Schedule, list_schedules, save_schedule, slugify, unique_id,
        validate_cron,
    )

    try:
        cron = validate_cron(cron)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    sid = unique_id(name or slugify(prompt), (s.id for s in list_schedules()))
    sched = Schedule(
        id=sid, prompt=prompt, cron=cron, model=model or None,
        yolo=yolo, resume=resume,
    )
    save_schedule(sched)
    return json.dumps(sched.model_dump(), indent=2, default=str)


@tool
def schedule_remove_tool(schedule_id: str) -> str:
    """Delete a schedule by id. Run history on disk is preserved."""
    from talos.lifecycle.scheduling import remove_schedule

    return "removed" if remove_schedule(schedule_id) else f"no schedule named {schedule_id!r}"


# ── 📬 scheduled-task run history ────────────────────────────────────


@tool
def list_runs_tool(schedule_id: str = "", limit: int = 25) -> str:
    """List recent scheduled-task runs. If schedule_id is empty,
    returns runs across every schedule (newest first).

    Returns JSON [{schedule_id, started_at, status, prompt, response,
    duration_s, ...}]."""
    from talos.lifecycle.scheduling import all_runs, list_runs

    try:
        if schedule_id:
            runs = list_runs(schedule_id, limit=limit)
        else:
            runs = all_runs(limit_per_schedule=10)[:limit]
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    return json.dumps(runs, indent=2, default=str)


# ── 📇 models ────────────────────────────────────────────────────────


@tool
def list_models_tool() -> str:
    """List the models the configured provider exposes (via /v1/models).
    Returns JSON [{id, context, input_per_m, output_per_m, vision}].

    Use this when the user asks 'what models do I have' or 'what's the
    cheapest model with vision'."""
    from talos.integrations.models import list_models

    try:
        found = sorted(list_models(), key=lambda m: m.id)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    return json.dumps([
        {
            "id": m.id,
            "context": m.context,
            "input_per_m": m.input_per_m,
            "output_per_m": m.output_per_m,
            "vision": m.vision,
        }
        for m in found
    ], indent=2, default=str)


# ── ⏪ checkpoints ───────────────────────────────────────────────────


@tool
def list_checkpoints_tool() -> str:
    """List time-travel checkpoints. Read-only — restore stays in the
    `/rewind` slash command because it's a destructive action that
    deserves an explicit human step.

    Returns JSON [{id, turn, label, has_tree}]."""
    from talos.memory.checkpoints import list_checkpoints

    try:
        cks = list_checkpoints()
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    return json.dumps([
        {"id": c.id, "turn": c.turn, "label": c.label, "has_tree": bool(c.tree)}
        for c in cks
    ], indent=2)


# ── 🎒 skills ────────────────────────────────────────────────────────


@tool
def create_skill_tool(name: str, description: str, body: str) -> str:
    """Save a new lazy-loaded skill at .talos/skills/<name>/SKILL.md.

    The name + description go into every future system prompt (cheap);
    the body is fetched on demand via load_skill when the agent decides
    it's relevant. Use this when a user-explicit "remember how to do X"
    request justifies a permanent skill (vs save_memory for one-off
    facts).
    """
    from pathlib import Path

    from talos.lifecycle.skills import skills_dir

    if not name or not description or not body:
        return "Error: name, description, and body are all required"
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_").lower()
    if not safe_name:
        return f"Error: invalid skill name {name!r}"
    target = skills_dir() / safe_name / "SKILL.md"
    if target.is_file():
        return f"Error: skill {safe_name!r} already exists at {target}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\nname: {safe_name}\ndescription: {description.strip()}\n---\n"
        f"{body.strip()}\n",
        encoding="utf-8",
    )
    return f"saved skill {safe_name!r} → {target}"


# ── 🔐 vault (read-only listing) ─────────────────────────────────────


@tool
def list_vault_handles_tool() -> str:
    """List vault handles by name + kind + description + scope.
    NEVER returns secret values — that opacity is structural.

    Returns JSON [{handle, kind, description, scope, created_at}].
    Use this when you need to recall WHICH secrets/values you have
    access to (the system prompt's Vault section also shows this
    every turn — this tool is for explicit lookup)."""
    from talos.infra.vault import all_handles

    try:
        handles = all_handles()
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    return json.dumps([
        {
            "handle": h.handle,
            "kind": h.kind,
            "description": h.description,
            "scope": h.scope,
            "created_at": h.created_at,
        }
        for h in handles
    ], indent=2)


# ── 🔗 linked agents ─────────────────────────────────────────────────


@tool
def list_links_tool() -> str:
    """List agent directories linked via `talos link` (kiro/cursor/etc).
    Returns JSON [{path, exists}]."""
    from pathlib import Path

    from talos.integrations.linking import load_links

    try:
        paths = load_links()
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    return json.dumps([
        {"path": str(p), "exists": Path(p).expanduser().is_dir()}
        for p in paths
    ], indent=2)


# ── 🔌 MCP servers (read-only listing) ────────────────────────────────


@tool
def list_mcp_servers_tool() -> str:
    """List configured MCP servers from .talos/mcp.json.
    Returns JSON {name: spec}. Server CONNECTIONS aren't established
    by this tool — use the configured MCP tools directly if available."""
    from talos.integrations.mcp import load_mcp_config

    try:
        servers = load_mcp_config()
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"
    return json.dumps(servers, indent=2, default=str)
