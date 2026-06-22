"""📅 Scheduling — fire Talos prompts on a cron schedule.

Why this exists: a lot of useful agent work is *recurring* — "every
morning, summarize what landed in my inbox", "every Monday at 9, write
the standup from last week's commits". `talos chat`/`talos run` cover
the on-demand case; scheduling closes the loop on unattended work.

Design (M49 — cron only; NL→cron lands in M50, surfacing in M51):

* **Storage.** Each schedule is one JSON file under
  ``.talos/schedules/<id>/schedule.json`` (mirrors the per-thing
  directory pattern used by sessions/skills/agents/checkpoints). The
  same folder also holds ``runs/<ts>.{md,json}`` — one record per fire.
* **Tick.** ``next_fire(sched, after)`` is a thin wrapper around
  ``croniter`` (the standard cron-parsing library). ``due_schedules(now)``
  returns the schedules whose next fire has already passed. We treat
  ``last_fire or created_at`` as the "after" floor, so a brand-new
  schedule never fires the instant it's added.
* **Daemon.** ``daemon_loop()`` is the in-process scheduler
  (``talos schedule run``): wake every ``tick_seconds``, find due
  schedules, fire each in a background task, sleep again. Cron-style
  semantics: at most one in-flight fire per schedule — if a previous
  fire is still running we skip the new tick and log it.
* **Fire.** ``fire_schedule()`` builds a non-interactive ``Runtime``
  (the same one ``talos run`` uses), calls ``.turn(prompt)``, and
  serializes the resulting messages plus a markdown digest to
  ``runs/<ts>.{md,json}``. ``yolo=True`` is required for any schedule
  that uses mutating tools — no human is around to approve.

croniter is an optional extra (``pip install -e ".[schedule]"``);
``import croniter`` is deferred so the import-time cost of this module
stays zero when the feature isn't installed.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from pydantic import BaseModel, Field


# ── 🗂️ storage layout ───────────────────────────────────────────────────


def schedules_dir() -> Path:
    """``.talos/schedules/`` — one subdir per schedule, just like sessions."""
    return Path(".talos") / "schedules"


def schedule_file(schedule_id: str) -> Path:
    return schedules_dir() / schedule_id / "schedule.json"


def runs_dir(schedule_id: str) -> Path:
    return schedules_dir() / schedule_id / "runs"


# ── 📋 the schedule itself ─────────────────────────────────────────────


class Schedule(BaseModel):
    """One scheduled task. Pydantic v2 — validated on construction so a
    malformed schedule.json fails loudly at load instead of at fire time.

    M65: ``action_kind`` extends the schedule beyond just "run a prompt".
    ``prompt`` (default) → runs ``Runtime.turn(prompt)`` as before.
    ``kb_update`` → re-ingests the KB named in ``kb_id`` (no LLM call).
    """

    id: str
    prompt: str = ""  # the prompt for action_kind='prompt'; ignored otherwise
    cron: str
    tz: str | None = None  # IANA zone name, e.g. "America/New_York" (M50+)
    model: str | None = None
    yolo: bool = False
    resume: bool = False  # 🎟️ rolling session per schedule (lands in M50)
    session_id: str | None = None  # filled when resume=True; first fire stamps it
    action_kind: str = "prompt"  # "prompt" | "kb_update"
    kb_id: str | None = None  # for action_kind='kb_update' — name or id
    created_at: str = Field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    last_fire: str | None = None  # ISO ts of the last fire we attempted
    last_status: str | None = None  # "ok" | "error" | "skipped"
    last_error: str | None = None
    fire_count: int = 0


# ── ✏️ CRUD ──────────────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_words: int = 4) -> str:
    """Turn a prompt into a short kebab-case id. ``"summarize my inbox"``
    → ``"summarize-my-inbox"``. Deterministic, no random tail — the caller
    handles uniqueness."""
    words = _SLUG_RE.sub("-", text.lower()).strip("-").split("-")
    return "-".join(w for w in words[:max_words] if w) or "schedule"


def unique_id(base: str, existing: Iterable[str]) -> str:
    """If ``base`` is taken, append ``-2``, ``-3``, … until it isn't."""
    taken = set(existing)
    if base not in taken:
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if candidate not in taken:
            return candidate
    raise RuntimeError(f"too many schedules named {base!r}")


def list_schedules() -> list[Schedule]:
    """Every schedule found under ``.talos/schedules/``, sorted by id."""
    base = schedules_dir()
    if not base.is_dir():
        return []
    found: list[Schedule] = []
    for f in sorted(base.glob("*/schedule.json")):
        try:
            found.append(Schedule.model_validate_json(f.read_text(encoding="utf-8")))
        except Exception:
            # A broken file shouldn't take the daemon down; skip it.
            continue
    return found


