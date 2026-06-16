"""Tests for the scheduling daemon (M49).

Pattern: every test runs in ``tmp_path`` with ``monkeypatch.chdir`` so
``.talos/schedules/`` is isolated. We never touch the network or the
real ``Runtime`` — fires inject a ``runtime_factory`` that returns a
trivial ``FakeRuntime``, and the daemon loop is driven by a
``frozen_now()`` clock so tests are deterministic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from talos.lifecycle import scheduling as sch


# ── 🎭 fakes ────────────────────────────────────────────────────────────


class FakeRuntime:
    """Drop-in for ``Runtime`` inside ``fire_schedule``: returns a canned
    response and exposes the same attributes ``fire_schedule`` reads."""

    def __init__(self, response: str = "ok", raise_exc: Exception | None = None,
                 session_id: str = "fake-session", model_name: str = "fake-model"):
        self._response = response
        self._raise = raise_exc
        self.messages = []
        self.usage = {"input": 1, "output": 2, "total": 3, "turns": 1}
        self.session_id = session_id
        self.model_name = model_name

    def turn(self, prompt: str) -> str:
        if self._raise:
            raise self._raise
        return self._response


def _make_schedule(prompt="hi", cron="* * * * *", **kw) -> sch.Schedule:
    return sch.Schedule(id=sch.slugify(prompt), prompt=prompt, cron=cron, **kw)


# ── 🗂️ storage ─────────────────────────────────────────────────────────


def test_slugify_and_unique_id():
    assert sch.slugify("Summarize my Inbox!") == "summarize-my-inbox"
    assert sch.slugify("hello", max_words=1) == "hello"
    assert sch.slugify("!!!") == "schedule"
    assert sch.unique_id("a", ["a", "a-2"]) == "a-3"
    assert sch.unique_id("b", ["a"]) == "b"


def test_save_and_load_schedule(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _make_schedule(prompt="summarize", cron="0 9 * * *")
    sch.save_schedule(s)
    assert sch.schedule_file(s.id).is_file()
    loaded = sch.get_schedule(s.id)
    assert loaded is not None
    assert loaded.cron == "0 9 * * *"
    assert sch.list_schedules() == [loaded]


def test_remove_schedule_preserves_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _make_schedule()
    sch.save_schedule(s)
    sch.write_run(s.id, datetime(2026, 6, 15, 9), datetime(2026, 6, 15, 9, 0, 1),
                  status="ok", prompt="hi", response="hello")
    assert sch.remove_schedule(s.id) is True
    assert sch.get_schedule(s.id) is None
    # run files survive
    runs = list(sch.runs_dir(s.id).glob("*.json"))
    assert len(runs) == 1
    assert sch.remove_schedule(s.id) is False  # second remove → False


# ── ⏰ cron arithmetic ─────────────────────────────────────────────────


def test_validate_cron_ok_and_bad():
    assert sch.validate_cron("0 9 * * *") == "0 9 * * *"
    with pytest.raises(ValueError):
        sch.validate_cron("not a cron")


def test_next_fire_basic():
    s = _make_schedule(cron="0 9 * * *")
    base = datetime(2026, 6, 15, 8, 0)
    nxt = sch.next_fire(s, base)
    assert nxt == datetime(2026, 6, 15, 9, 0)


def test_upcoming_fires_returns_three():
    base = datetime(2026, 6, 15, 8, 0)
    fires = sch.upcoming_fires("0 9 * * *", base, 3)
    assert fires == [
        datetime(2026, 6, 15, 9, 0),
        datetime(2026, 6, 16, 9, 0),
        datetime(2026, 6, 17, 9, 0),
    ]


def test_is_due_uses_created_at_floor(tmp_path, monkeypatch):
    """A brand-new schedule shouldn't fire instantly — the floor is
    created_at, so the first fire is the next cron tick *after* creation."""
    monkeypatch.chdir(tmp_path)
    s = sch.Schedule(
        id="x", prompt="hi", cron="0 9 * * *",
        created_at=datetime(2026, 6, 15, 8, 30).isoformat(timespec="seconds"),
    )
    # one minute after creation: not due yet (next fire is at 9:00)
    assert sch.is_due(s, datetime(2026, 6, 15, 8, 31)) is False
    # 9:00 sharp: due
    assert sch.is_due(s, datetime(2026, 6, 15, 9, 0)) is True


def test_floor_for_prefers_last_fire():
    s = sch.Schedule(
        id="x", prompt="hi", cron="* * * * *",
        created_at=datetime(2026, 6, 15, 8).isoformat(timespec="seconds"),
        last_fire=datetime(2026, 6, 15, 10).isoformat(timespec="seconds"),
    )
    assert sch.floor_for(s) == datetime(2026, 6, 15, 10)


# ── 📝 run records ─────────────────────────────────────────────────────


def test_write_run_creates_both_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _make_schedule()
    sch.save_schedule(s)
    md, js = sch.write_run(
        s.id, datetime(2026, 6, 15, 9), datetime(2026, 6, 15, 9, 0, 2),
        status="ok", prompt="hi", response="hello world",
    )
    assert md.is_file() and js.is_file()
    text = md.read_text(encoding="utf-8")
    assert "✅" in text and "hello world" in text and "hi" in text
    runs = sch.list_runs(s.id)
    assert len(runs) == 1
    assert runs[0]["response"] == "hello world"
    assert runs[0]["read"] is False


def test_mark_all_read_flips_only_unread(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _make_schedule()
    sch.save_schedule(s)
    for hour in (9, 10):
        sch.write_run(
            s.id, datetime(2026, 6, 15, hour),
            datetime(2026, 6, 15, hour, 0, 1),
            status="ok", prompt="hi", response="hello",
        )
    assert sch.unread_count() == 2
    assert sch.mark_all_read() == 2
    assert sch.unread_count() == 0
    assert sch.mark_all_read() == 0  # idempotent


# ── 🔥 firing ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fire_schedule_writes_run_and_updates_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _make_schedule(prompt="ping")
    sch.save_schedule(s)

    clock = [datetime(2026, 6, 15, 9, 0, 0)]
    def now_fn():
        v = clock[0]
        clock[0] = clock[0] + timedelta(seconds=1)
        return v

    record = await sch.fire_schedule(
        s, now_fn=now_fn, runtime_factory=lambda _s: FakeRuntime(response="pong"),
    )
    assert record.status == "ok"
    assert record.response == "pong"
    after = sch.get_schedule(s.id)
    assert after.fire_count == 1
    assert after.last_status == "ok"
    runs = sch.list_runs(s.id)
    assert runs and runs[0]["response"] == "pong"


@pytest.mark.asyncio
async def test_fire_captures_exceptions_as_error_record(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _make_schedule()
    sch.save_schedule(s)

    record = await sch.fire_schedule(
        s, runtime_factory=lambda _s: FakeRuntime(raise_exc=RuntimeError("boom")),
    )
    assert record.status == "error"
    assert "boom" in record.error
    after = sch.get_schedule(s.id)
    assert after.last_status == "error"
    assert after.fire_count == 1


@pytest.mark.asyncio
async def test_fire_records_session_for_resume(tmp_path, monkeypatch):
    """When resume=True, the first fire stamps the session_id so all
    subsequent fires reuse it (M50's rolling-session feature)."""
    monkeypatch.chdir(tmp_path)
    s = _make_schedule(resume=True)
    sch.save_schedule(s)
    await sch.fire_schedule(
        s, runtime_factory=lambda _s: FakeRuntime(session_id="sess-123"),
    )
    after = sch.get_schedule(s.id)
    assert after.session_id == "sess-123"


# ── ⏰ daemon loop ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_daemon_fires_due_schedule(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = sch.Schedule(
        id="hourly", prompt="ping", cron="0 * * * *",
        created_at=datetime(2026, 6, 15, 8, 0).isoformat(timespec="seconds"),
    )
    sch.save_schedule(s)

    # Two ticks: first at 9:00 sharp (should fire), second at 9:00:01 (not due again).
    times = iter([
        datetime(2026, 6, 15, 9, 0, 0),     # tick 1 check
        datetime(2026, 6, 15, 9, 0, 0, 100), # fire_schedule start ts
        datetime(2026, 6, 15, 9, 0, 0, 200), # fire_schedule end ts
        datetime(2026, 6, 15, 9, 0, 1),     # tick 2 check
    ])
    def now_fn():
        try:
            return next(times)
        except StopIteration:
            return datetime(2026, 6, 15, 9, 0, 2)

    fires = []
    def factory(sched):
        fires.append(sched.id)
        return FakeRuntime(response="pong")

    await sch.daemon_loop(
        tick_seconds=0,                  # don't actually sleep
        now_fn=now_fn,
        runtime_factory=factory,
        max_ticks=2,
    )
    assert fires == ["hourly"]
    runs = sch.list_runs("hourly")
    assert len(runs) == 1 and runs[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_daemon_skips_overlapping_fire(tmp_path, monkeypatch):
    """If a previous fire is still running, the next due tick should not
    pile up — log 'skipped', advance last_fire, and continue."""
    monkeypatch.chdir(tmp_path)
    s = sch.Schedule(
        id="slow", prompt="ping", cron="* * * * *",
        created_at=datetime(2026, 6, 15, 8, 59).isoformat(timespec="seconds"),
    )
    sch.save_schedule(s)

    # A factory whose runtime blocks until we release it — simulates an
    # in-flight fire from the prior tick.
    release = asyncio.Event()
    started = asyncio.Event()

    class BlockingRuntime(FakeRuntime):
        async def turn(self, prompt):  # noqa: D401
            started.set()
            await release.wait()
            return "ok"

    def factory(_s):
        return BlockingRuntime()

    # Run two ticks back-to-back; the first kicks off the blocking fire,
    # the second sees it in-flight and skips.
    now_iter = iter([
        datetime(2026, 6, 15, 9, 0, 0),
        datetime(2026, 6, 15, 9, 0, 0, 100),  # fire start
        datetime(2026, 6, 15, 9, 1, 0),       # next tick, still in-flight
    ])
    def now_fn():
        try:
            return next(now_iter)
        except StopIteration:
            return datetime(2026, 6, 15, 9, 2, 0)

    logs: list[str] = []
    task = asyncio.create_task(
        sch.daemon_loop(
            tick_seconds=0,
            now_fn=now_fn,
            runtime_factory=factory,
            log=logs.append,
            max_ticks=2,
        )
    )
    # Let both ticks happen
    await started.wait()
    await asyncio.sleep(0.05)
    release.set()
    await task
    # Should have logged at least one "skipping" line
    assert any("skipping" in l for l in logs), logs
    # Only one fire actually completed
    runs = sch.list_runs("slow")
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_daemon_picks_up_schedules_added_after_start(tmp_path, monkeypatch):
    """The daemon re-reads .talos/schedules/ every tick, so an `add` from
    another shell is honored without restart."""
    monkeypatch.chdir(tmp_path)
    # First tick: nothing to fire (no schedules on disk).
    # Between tick 1 and tick 2 we add a schedule via the public API.
    times = iter([
        datetime(2026, 6, 15, 9, 0, 0),   # tick 1 → no schedules
        datetime(2026, 6, 15, 9, 1, 0),   # tick 2 → new schedule is due
        datetime(2026, 6, 15, 9, 1, 0, 100),
        datetime(2026, 6, 15, 9, 1, 0, 200),
    ])
    def now_fn():
        try:
            return next(times)
        except StopIteration:
            return datetime(2026, 6, 15, 9, 2, 0)

    fires: list[str] = []
    def factory(sched):
        fires.append(sched.id)
        return FakeRuntime()

    async def add_schedule_after_first_tick():
        await asyncio.sleep(0.01)
        s = sch.Schedule(
            id="late", prompt="ping", cron="* * * * *",
            created_at=datetime(2026, 6, 15, 8, 59).isoformat(timespec="seconds"),
        )
        sch.save_schedule(s)

    await asyncio.gather(
        sch.daemon_loop(
            tick_seconds=0.02,
            now_fn=now_fn,
            runtime_factory=factory,
            max_ticks=2,
        ),
        add_schedule_after_first_tick(),
    )
    assert "late" in fires


# ── 🗣️ NL→cron (M50) ──────────────────────────────────────────────────


import pytest as _pytest  # already imported above; alias to keep the section self-contained


@_pytest.mark.asyncio
async def test_parse_nl_to_cron_basic():
    """A clean LLM response is accepted and validated."""
    async def fake_llm(system, user):
        # Verify the prompt actually carries the NL phrase.
        assert "every morning at 9" in user
        return "0 9 * * *"

    cron = await sch.parse_nl_to_cron("every morning at 9", fake_llm)
    assert cron == "0 9 * * *"


@_pytest.mark.asyncio
async def test_parse_nl_to_cron_strips_backticks_and_prose():
    """LLMs love wrapping cron in `…` or prefixing prose. We grab the
    first non-empty line and strip ornaments."""
    async def fake_llm(_sys, _user):
        return "`0 9 * * *`"
    assert await sch.parse_nl_to_cron("morning", fake_llm) == "0 9 * * *"

    async def multiline(_sys, _user):
        return "\n\n0 9 * * *\nSure!\n"
    assert await sch.parse_nl_to_cron("morning", multiline) == "0 9 * * *"


@_pytest.mark.asyncio
async def test_parse_nl_to_cron_rejects_garbage():
    async def fake_llm(_sys, _user):
        return "I don't know"
    with _pytest.raises(ValueError):
        await sch.parse_nl_to_cron("morning", fake_llm)

    async def empty(_sys, _user):
        return "   "
    with _pytest.raises(ValueError):
        await sch.parse_nl_to_cron("morning", empty)


# ── 🎟️ rolling session (M50) ──────────────────────────────────────────


@_pytest.mark.asyncio
async def test_rolling_session_is_stamped_then_reused(tmp_path, monkeypatch):
    """A schedule with resume=True should stamp session_id on the first
    fire, and the factory should see that same session_id on the next
    fire (so the rolling session actually rolls)."""
    monkeypatch.chdir(tmp_path)
    s = _make_schedule(prompt="ping", resume=True)
    sch.save_schedule(s)

    seen_resume_ids: list = []

    def factory(sched):
        # Capture the session_id passed to the runtime (what Runtime would
        # see as `resume=...`). On fire #1 it's None; on fire #2 it should
        # be the value we stamped during fire #1.
        seen_resume_ids.append(sched.session_id)
        # Echo a stable session_id back to be saved on fire #1.
        return FakeRuntime(session_id="rolling-sess")

    await sch.fire_schedule(s, runtime_factory=factory)
    after_first = sch.get_schedule(s.id)
    assert after_first.session_id == "rolling-sess"

    # Fire again with the reloaded schedule.
    await sch.fire_schedule(after_first, runtime_factory=factory)

    assert seen_resume_ids == [None, "rolling-sess"]


@_pytest.mark.asyncio
async def test_fresh_session_when_resume_false(tmp_path, monkeypatch):
    """resume=False is the default; session_id should stay None across
    fires so each call uses a brand-new session."""
    monkeypatch.chdir(tmp_path)
    s = _make_schedule(prompt="ping")  # resume defaults to False
    sch.save_schedule(s)

    def factory(_sched):
        return FakeRuntime(session_id="ephemeral")

    await sch.fire_schedule(s, runtime_factory=factory)
    after = sch.get_schedule(s.id)
    assert after.session_id is None  # not stamped because resume=False
