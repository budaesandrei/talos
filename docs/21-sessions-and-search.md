# 21 · 💬 Sessions, global storage, and search

> Files: `memory/sessions.py`, `memory/sessions_kb.py`, `memory/knowledge.py`, `memory/embeddings.py`, `tools/sessions_tool.py`, `agent/runtime.py` (`_resolve_resume`, `reprint_history`) · Milestones: M60–M62

The sessions feature went from "JSON files in `.talos/sessions/` you
have to remember the timestamp of" to a globally-discoverable,
semantically-searchable, naturally-resumable conversation history with
the prior turns reprinted on resume. Three milestones doing one
coherent thing.

## 🗺️ The shape of it

```mermaid
flowchart TB
    subgraph storage["💾 ~/.talos/sessions/  (global)"]
        SJ["<id>.json — full message list"]
        IDX["index.json — title, usage, project_path per session"]
    end

    subgraph kb["🗂 ~/.talos/kb/sessions/index.db  (sqlite-vec)"]
        EM["chunk → embedding (384-dim, all-MiniLM-L6-v2)"]
        META["chunk metadata: source_id, role, msg_index, project_path"]
    end

    subgraph save["save_session()"]
        S1["write the JSON"]
        S2["ingest_session(id) → re-embed this session's chunks"]
        S1 --> S2 -.->|"failures swallowed —<br/>index out-of-sync is OK,<br/>save failures are not"| EM
    end

    subgraph search["talos sessions search 'auth refactor'"]
        Q1["embed query"]
        Q2["sqlite-vec knn k=5"]
        Q3["aggregate chunk hits → session hits"]
        Q4["filter by project_path (default: cwd)"]
    end

    subgraph resume["talos chat -r 'auth refactor'"]
        R1["_resolve_resume → 'latest'? exact id?<br/>else fuzzy search"]
        R2["load_session(id)"]
        R3["reprint_history → scrollback has prior turns"]
        R4["banner: ⏱ last active 14h ago"]
    end

    SJ <--> IDX
    Q1 --> Q2 --> Q3 --> Q4
    R1 --> R2 --> R3 --> R4
    EM <-- Q2
    META <-- Q2
```

## 🌍 Global storage (M60)

Pre-M60, sessions lived under `./.talos/sessions/` in the cwd. That
meant a conversation you had in `~/repos/auth-svc` was *invisible* from
`~/repos/billing-svc` — the per-project storage matched git's mental
model but failed the realistic case of "I had a chat about X last week
in some repo, where was it?"

M60 moves sessions to `~/.talos/sessions/` globally (resolved via
`vault.global_dir()`: honors `TALOS_HOME` / `XDG_CONFIG_HOME` /
`%APPDATA%` / fallback to `~/.talos`). To preserve the "this session
belongs to that project" coupling, every session is stamped with
`project_path = cwd_at_creation` in the index. The default
`talos sessions` view filters to your current project; `--all` shows
everything; `--project <path>` filters explicitly.

Legacy per-project sessions don't vanish: any session without a
`project_path` field still appears in the default view (treated as
"unknown project, show me"). `talos sessions migrate` copies any
local `.talos/sessions/*.json` into the global home and stamps them
with `project_path = current_cwd`.

## 🗂 The KB primitive (M60)

`memory/knowledge.py` is the foundation everything else in this
trilogy — and the future `/knowledge` work — sits on top of. One file,
intentionally generic:

```python
kb = KnowledgeBase.open(name="sessions", dir=root, embedder=get_embedder())
kb.add_chunks([Chunk(text=..., source_id=..., metadata={...}), ...])
hits = kb.search("query", k=5, source_id=None)  # → list[Hit]
kb.remove_source(source_id)
kb.clear(); kb.count()
```

Storage is sqlite-vec — file-based (`<dir>/<kb_id>/index.db`), no
daemon, fast at the size we care about (thousands of messages, not
millions). Each KB persists its `embedder_name` and `dim` in `kb.json`,
and `open()` refuses to query a 384-dim index with a 32-dim embedder.
Without that check, switching embedders would silently return garbage
rankings — the failure mode where everything compiles but nothing
works.

The embedder is injected through a Protocol so tests use
`HashEmbedder` (SHA-256 derived, deterministic, no model download)
while production uses `SentenceTransformersEmbedder` with
`all-MiniLM-L6-v2` — the same ~80MB model kiro's semantic
`/knowledge` uses. First use downloads it to
`~/.cache/huggingface`; offline thereafter. If
`sentence-transformers` isn't installed, `get_embedder("auto")` falls
back to the hash embedder with a clear warning so you know search
quality will be poor until you `pip install -e ".[knowledge]"`.

## 💬 SessionsKB (M60)

