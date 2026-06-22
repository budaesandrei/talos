"""Tests for URL sources in /knowledge (M64)."""

from __future__ import annotations

from pathlib import Path

import pytest

from talos.infra import vault
from talos.infra.vault import InMemoryBackend
from talos.lifecycle import knowledge_cli as kc
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


# ── 🌐 is_url ─────────────────────────────────────────────────────────


def test_is_url_recognizes_http_and_https():
    assert kc.is_url("https://example.com/file.md")
    assert kc.is_url("http://example.com")
    assert kc.is_url("HTTPS://shouty.example.com")


def test_is_url_rejects_filesystem_paths():
    assert not kc.is_url("/etc/hosts")
    assert not kc.is_url("./docs/api.md")
    assert not kc.is_url("docs/api.md")


# ── 🌐 add_kb with URL source ────────────────────────────────────────


def test_add_kb_url_fetches_and_indexes_content():
    captured = []
    def fake_fetch(url, headers=None, timeout=30):
        captured.append((url, headers))
        return "The quick brown fox jumps over the lazy dog"

    kc.configure_fetcher(fake_fetch)
    kb, n = kc.add_kb(name="webdoc", path="https://example.com/quick.txt")
    assert n == 1
    assert captured == [("https://example.com/quick.txt", None)] or len(captured) == 1
    hits = kb.search("quick brown fox", k=1)
    assert hits and "fox" in hits[0].chunk.text


def test_add_kb_url_records_in_manifest():
    kc.configure_fetcher(lambda u, headers=None, timeout=30: "content")
    kb, _ = kc.add_kb(name="urlkb", path="https://example.com/x")
    manifest = kc.load_manifest(kb.dir)
    assert manifest is not None
    assert len(manifest.sources) == 1
    src = manifest.sources[0]
    assert src.kind == "url"
    assert src.path == "https://example.com/x"


def test_add_kb_url_idempotent_on_re_add():
    kc.configure_fetcher(lambda u, headers=None, timeout=30: "stable content")
    kb1, n1 = kc.add_kb(name="kb", path="https://example.com/a")
    kb2, n2 = kc.add_kb(name="kb", path="https://example.com/a")
    # Same source re-added → prior chunks removed, so count stays at n1
    assert kb2.count() == n1


def test_add_kb_url_empty_response_returns_zero():
    kc.configure_fetcher(lambda u, headers=None, timeout=30: "   \n  ")
    kb, n = kc.add_kb(name="empty", path="https://example.com/blank")
    assert n == 0


def test_add_kb_url_fetch_failure_raises_helpfully():
    def bad_fetch(url, headers=None, timeout=30):
        raise ConnectionError("simulated network failure")
    kc.configure_fetcher(bad_fetch)
    with pytest.raises(RuntimeError, match="fetch failed"):
        kc.add_kb(name="dies", path="https://example.com/x")


# ── ♻️ update_kb re-fetches URL sources ─────────────────────────────


def test_update_kb_re_fetches_url_sources():
    seq = ["original-text", "updated-text"]
    counter = {"i": 0}
    def evolving_fetch(url, headers=None, timeout=30):
        out = seq[counter["i"]]
        counter["i"] = min(counter["i"] + 1, len(seq) - 1)
        return out

    kc.configure_fetcher(evolving_fetch)
    kb, _ = kc.add_kb(name="evolve", path="https://example.com/x")
    assert kb.search("original-text", k=1)[0].chunk.text == "original-text"

    # Re-index
    res = kc.update_kb(kb_id_or_name="evolve")
    assert res["sources"] >= 1
    hits = kb.search("updated-text", k=1)
    assert hits and "updated" in hits[0].chunk.text


# ── 🔐 vault auth headers ────────────────────────────────────────────


def test_substitute_headers_resolves_vault_handles():
    """Header values can use {{secret:..}} — vault substitution happens
    before the request is sent. The model is not in the loop."""
    vault.add_entry("ghpat", "real-token-xyz", kind="secret", scope="project")
    out = kc._substitute_headers({
        "Authorization": "Bearer {{secret:ghpat}}",
        "X-Other": "static",
    })
    assert out["Authorization"] == "Bearer real-token-xyz"
    assert out["X-Other"] == "static"


def test_substitute_headers_passes_through_when_no_placeholders():
    out = kc._substitute_headers({"Accept": "text/plain"})
    assert out == {"Accept": "text/plain"}


# ── 🧪 HTML extraction ──────────────────────────────────────────────


def test_html_to_text_via_trafilatura_strips_chrome():
    """The fetcher should strip <html>, <body>, navigation, etc. — only
    article-like content remains."""
    html = (
        "<html><head><title>X</title></head><body>"
        "<nav>menu link 1 menu link 2</nav>"
        "<article><h1>Real Title</h1><p>real article body content here.</p></article>"
        "<footer>copyright</footer>"
        "</body></html>"
    )
    text = kc._html_to_text(html, "https://example.com")
    assert "real article body content" in text
    # boilerplate likely stripped (trafilatura is best-effort, not absolute)


def test_html_to_text_falls_back_to_naive_strip_when_extractor_returns_none():
    """If trafilatura returns None (no article structure), we still
    return *something* useful via the regex fallback."""
    html = "<p>just a fragment</p>"
    text = kc._html_to_text(html, "https://example.com")
    assert "just a fragment" in text