def get_schedule(schedule_id: str) -> Schedule | None:
    f = schedule_file(schedule_id)
    if not f.is_file():
        return None
    try:
        return Schedule.model_validate_json(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_schedule(sched: Schedule) -> Path:
    f = schedule_file(sched.id)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(sched.model_dump_json(indent=2), encoding="utf-8")
    return f


def remove_schedule(schedule_id: str) -> bool:
    """Delete the schedule's schedule.json. Run history is preserved on
    disk so an accidental ``remove`` doesn't wipe historical context;
    re-creating with the same id picks the old runs back up."""
    f = schedule_file(schedule_id)
    if not f.is_file():
        return False
    f.unlink()
    return True


# ── ⏰ cron arithmetic ──────────────────────────────────────────────────


def validate_cron(expr: str) -> str:
    """Raise ``ValueError`` if ``expr`` isn't a valid cron. Returns the
    expression unchanged on success — handy for inline validation."""
    try:
        from croniter import croniter  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "📅 scheduling requires croniter — install with: "
            'pip install -e ".[schedule]"'
        ) from exc
    if not croniter.is_valid(expr):
        raise ValueError(f"invalid cron expression: {expr!r}")
    return expr


def next_fire(sched: Schedule, after: datetime) -> datetime:
    """The next fire moment strictly after ``after`` for this schedule."""
    from croniter import croniter  # validated by validate_cron at create time

    return croniter(sched.cron, after).get_next(datetime)


def upcoming_fires(cron: str, after: datetime, count: int = 3) -> list[datetime]:
    """Preview the next ``count`` fire times — used by the CLI to show
    "looks right?" confirmation when a schedule is created."""
    from croniter import croniter

    it = croniter(cron, after)
    return [it.get_next(datetime) for _ in range(count)]


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def floor_for(sched: Schedule) -> datetime:
    """The "after" anchor for ``next_fire``: last_fire if it ever fired,
    else created_at. Ensures a brand-new schedule never fires the second
    it's added, and the daemon picks up exactly one missed fire if it was
    offline (not a whole backlog)."""
    return (
        _parse_iso(sched.last_fire)
        or _parse_iso(sched.created_at)
        or datetime.now()
    )


def is_due(sched: Schedule, now: datetime) -> bool:
    return next_fire(sched, floor_for(sched)) <= now


def due_schedules(now: datetime) -> list[Schedule]:
    return [s for s in list_schedules() if is_due(s, now)]


# ── 📝 run records ──────────────────────────────────────────────────────


@dataclass
class RunRecord:
    """One scheduled fire's persisted artifact, JSON side."""

    schedule_id: str
    started_at: str
    finished_at: str
    status: str  # "ok" | "error" | "skipped"
    prompt: str
    response: str  # the final assistant text (or the error message)
    error: str | None = None
    duration_s: float | None = None
    usage: dict | None = None
    model: str | None = None
    session_id: str | None = None
    read: bool = False  # surfaced in /runs?

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, ensure_ascii=False)


def _run_basename(started_at: datetime) -> str:
    # filesystem-safe, sorts chronologically
    return started_at.strftime("%Y-%m-%dT%H-%M-%S")


def _markdown_for(record: RunRecord, messages: list) -> str:
    """Human-friendly transcript for a run. Kept simple — full structured
    detail lives in the .json sidecar."""
    from langchain_core.messages import AIMessage, ToolMessage

    def text(msg) -> str:
        c = getattr(msg, "content", "")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(b.get("text", "") for b in c if isinstance(b, dict))
        return str(c)

    icon = {"ok": "✅", "error": "💥", "skipped": "⏭️"}.get(record.status, "•")
    lines = [
        f"# {icon} {record.schedule_id} · {record.started_at}",
        "",
        f"**Status:** {record.status}",
    ]
    if record.duration_s is not None:
        lines.append(f"**Duration:** {record.duration_s:.1f}s")
    if record.model:
        lines.append(f"**Model:** {record.model}")
    lines += [
        "",
        "## 📝 Prompt",
        "",
        record.prompt,
        "",
        "## 💬 Response",
        "",
        record.response or "(no response)",
    ]
    if record.error:
        lines += ["", "## 💥 Error", "", "```", record.error, "```"]
    tool_lines = []
    for m in messages or []:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for call in m.tool_calls:
                tool_lines.append(f"- 🔧 {call['name']}")
        elif isinstance(m, ToolMessage):
            first = text(m).strip().splitlines()
            tool_lines.append(f"  ↳ {(first[0] if first else '')[:120]}")
    if tool_lines:
        lines += ["", "## 🔧 Tool activity", ""] + tool_lines
    return "\n".join(lines) + "\n"


