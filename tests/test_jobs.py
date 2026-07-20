"""📟 Background jobs: inline fast path, the callback door, kill, reaping."""

import asyncio
import importlib
import sys

import pytest

import talos.tools.jobs as jobs_mod
from talos.tools.jobs import JobManager, job_kill, job_status

# `talos.tools` re-exports the *tool* under the name `shell`, which shadows
# the submodule on `import talos.tools.shell as …` — go via importlib.
shell_mod = importlib.import_module("talos.tools.shell")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """A fresh manager whose 'shell' is the python interpreter (portable)."""
    monkeypatch.chdir(tmp_path)  # .talos/jobs lands in tmp
    m = JobManager()
    monkeypatch.setattr(jobs_mod, "manager", m)
    monkeypatch.setattr(shell_mod, "manager", m)
    monkeypatch.setattr(
        jobs_mod, "shell_command", lambda c: [sys.executable, "-c", c]
    )
    return m


async def test_fast_command_returns_inline(manager):
    out = await shell_mod.shell.ainvoke({"command": "print('hi there')"})
    assert "exit code: 0" in out and "hi there" in out and "job #1" in out


async def test_slow_command_backgrounds_then_knocks_on_the_door(
    manager, monkeypatch
):
    monkeypatch.setattr(shell_mod, "FOREGROUND_GRACE", 0.2)
    events: asyncio.Queue = asyncio.Queue()
    manager.set_notifier(events.put_nowait)
    out = await shell_mod.shell.ainvoke(
        {"command": "import time; time.sleep(1); print('done!')"}
    )
    assert "background" in out and "job #1" in out  # returned immediately
    job = await asyncio.wait_for(events.get(), timeout=15)  # 🚪 the door
    assert job.status == "done" and job.exit_code == 0
    assert "done!" in job.read_log()


async def test_job_status_reports_and_kill_reaps_the_tree(manager, monkeypatch):
    monkeypatch.setattr(shell_mod, "FOREGROUND_GRACE", 0.2)
    await shell_mod.shell.ainvoke({"command": "import time; time.sleep(60)"})
    assert "running" in job_status.invoke({"job_id": 1})
    msg = job_kill.invoke({"job_id": 1})
    assert "killed" in msg
    await asyncio.wait_for(manager.jobs[1].proc.wait(), timeout=15)
    assert manager.jobs[1].status == "killed"
    assert manager.running() == []


async def test_shutdown_leaves_no_orphans(manager, monkeypatch):
    monkeypatch.setattr(shell_mod, "FOREGROUND_GRACE", 0.2)
    await shell_mod.shell.ainvoke({"command": "import time; time.sleep(60)"})
    await shell_mod.shell.ainvoke({"command": "import time; time.sleep(60)"})
    assert len(manager.running()) == 2
    victims = await manager.shutdown()
    assert len(victims) == 2
    assert manager.running() == []
    for j in victims:
        assert j.proc.returncode is not None  # actually dead, not orphaned
    # registry is empty again — nothing for the next session to warn about
    assert manager.leftovers() == []


async def test_registry_breadcrumb_survives_a_crash(manager):
    await manager.start("import time; time.sleep(60)")
    fresh = JobManager()  # simulates the next session after a crash
    left = fresh.leftovers()
    assert len(left) == 1 and left[0]["pid"] == manager.jobs[1].pid
    manager.kill(1)  # tidy up


async def test_unknown_job_ids_answer_politely(manager):
    assert "no jobs started" in job_status.invoke({"job_id": 0})
    await shell_mod.shell.ainvoke({"command": "print('x')"})
    assert "no job #99" in job_status.invoke({"job_id": 99})
    assert "no job #99" in job_kill.invoke({"job_id": 99})
