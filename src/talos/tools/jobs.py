"""📟 Background jobs — every shell command is a tracked process, never an orphan.

Instead of blocking the agent loop while a command runs, every command
becomes a *job*:

- spawned in its own process group, stdout+stderr streamed to
  ``.talos/jobs/job-N.log``
- a watcher task awaits the exit and, if the job outlived the foreground
  grace period (see tools/shell.py), hands the finished ``Job`` to the
  *notifier* — the runtime's separate callback door. Completion re-enters
  the conversation at a safe boundary instead of being typed into the
  user's prompt.
- fast commands still feel synchronous: the shell tool waits the grace
  period and returns their output inline

No-orphan guarantees:

- every running pid is persisted to ``.talos/jobs/registry.json``
- ``kill()`` takes down the whole process *tree* (process group on POSIX,
  ``taskkill /T`` on Windows) — never just the shell wrapper
- ``shutdown()`` reaps everything still running when the session ends
- an ``atexit`` hook is the crash-path backstop
- ``leftovers()`` reports pids a previous crashed session may have left,
  so the user can check them (we warn instead of killing blindly — pids
  get reused)
"""

import asyncio
import atexit
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from langchain_core.tools import tool

from talos.infra.environment import shell_command
from talos.infra.sandbox import wrap_command

MAX_TAIL_CHARS = 4_000


def jobs_dir() -> Path:
    return Path(".talos") / "jobs"


def _registry_file() -> Path:
    return jobs_dir() / "registry.json"


def _kill_tree(pid: int) -> None:
    """Terminate a process and everything it spawned. Best-effort."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10,
            )
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError,
            subprocess.TimeoutExpired):
        pass


@dataclass
class Job:
    id: int
    command: str
    pid: int
    log_path: Path
    proc: "asyncio.subprocess.Process"
    started_at: float
    status: str = "running"        # running | done | failed | killed
    exit_code: int | None = None
    finished_at: float | None = None
    background: bool = False       # True once it outlived the grace period

    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def log_age(self) -> float | None:
        """Seconds since the log last grew — the hang detector."""
        try:
            return time.time() - self.log_path.stat().st_mtime
        except OSError:
            return None

    def read_log(self, max_chars: int = MAX_TAIL_CHARS) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        if len(text) > max_chars:
            text = (f"… [{len(text) - max_chars} earlier chars omitted]\n"
                    + text[-max_chars:])
        return text

    def describe(self) -> str:
        base = (f"job #{self.id} [{self.status}] pid {self.pid} · "
                f"{self.elapsed():.0f}s · `{self.command[:60]}` · "
                f"log: {self.log_path}")
        if self.status == "running":
            age = self.log_age()
            if age is not None and age > 60:
                base += f" · ⚠️ log silent for {age:.0f}s (hanging?)"
        else:
            base += f" · exit {self.exit_code}"
        return base


class JobManager:
    """The single registry of everything Talos has spawned."""

    def __init__(self):
        self.jobs: dict[int, Job] = {}
        self._next_id = 1
        self._notify: Callable[[Job], None] | None = None
        self._atexit_armed = False

    # the runtime installs the callback door here
    def set_notifier(self, fn: "Callable[[Job], None] | None") -> None:
        self._notify = fn

    async def start(self, command: str) -> Job:
        jobs_dir().mkdir(parents=True, exist_ok=True)
        jid = self._next_id
        self._next_id += 1
        log_path = jobs_dir() / f"job-{jid}.log"
        log = open(log_path, "wb")

        cmd = shell_command(command)
        wrapped = wrap_command(command, os.getcwd())  # 📦 docker sandbox (if on)
        if isinstance(wrapped, list):
            cmd = wrapped
        kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True  # own group → killable tree

        try:
            if isinstance(cmd, list):
                proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
            else:
                proc = await asyncio.create_subprocess_shell(cmd, **kwargs)
        except Exception:
            log.close()
            raise

        job = Job(id=jid, command=command, pid=proc.pid, log_path=log_path,
                  proc=proc, started_at=time.time())
        self.jobs[jid] = job
        self._persist()
        if not self._atexit_armed:
            atexit.register(self._reap_sync)
            self._atexit_armed = True
        asyncio.ensure_future(self._watch(job, log))
        return job

    async def _watch(self, job: Job, log_handle) -> None:
        rc = await job.proc.wait()
        try:
            log_handle.close()
        except OSError:
            pass
        job.exit_code = rc
        job.finished_at = time.time()
        if job.status != "killed":
            job.status = "done" if rc == 0 else "failed"
        self._persist()
        # 🚪 the separate door: only jobs that went background announce
        # themselves — inline commands already returned their output.
        if job.background and self._notify is not None:
            try:
                self._notify(job)
            except Exception:
                pass

    def kill(self, jid: int) -> str:
        job = self.jobs.get(jid)
        if job is None:
            return f"Error: no job #{jid} in this session"
        if job.status != "running":
            return (f"job #{jid} already finished "
                    f"({job.status}, exit {job.exit_code})")
        job.status = "killed"  # set BEFORE the signal so _watch keeps it
        _kill_tree(job.pid)
        return f"🔪 killed job #{jid} (pid {job.pid} and its process tree)"

    def running(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.status == "running"]

    async def shutdown(self) -> list[Job]:
        """Reap every running job — called when the session ends."""
        victims = self.running()
        for job in victims:
            job.status = "killed"
            _kill_tree(job.pid)
        for job in victims:
            try:
                await asyncio.wait_for(job.proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        self._persist()
        return victims

    def _reap_sync(self) -> None:
        """atexit backstop — the loop may be gone, so pure os-level kills."""
        for job in self.running():
            _kill_tree(job.pid)
        try:
            _registry_file().unlink(missing_ok=True)
        except OSError:
            pass

    def _persist(self) -> None:
        """Registry of *running* pids — the crash-recovery breadcrumb."""
        try:
            entries = [
                {"pid": j.pid, "command": j.command, "started": j.started_at}
                for j in self.running()
            ]
            jobs_dir().mkdir(parents=True, exist_ok=True)
            _registry_file().write_text(
                json.dumps(entries, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def leftovers(self) -> list[dict]:
        """Entries a previous session left behind (crash without reaping).
        Reported, not killed: those pids may belong to someone else now."""
        f = _registry_file()
        if not f.is_file():
            return []
        try:
            entries = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            entries = []
        try:
            f.unlink()
        except OSError:
            pass
        return [e for e in entries if isinstance(e, dict) and e.get("pid")]


manager = JobManager()


# ── 🔧 the model-facing tools ────────────────────────────────────────────

@tool
def job_status(job_id: int = 0) -> str:
    """Check on background jobs. With job_id: full status plus the tail of
    that job's log (a log that stopped growing usually means it hangs).
    With job_id=0 (default): a one-line summary of every job this session."""
    if not manager.jobs:
        return "no jobs started this session"
    if job_id:
        job = manager.jobs.get(job_id)
        if job is None:
            return f"Error: no job #{job_id} in this session"
        tail = job.read_log()
        return job.describe() + (
            "\n--- log tail ---\n" + tail if tail else "\n(log is empty)"
        )
    return "\n".join(j.describe() for j in manager.jobs.values())


@tool
def job_kill(job_id: int) -> str:
    """Kill a background job (its entire process tree) by id. Only affects
    processes this session started, so it is always safe to call."""
    return manager.kill(job_id)