def write_run(
    schedule_id: str,
    started_at: datetime,
    finished_at: datetime,
    *,
    status: str,
    prompt: str,
    response: str,
    messages: list | None = None,
    error: str | None = None,
    usage: dict | None = None,
    model: str | None = None,
    session_id: str | None = None,
) -> tuple[Path, Path]:
    """Persist one fire as ``{base}.md`` + ``{base}.json`` under
    ``.talos/schedules/<id>/runs/``. Returns ``(md_path, json_path)``."""
    record = RunRecord(
        schedule_id=schedule_id,
        started_at=started_at.isoformat(timespec="seconds"),
        finished_at=finished_at.isoformat(timespec="seconds"),
        status=status,
        prompt=prompt,
        response=response,
        error=error,
        duration_s=(finished_at - started_at).total_seconds(),
        usage=usage,
        model=model,
        session_id=session_id,
    )
    d = runs_dir(schedule_id)
    d.mkdir(parents=True, exist_ok=True)
    base = _run_basename(started_at)
    md_path = d / f"{base}.md"
    json_path = d / f"{base}.json"
    md_path.write_text(_markdown_for(record, messages or []), encoding="utf-8")
    json_path.write_text(record.to_json(), encoding="utf-8")
    return md_path, json_path


def list_runs(schedule_id: str, limit: int | None = None) -> list[dict]:
    """Newest-first list of every run on disk for a schedule."""
    d = runs_dir(schedule_id)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
        if limit and len(out) >= limit:
            break
    return out


def all_runs(limit_per_schedule: int | None = None) -> list[dict]:
    """All runs across every schedule (for the /runs command). Sorted
    newest-first by started_at."""
    out: list[dict] = []
    for s in list_schedules():
        out.extend(list_runs(s.id, limit_per_schedule))
    out.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return out


def unread_count() -> int:
    """How many run records are still flagged ``read=False`` — drives the
    📬 banner line on REPL startup (M51)."""
    return sum(1 for r in all_runs() if not r.get("read"))


def mark_all_read() -> int:
    """Flip every run record to ``read=True``. Returns the count flipped."""
    n = 0
    for s in list_schedules():
        d = runs_dir(s.id)
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if data.get("read"):
                continue
            data["read"] = True
            f.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            n += 1
    return n


# ── 🔥 firing a schedule ────────────────────────────────────────────────


# Default runtime factory; tests inject a fake by overriding this name.
def _default_runtime_factory(sched: Schedule):
    """Build the Runtime that will execute one fire. Wrapped in a function
    so tests can monkeypatch this name with a fake that doesn't pull in
    the LLM/MCP stack."""
    from talos.agent.runtime import Runtime

    return Runtime(
        model=sched.model,
        yolo=sched.yolo,
        interactive=False,
        resume=sched.session_id if sched.resume and sched.session_id else None,
    )


async def fire_schedule(
    sched: Schedule,
    *,
    now_fn: Callable[[], datetime] = datetime.now,
    runtime_factory: Callable[[Schedule], object] | None = None,
    log: Callable[[str], None] | None = None,
) -> RunRecord:
    """Run one scheduled fire end-to-end and persist the result.

    Returns the ``RunRecord`` so the daemon can log a one-line summary.
    Never raises — any exception becomes a status=error run record so the
    daemon loop can keep going.
    """
    factory = runtime_factory or _default_runtime_factory
    started = now_fn()
    log = log or (lambda _msg: None)
    log(f"🔥 firing {sched.id} ({sched.cron})")

    response = ""
    error: str | None = None
    status = "ok"
    usage = None
    messages: list = []
    session_id = sched.session_id
    model = sched.model

    try:
        if sched.action_kind == "kb_update":
            from talos.lifecycle.knowledge_cli import update_kb

            if not sched.kb_id:
                raise ValueError("kb_update schedule has no kb_id set")
            res = update_kb(kb_id_or_name=sched.kb_id)
            if "error" in res:
                raise RuntimeError(res["error"])
            response = (
                f"♻️ {sched.kb_id}: re-indexed "
                f"{res['sources']} source(s), {res['chunks']} chunk(s)"
            )
        else:
            runtime = factory(sched)
            result = runtime.turn(sched.prompt)
            if asyncio.iscoroutine(result):
                response = await result
            else:
                response = result
            messages = list(getattr(runtime, "messages", []) or [])
            usage = dict(getattr(runtime, "usage", {}) or {})
            session_id = getattr(runtime, "session_id", session_id)
            model = getattr(runtime, "model_name", model)
    except Exception as exc:  # noqa: BLE001 — daemon must not die on a bad fire
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        response = error
        log(f"💥 {sched.id}: {error}")

    finished = now_fn()
    write_run(
        sched.id,
        started,
        finished,
        status=status,
        prompt=sched.prompt,
        response=response,
        messages=messages,
        error=error,
        usage=usage,
        model=model,
        session_id=session_id,
    )

    sched.last_fire = started.isoformat(timespec="seconds")
    sched.last_status = status
    sched.last_error = error
    sched.fire_count += 1
    if sched.resume and not sched.session_id and session_id:
        sched.session_id = session_id
    save_schedule(sched)
    log(
        f"{'✅' if status == 'ok' else '💥'} {sched.id} → {status} "
        f"({(finished - started).total_seconds():.1f}s)"
    )

    return RunRecord(
        schedule_id=sched.id,
        started_at=started.isoformat(timespec="seconds"),
        finished_at=finished.isoformat(timespec="seconds"),
        status=status,
        prompt=sched.prompt,
        response=response,
        error=error,
        duration_s=(finished - started).total_seconds(),
        usage=usage,
        model=model,
        session_id=session_id,
    )