The first concrete user of the primitive. `memory/sessions_kb.py`
maps the session → KB shape:

- **Source ID** = session id
- **Chunk** = one message (long messages over 1500 chars are split
  with 200-char overlap so phrases near a chunk edge appear in both)
- **Chunk metadata** = `{role, msg_index, sub_index, created_at,
  project_path}`

`ingest_session(id)` is idempotent — it removes any prior chunks for
that source first, then re-adds. Means we can safely re-run after a
session grows by one turn. `ingest_all()` walks the global sessions
dir and indexes everything.

## ✏️ Search + auto-ingest (M61)

Two surfaces:

**CLI**:

```
talos sessions search "auth refactor"       # current project
talos sessions search "auth refactor" --all # cross-project
talos sessions reindex                       # catch up after deletes
```

**Agent tools** (read-only):

- `search_sessions_tool(query, k=5, scope="here")` — semantic search,
  returns JSON. Use case: "find the conversation about the auth refactor."
- `list_sessions_tool(scope="here")` — JSON listing, same scope
  semantics as the CLI.

Both are read-only by design (matches the vault pattern: agent reads,
user writes). Resume / delete stay user-actions.

**Auto-ingest on save.** `save_session()` now triggers an idempotent
re-ingest after writing the JSON. Failures are swallowed —
`save_session` *must not fail* because of an indexing problem, since
the JSON is the load-bearing artifact and the index is rebuildable.
Set `TALOS_SESSIONS_AUTOINDEX=false` to opt out (tests do this via
`conftest.py` for speed; low-resource setups can disable it
permanently).

## 🔮 Fuzzy resume (M61)

`talos chat -r <arg>` resolves `arg` in three steps:

1. **"latest"** → most recent session in the global dir.
2. **Exact id match** → use as-is (the timestamp form still works).
3. **Otherwise** → semantic search via the KB; auto-resume the best
   match (and print the top candidates so you see what it picked).

```
$ talos chat -r "auth refactor"
🔍 fuzzy resume — matched 3 candidate(s) for 'auth refactor':
  → 20260618-143000 · auth middleware extraction  (score 0.34)
    20260615-094700 · debugging the token refresh  (score 0.61)
    20260601-180000 · session validation edge cases (score 0.78)
```

The fuzzy path never silently picks the wrong session — even when it
auto-resumes the top hit, the candidates print so you can `Ctrl-C`
and try a different phrasing if needed.

## 🖨️ Resume reprint (M62)

The smaller UX win that made resuming actually feel like resuming:
after the banner, the prior message history is rendered to the
terminal so you can scroll up and see what was previously discussed.
Same rendering style as live streaming — user lines in golden,
agent header bar, tool calls as dim one-liners — just printed all
at once.

```
┌─ banner ─┐
🤖 talos · session 20260618-143000 · 💾 24 messages · ⏱ last active 14h ago

─── resumed (24 messages) ───────────────────────────────
→ how do I extract the token validation
▌⚒ talos  gpt-4o-mini
The validator lives in middleware/auth.py — you'd pull lines 42-87…
🔧 read_file({"path": "middleware/auth.py"})
   ↳ ✓ from typing import Optional
…
─────────────────────────────────────────────────────────

→ ▮   <- prompt
```

Filters: M58 gap-notice SystemMessages are excluded (UI hint, not
conversation), random SystemMessages also excluded, but the
compaction summary marker IS shown so you see when a folded history
has been summarized.

## 🧪 Testing

- `tests/test_knowledge.py` — 20 cases on the KB primitive
- `tests/test_sessions_kb.py` — 18 cases on SessionsKB + migration
- `tests/test_sessions_search.py` — 13 cases on auto-ingest + tools +
  fuzzy resume
- `tests/test_resume_reprint.py` — 6 cases on the reprint filter

Every test uses `HashEmbedder` and an isolated `HOME` so no model is
downloaded and no real global dir is touched. `conftest.py` disables
auto-ingest by default; tests that exercise it opt back in via
`monkeypatch.setenv("TALOS_SESSIONS_AUTOINDEX", "true")`.

## 🪟 What's NOT here (deliberately)

- **A "share my session" mechanism.** Sessions are personal; if you
  want to share, copy the JSON and the recipient runs
  `talos sessions migrate`. We don't have an export/import format.
- **A delete from search.** `talos sessions search` shows you what
  exists; deleting requires `rm` against the JSON (or a future
  `talos sessions remove`).
- **Cross-machine sync.** The global dir is per-machine. Syncing is
  out of scope — point `TALOS_HOME` at a synced dir if you want
  Dropbox/iCloud sharing.
- **Re-titling.** Auto-generated titles are advisory. If you want a
  specific title, that's a small follow-up — for now, the title is
  whatever the LLM came up with on the first turn.
