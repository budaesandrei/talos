# 20 · 🐚 Shell escape

> Files: `tools/shell_escape.py`, `ui/commands.py`, `agent/runtime.py` · Milestones: M59

Sometimes you just want to run `git status` without spending tokens.
M59 adds the standard REPL shortcut:

```
> !git status
🐚 $ git status
On branch main
nothing to commit, working tree clean
✅  ·  shared with agent
```

`!cmd` runs the command directly — no LLM in the loop, no permission
prompt, no sandbox wrapping — and shares the output with the agent so
it sees the context. `!!cmd` runs silently (output to your terminal
only, nothing added to the conversation).

## ⚙️ How it differs from the `shell` tool

The agent's `shell` tool exists for *model-initiated* commands. The
shell escape exists for *user-initiated* ones. Same `subprocess.run`
under the hood, three policy differences:

* **No permission gate.** The gate (M7) sits between "the model wants
  to" and "it happens." You're not the model — gating your own keystrokes
  is silly.
* **No sandbox wrapping.** The sandbox (M36) isolates *agent-initiated*
  commands. A user running `!cat /etc/hosts` should actually read their
  hosts file, not get an empty container.
* **Vault substitution still applies.** `!echo {{secret:ghpat}}` does
  substitute the placeholder, since the LLM isn't in the loop and
  opacity isn't violated. The output gets scrubbed before it lands in
  history (default mode), so if you accidentally print a secret the
  *agent* still doesn't see it.

## 🤝 Shared vs silent

| Form | Runs the command | Prints to terminal | Adds to message history |
|---|---|---|---|
| `!cmd` | yes | yes | yes (as `HumanMessage`, scrubbed) |
| `!!cmd` | yes | yes | no |

Shared is the right default for an agent CLI. The common case is
"here, look at this output, now do X" — you ran `!cat config.yaml`,
the file content is now in the conversation, and your next message
can just say "fix the typo on line 12." Silent is for "I just need to
check something for myself" cases like `!!ls` or `!!ps aux | grep talos`.

## 🔐 The scrub-the-whole-message detail

When a shell-escape result becomes a `HumanMessage`, the scrubber runs
on the **entire content** — not just the captured output — because the
echoed command line `[shell] $ echo {{secret:foo}}` could also leak the
plaintext if the substitution succeeded. Without scrubbing the command
line too, a user typing `!echo <pasted-secret>` would leak the secret
through the header. Tested directly in `tests/test_shell_escape.py`.

## 📐 Dispatch rules

`ui/commands.py::dispatch()` checks for `!!` before `!` (prefix overlap),
strips leading whitespace after the bang, and returns `("unknown", "!")`
for a bare bang with nothing after it (so you get a hint, not a silent
no-op). The bang must be at the very start of the line — `echo !` is a
normal chat line.

## 🧪 Testing

`tests/test_shell_escape.py` is 15 cases: dispatch classification
(single bang shared, double bang silent, whitespace tolerance, bare
bang → unknown, bang-mid-line stays chat, slash commands unaffected),
end-to-end run (output captured, exit code non-zero surfaced), silent
mode skips history, shared mode includes command + output + M58
timestamp, vault VALUE substitution works, vault SECRET scrubbing
applies to the whole history message, unresolved placeholders are
reported, /help mentions the syntax.
