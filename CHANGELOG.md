# Changelog

Talos uses **milestones** as the unit of change. Each milestone is one
focused, independently-shippable feature with its own docs chapter and
offline tests. Semantic versions bump when a trilogy or major
architectural shift lands.

The format roughly follows [Keep a Changelog](https://keepachangelog.com/);
see the project's `docs/` directory for the long-form chapters
describing the design of each feature.

## [0.3.0] · M49–M67

Scheduling, self-knowledge + safe self-modification, secrets vault,
time-awareness, shell escape, global sessions + vector search,
kiro-style `/knowledge` with URL sources and scheduled re-indexing,
and a full agent-tool backfill — 18 milestones, test count grew from
116 to 376.

### M49–M51 · 📅 Scheduled tasks

* **M49** — `talos schedule add|list|show|remove|run` with a cron-only
  daemon. `Schedule` pydantic model. Cron-style "skip overlapping"
  semantics. New `[schedule]` extra (`croniter`).
* **M50** — `--when` accepts cron OR natural language; LLM parses NL
  → cron with a y/N human gate showing the resolved expression and
  next 3 fire times. Opt-in rolling session via `--resume`.
* **M51** — `📬 N scheduled runs since last open` banner line on
  REPL startup. `/runs` slash command to browse + mark-read.
  `docs/16-scheduled-tasks.md`.

### M52–M54 · 🪞 Self-knowledge + 🔧 safe self-modification

* **M52** — `lifecycle/self_knowledge.py` walks `src/talos/` and
  produces a manifest (one line per file, from docstring first
  sentences). Compact index injected into the system prompt every
  turn so the agent knows its own layout without grepping.
  `read_self(file_path)` tool for lazy full-file reads.
  `talos self show|refresh|read` CLI.
* **M53** — `talos self edit "<request>"` runs a sub-agent in an
  isolated `git worktree`, captures the diff, runs `pytest`, persists
  a `SelfEditCandidate` to `.talos/self-edits/<id>/`. Host process
  never touches its own source.
* **M54** — Verifier scores the diff against the request (reuses M37
  judge pattern). `PROTECTED_FILES` allowlist refuses to merge edits
  to the safety machinery without `--force`. `talos self apply <id>`
  with human gate + `git apply --3way`. `docs/17-self.md`.

### M55–M57 · 🔐 Vault

* **M55** — Secrets and scoped values, three scopes (session /
  project / global), backed by OS keyring. `~/.talos/` established
  as a first-class global config dir. `vault_get` tool refuses
  SECRET handles. `talos vault add` defaults to `getpass` so values
  never enter argv. New `[vault]` extra.
* **M56** — `{{secret:name}}` / `{{value:name}}` substitution in
  shell commands at exec time (model never sees plaintext). Output
  scrubber redacts revealed secret values from tool outputs before
  they enter message history. System-prompt projection lists
  handles. `/vault` slash command.
* **M57** — Vault files added to `PROTECTED_FILES`. Cross-platform
  `global_dir()` resolution (`TALOS_HOME` → `%APPDATA%` →
  `$XDG_CONFIG_HOME` → `~/.talos`). `docs/18-vault.md` with the
  honest "honest-leak defense, NOT adversarial defense" disclaimer.

### M58 · ⏱ Time-awareness

* Per-message timestamps via `additional_kwargs['created_at']`
  (idempotent — first stamp wins, survives compaction/resume).
* `detect_gap()` triggers a `SystemMessage` injection + dim terminal
  notice when a new turn arrives after more than `gap_minutes`
  (default 30, configurable via `TALOS_GAP_MINUTES`).
* Resume banner gains `⏱ last active 14h ago`.
* Compaction summaries get a time-range string so digests can
  mention "these turns spanned 2 days". Backward compat for two-arg
  summarize callables via TypeError fallback. `docs/19-time-awareness.md`.

### M59 · 🐚 Shell escape

* `!cmd` runs a command directly (no LLM, no permission gate, no
  sandbox wrapping) and adds the result to chat history. `!!cmd` is
  silent (output to terminal only).
* Vault substitution applies; scrubber redacts the whole history
  message (including the echoed command line, not just the output).
* `/help` documents the syntax. `docs/20-shell-escape.md`.

### M60–M62 · 💬 Sessions, search, fuzzy resume

* **M60** — Content-agnostic `KnowledgeBase` primitive in
  `memory/knowledge.py` (sqlite-vec + injectable `Embedder`).
  `SentenceTransformersEmbedder` (~80MB `all-MiniLM-L6-v2`) default
  with `HashEmbedder` fallback for tests. Sessions migrated to
  `~/.talos/sessions/` globally with `project_path` metadata.
  `talos sessions migrate` for legacy `.talos/sessions/`.
  `SessionsKB` as first concrete user of the primitive.
* **M61** — `talos sessions search "..."` + `reindex`. Two
  agent tools (`search_sessions_tool`, `list_sessions_tool`).
  Auto-ingest on `save_session()` (failures swallowed; toggle via
  `TALOS_SESSIONS_AUTOINDEX`). Fuzzy resume — `talos chat -r "auth
  refactor"` does a vector search when no exact id matches.
* **M62** — `reprint_history()` renders prior turns to the terminal
  after the banner on resume (kiro-style "scroll up to see what we
  discussed"). Filters gap-notices but keeps compaction summaries.
  `docs/21-sessions-and-search.md`.

### M63–M65 · 🗂 /knowledge

* **M63** — `talos knowledge add|show|update|remove|clear|search`
  for local files/dirs. Supported file types match kiro's set; glob
  patterns expand `**/*.py` to also match top-level files (Python
  `fnmatch` doesn't have glob's `**` semantic natively). Idempotent
  source-level re-ingest. Five agent tools (read + write). User-set
  KBs stored under `~/.talos/kb/user/` so they don't pollute the
  SessionsKB view.
* **M64** — `talos knowledge add` accepts http(s) URLs. HTML
  through `trafilatura` for chrome/nav stripping. Vault
  `{{secret:...}}` substitution in header values for private URL
  auth. Injectable fetcher for tests.
* **M65** — `Schedule.action_kind` enum extended with `kb_update`
  (+ `kb_id` field). `talos knowledge add --schedule "..."` creates
  both KB and paired schedule in one command. The
  agent-as-continuous-advisor pattern. `docs/22-knowledge.md`.

### M66 · 🛠 Full agent-tool backfill

* 11 new agent tools in `tools/meta_tools.py` for every non-safety
  CLI verb: schedules (CRUD), runs (list), models (list),
  checkpoints (list), skills (create), vault handles (read-only
  list — never values), MCP servers (list), linked agents (list).
* Tool registry grows to 33 tools (from 15 pre-session). The agent
  can now drive Talos in natural language for every non-safety
  action.
* User-only forever: vault writes (add/remove/reveal), self-edit
  apply, checkpoint restore, settings / policy / sandbox changes.

### M67 · 📝 Housekeeping

* CHANGELOG.md (this file).
* LICENSE — Apache 2.0.
* CONTRIBUTING.md with a CLA note + dev setup.
* README test count + layout block brought current; `docs/05`
  updated for the global sessions path.
* Version bumped 0.2.0 → 0.3.0.

## [0.2.0] · M27–M48 (pre-CHANGELOG)

The pre-CHANGELOG history is preserved in the git log
(`git log --oneline`); milestones M27–M48 covered prompt_toolkit
input, animated banner, kiro-style inline command menu, the Textual
TUI, auto-compaction, GraphRAG memory, time-travel checkpoints, the
verifier pattern, OTel tracing, cross-agent linking, skill synthesis,
parallel teams, vision, `/init`, the `/evolve` ouroboros, and the
bordered-user-message input UX.

## [0.1.0] · M4–M26 (pre-CHANGELOG)

Original learning-first milestones — provider-agnostic LLM config,
the ReAct loop, tools, permissions, context layers, sessions, slash
commands, skills, subagents, MCP, documentation, the SSL toggle,
crash-safe sessions, live markdown, mermaid rendering, usage tracking,
the 8-bit banner, interjections, environment awareness, session
titles, `/models` + cost, `/plan` (AI-DLC), and reasoning support.
