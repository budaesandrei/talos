# Contributing to Talos

Thanks for your interest in Talos! This is a learning-first project, so
the bar for contributions is "does it make the codebase easier to read
and understand" alongside "does it work."

## Quick start

```bash
git clone https://github.com/budaesandrei/talos.git
cd talos
pip install -e ".[dev,mcp,schedule,vault,knowledge]"
pytest -q             # should be all green
```

The `[knowledge]` extra downloads `sentence-transformers` (and its
~80MB model on first use). If you're working on something unrelated
to embeddings, leave it off and the hash-embedder fallback is used.

## Project conventions

These exist because they're load-bearing for the learning-first goal —
new code should follow them so the repo stays readable as a tutorial.

* **One milestone per commit.** A milestone is one focused,
  independently-shippable feature with its own docs chapter and
  offline tests. Commit messages start with `Mxx: <emoji> <topic>`
  and the body explains what shipped + why. See `git log` for the
  pattern.

* **Feature subpackages, not layers.** Code lives under
  `src/talos/<feature>/` (e.g., `memory/`, `lifecycle/`,
  `integrations/`). Don't introduce `utils/`, `models/`, or
  `helpers/` directories — group by capability.

* **Lazy imports in CLI commands.** Every `@app.command()` body
  imports its deps inside the function. Keeps `talos --help` fast.

* **Docstrings on every module.** The first sentence of the module's
  docstring is what `read_self`'s manifest shows the agent. Make it
  count.

* **Tests are offline.** No network, no API keys, no real LLM. Use
  `FakeToolCallingModel` from `tests/fakes.py` for graph tests and
  `HashEmbedder` for knowledge tests. The fake versions are
  battle-tested — extend them rather than mock at the call site.

* **Heavy deps go in `[extras]`.** Anything beyond the core
  `dependencies` list lives in an optional extra (`[knowledge]`,
  `[vault]`, `[mcp]`, etc.) with a graceful `ImportError` →
  `RuntimeError` message pointing at the install command.

* **Safety machinery is on the `PROTECTED_FILES` allowlist.** Files
  in `src/talos/infra/{policy,permissions,sandbox,vault}.py` plus
  the self-edit code itself can't be modified by `talos self edit`
  without `--force`. Add new safety files to the list.

## Pull requests

1. Open an issue first for anything bigger than a typo fix — the
   "is this in the project's character" conversation is faster up
   front than in PR review.
2. Run `pytest -q` and confirm green before pushing.
3. Add or update the relevant chapter under `docs/`. New milestones
   need a new chapter; existing milestone changes update the
   existing one.
4. Update `CHANGELOG.md` under the current `## [Unreleased]` section
   (add the section if missing).

## Contributor License Agreement (CLA)

Talos is licensed under Apache 2.0, but the project maintainer
retains the right to relicense the code (or a successor version)
under different terms in the future — e.g., a source-available or
dual-licensed model if commercial sustainability requires it.

For this to be legally feasible, all contributions need to be
covered by a Contributor License Agreement assigning the necessary
rights to the maintainer (you keep your copyright; you grant a
license that includes relicensing).

A formal CLA-signing flow is **not yet set up**. Until it is, by
submitting a pull request you agree that your contribution may be
included in future relicensed versions of Talos. This will be
formalized via a CLA bot before the project's first commercial
release, if any.

If that's a deal-breaker for you, that's understandable — flag it on
the issue/PR and we'll figure it out together. Many successful
projects have walked this exact path (Sentry, Elastic, MongoDB,
HashiCorp); the CLA is standard insurance, not a sign of impending
enclosure.

## License

By contributing, you agree your contributions are licensed under the
Apache License 2.0 (see `LICENSE`).