# ── ⏰ the daemon ───────────────────────────────────────────────────────


async def daemon_loop(
    *,
    tick_seconds: int = 30,
    stop: asyncio.Event | None = None,
    now_fn: Callable[[], datetime] = datetime.now,
    runtime_factory: Callable[[Schedule], object] | None = None,
    log: Callable[[str], None] | None = None,
    max_ticks: int | None = None,
) -> int:
    """The ``talos schedule run`` loop.

    Every ``tick_seconds`` we check every schedule on disk. Schedules
    are re-read from disk on each tick so ``talos schedule add`` from
    another shell is picked up without restarting the daemon.

    Overlap policy: **skip**. If a schedule is still firing from a
    previous tick, the new fire is skipped (logged as such) — the next
    tick will try again. Matches how cron behaves.

    Returns the number of ticks executed (useful in tests).
    """
    stop = stop or asyncio.Event()
    log = log or (lambda _msg: None)
    in_flight: dict[str, asyncio.Task] = {}
    ticks = 0

    log(f"📅 talos schedule daemon — tick={tick_seconds}s")
    while not stop.is_set():
        now = now_fn()
        for sid, task in list(in_flight.items()):
            if task.done():
                del in_flight[sid]

        for sched in list_schedules():
            try:
                if not is_due(sched, now):
                    continue
            except Exception as exc:  # noqa: BLE001 — bad cron etc.
                log(f"⚠️ {sched.id}: {exc}")
                continue
            if sched.id in in_flight:
                log(f"⏭️ {sched.id}: previous fire still running, skipping")
                sched.last_fire = now.isoformat(timespec="seconds")
                sched.last_status = "skipped"
                save_schedule(sched)
                continue

            async def _runner(s=sched):
                await fire_schedule(
                    s, now_fn=now_fn, runtime_factory=runtime_factory, log=log
                )

            in_flight[sched.id] = asyncio.create_task(_runner())

        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break

        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass

    if in_flight:
        log(f"⏳ waiting for {len(in_flight)} in-flight fire(s) to finish…")
        await asyncio.gather(*in_flight.values(), return_exceptions=True)
    log("📅 daemon stopped")
    return ticks


# ── 🗣️ natural-language → cron (M50) ───────────────────────────────────


NL_TO_CRON_PROMPT = (
    "You convert natural-language schedule descriptions into a 5-field cron "
    "expression (minute hour day-of-month month day-of-week).\n\n"
    "Reply with EXACTLY the cron expression and nothing else — no prose, no "
    "code fences, no explanation. If the description is ambiguous, pick the "
    "most likely interpretation. Day-of-week: 0=Sun, 1=Mon, …, 6=Sat.\n\n"
    "Examples:\n"
    '  "every morning at 9" → 0 9 * * *\n'
    '  "every weekday at 9am" → 0 9 * * 1-5\n'
    '  "every Monday at 6pm" → 0 18 * * 1\n'
    '  "every hour" → 0 * * * *\n'
    '  "every 15 minutes" → */15 * * * *\n'
    '  "the first of every month at noon" → 0 12 1 * *\n'
    '  "every Sunday at midnight" → 0 0 * * 0\n'
)


def _strip_to_cron(text: str) -> str:
    """LLMs love to wrap things in backticks or add 'sure!' prefixes.
    Grab the first non-empty line and strip the usual ornaments."""
    for raw in text.strip().splitlines():
        line = raw.strip().strip("`").strip()
        if line:
            return line
    return ""


async def parse_nl_to_cron(nl: str, llm_call) -> str:
    """Translate ``nl`` to a 5-field cron via the model.

    ``llm_call`` is an async callable ``(system_prompt, user) -> str`` —
    injected so tests can supply a fake without touching the real
    ChatOpenAI wiring. Raises ``ValueError`` if the model returns garbage.
    """
    raw = await llm_call(NL_TO_CRON_PROMPT, nl)
    cron = _strip_to_cron(raw)
    if not cron:
        raise ValueError(f"empty cron from model for input: {nl!r}")
    return validate_cron(cron)
