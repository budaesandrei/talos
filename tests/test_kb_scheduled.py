"""Tests for scheduled KB re-indexing (M65)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from talos.infra import vault
from talos.infra.vault import InMemoryBackend
from talos.lifecycle import knowledge_cli as kc
from talos.lifecycle import scheduling as sch
from talos.memory import embeddings


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir(); project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TALOS_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(project)
    embeddings.configure_default(embeddings.HashEmbedder())
    vault.configure(persistent=InMemoryBackend(), session=InMemoryBackend())
    vault._session_index.clear()
    yield
    embeddings.reset_default_embedder()
    kc.reset_fetcher()


# ── 🧱 Schedule.action_kind ──────────────────────────────────────────


def test_schedule_defaults_to_prompt_action():
    s = sch.Schedule(id="x", prompt="hello", cron="* * * * *")
    assert s.action_kind == "prompt"
    assert s.kb_id is None


def test_schedule_can_be_kb_update_kind():
    s = sch.Schedule(id="x", cron="0 6 * * *", action_kind="kb_update",
                      kb_id="docs")
    assert s.action_kind == "kb_update"
    assert s.kb_id == "docs"


def test_schedule_kb_update_round_trips_through_disk(tmp_path):
    """A kb_update schedule must serialize + reload cleanly."""
    s = sch.Schedule(id="r", cron="0 6 * * *", action_kind="kb_update",
                      kb_id="docs")
    sch.save_schedule(s)
    loaded = sch.get_schedule("r")
    assert loaded.action_kind == "kb_update"
    assert loaded.kb_id == "docs"


# ── 🔥 fire_schedule dispatches by action_kind ───────────────────────


@pytest.mark.asyncio
async def test_fire_kb_update_re_indexes_kb(tmp_path):
    """A fire of an action_kind='kb_update' schedule should call
    update_kb on the named KB."""
    # Create a URL-backed KB with a stub fetcher we can mutate
    seq = ["v1", "v2"]
    counter = {"i": 0}
    def evolving_fetch(url, headers=None, timeout=30):
        out = seq[counter["i"]]
        counter["i"] = min(counter["i"] + 1, len(seq) - 1)
        return out
    kc.configure_fetcher(evolving_fetch)
    kb, _ = kc.add_kb(name="evolve", path="https://example.com/x")
    # KB initially has "v1"
    assert kb.search("v1", k=1)[0].chunk.text == "v1"

    # Build a schedule whose action is to re-ingest this KB
    s = sch.Schedule(id="kb-re-index", cron="* * * * *",
                      action_kind="kb_update", kb_id="evolve")
    sch.save_schedule(s)

    # Fire it. No factory needed — kb_update doesn't build a Runtime.
    record = await sch.fire_schedule(s, now_fn=lambda: datetime(2026, 6, 22, 9))
    assert record.status == "ok"
    assert "re-indexed" in record.response

    # The KB now has v2
    hits = kb.search("v2", k=1)
    assert hits and "v2" in hits[0].chunk.text


@pytest.mark.asyncio
async def test_fire_kb_update_missing_kb_records_error():
    s = sch.Schedule(id="ghost", cron="* * * * *",
                      action_kind="kb_update", kb_id="no-such-kb")
    sch.save_schedule(s)
    record = await sch.fire_schedule(s)
    assert record.status == "error"
    assert "no KB" in (record.error or "")


@pytest.mark.asyncio
async def test_fire_kb_update_without_kb_id_errors():
    s = sch.Schedule(id="incomplete", cron="* * * * *",
                      action_kind="kb_update", kb_id=None)
    sch.save_schedule(s)
    record = await sch.fire_schedule(s)
    assert record.status == "error"
    assert "kb_id" in (record.error or "")


# ── 🔄 prompt-kind schedules still work unchanged ────────────────────


@pytest.mark.asyncio
async def test_existing_prompt_schedule_still_runs(tmp_path):
    """Backwards compat — schedules without action_kind explicitly set
    still default to 'prompt' and run a Runtime turn."""

    class FakeRT:
        def __init__(self, sched):
            self.messages = []
            self.usage = {"input": 1, "output": 2, "total": 3, "turns": 1}
            self.session_id = "fakesess"
            self.model_name = "fake-model"
        def turn(self, prompt):
            return f"answered: {prompt}"

    s = sch.Schedule(id="legacy", cron="* * * * *", prompt="ping")
    sch.save_schedule(s)
    record = await sch.fire_schedule(s, runtime_factory=lambda sd: FakeRT(sd))
    assert record.status == "ok"
    assert record.response == "answered: ping"
