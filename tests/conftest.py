"""Shared test setup.

* Disable the auto-ingest hook in save_session by default. Tests that
  specifically want to exercise auto-ingest opt back in via monkeypatch.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _disable_session_autoindex(monkeypatch):
    """Most tests don't want save_session to trigger a KB write — keeps
    them fast and avoids HOME-isolation requirements for tests that
    don't use sessions search."""
    monkeypatch.setenv("TALOS_SESSIONS_AUTOINDEX", "false")
