"""🖥️ Talos CLI — the front door.

Built with Typer: each ``@app.command()`` function becomes a subcommand.

    talos chat                      💬 interactive REPL
    talos chat -n "do the thing"    ⚡ one-shot (like kiro --no-interactive)
    talos run "do the thing"        ⚡ same as above, shorter
    talos config                    ⚙️  show effective settings
    talos version                   🏷️  print version
"""

import asyncio
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from talos import __version__
from talos.config import settings

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")
console = Console()


@app.command()
def chat(
    prompt: Optional[str] = typer.Argument(
        None, help="Optional first message to seed the session."
    ),
    no_interactive: bool = typer.Option(
        False, "--no-interactive", "-n", help="Answer once and exit (no REPL)."
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m", help="Override the configured model."
    ),
    yolo: bool = typer.Option(
        False, "--yolo", help="🛡️  Skip all permission prompts (dangerous)."
    ),
    resume: Optional[str] = typer.Option(
        None, "--resume", "-r",
        help="💾 Resume a saved session: an ID from 'talos sessions', or 'latest'.",
    ),
    trace: bool = typer.Option(False, "--trace", help="🔭 Emit OpenTelemetry spans."),
) -> None:
    """💬 Chat with Talos (interactive by default)."""
    if trace:
        settings.trace = True
    from talos.infra.tracing import init_tracing

    init_tracing()
    from talos.agent.runtime import repl, run_once

    if no_interactive:
        if not prompt:
            raise typer.BadParameter("--no-interactive needs a PROMPT argument")
        asyncio.run(run_once(prompt, model, yolo=yolo))
    else:
        asyncio.run(repl(model, initial_prompt=prompt, yolo=yolo, resume=resume))


@app.command()
def run(
    prompt: str = typer.Argument(..., help="The task for Talos."),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    yolo: bool = typer.Option(
        False, "--yolo", help="🛡️  Skip all permission prompts (dangerous)."
    ),
) -> None:
    """⚡ One-shot: send a single prompt, stream the answer, exit."""
    from talos.agent.runtime import run_once

    asyncio.run(run_once(prompt, model, yolo=yolo))


@app.command()
def sessions() -> None:
    """💾 List saved chat sessions."""
    from talos.memory.sessions import list_sessions

    rows = list_sessions()
    if not rows:
        console.print("[dim]no saved sessions yet — run 'talos chat'[/]")
        return
    table = Table(title="💾 Sessions")
    table.add_column("id", style="cyan")
    table.add_column("title")
    table.add_column("messages", justify="right")
    for row in rows:
        table.add_row(row["id"], row.get("title") or "[dim]…[/]", str(row["messages"]))
    console.print(table)
    console.print("[dim]resume with: talos chat -r <id>   (or -r latest)[/]")


@app.command()
def skills() -> None:
    """🎒 List discovered skills (.talos/skills/*/SKILL.md)."""
    from talos.lifecycle.skills import discover_skills

    found = discover_skills()
    if not found:
        console.print("[dim]no skills yet — create .talos/skills/<name>/SKILL.md[/]")
        return
    table = Table(title="🎒 Skills")
    table.add_column("name", style="cyan")
    table.add_column("description")
    for s in found:
        table.add_row(s.name, s.description)
    console.print(table)


@app.command()
def agents() -> None:
    """🤖 List subagent definitions (.talos/agents/*.md)."""
    from talos.integrations.agents import discover_agents

    found = discover_agents()
    if not found:
        console.print("[dim]no subagents yet — create .talos/agents/<name>.md[/]")
        return
    table = Table(title="🤖 Subagents")
    table.add_column("name", style="cyan")
    table.add_column("description")
    table.add_column("tools", style="dim")
    for a in found:
        table.add_row(a.name, a.description, ", ".join(a.tools) or "(default read-only)")
    console.print(table)


@app.command()
def commands() -> None:
    """⌨️  List custom slash commands (.talos/commands/*.md)."""
    from talos.ui.commands import custom_commands

    found = custom_commands()
    if not found:
        console.print("[dim]no custom commands yet — create .talos/commands/<name>.md[/]")
        return
    for name, path in found.items():
        console.print(f"  [cyan]{name}[/] — {path}")


@app.command()
def mcp() -> None:
    """🔌 Show configured MCP servers and the tools they expose."""
    from talos.integrations.mcp import load_mcp_config, load_mcp_tools, mcp_config_file

    servers = load_mcp_config()
    if not servers:
        console.print(f"[dim]no MCP servers — create {mcp_config_file()}[/]")
        return
    for name, spec in servers.items():
        target = spec.get("command", spec.get("url", "?"))
        console.print(f"  [cyan]{name}[/] → {target}")
    try:
        tools = asyncio.run(load_mcp_tools())
    except (RuntimeError, ValueError) as exc:
        console.print(f"[yellow]{exc}[/]")
        return
    table = Table(title="🔌 MCP tools")
    table.add_column("tool", style="cyan")
    table.add_column("description")
    for t in tools:
        table.add_row(t.name, (t.description or "").splitlines()[0][:80])
    console.print(table)


@app.command()
def models() -> None:
    """📇 List the provider's models with context/pricing/vision info."""
    from talos.integrations.models import list_models

    try:
        found = sorted(list_models(), key=lambda m: m.id)
    except Exception as exc:
        console.print(f"[red]could not fetch models: {exc}[/]")
        raise typer.Exit(1)
    table = Table(title="📇 models")
    table.add_column("id", style="cyan")
    table.add_column("ctx", justify="right")
    table.add_column("$/M in", justify="right")
    table.add_column("$/M out", justify="right")
    table.add_column("👁", justify="center")
    for m in found:
        table.add_row(
            m.id,
            f"{m.context:,}" if m.context else "·",
            f"{m.input_per_m:.2f}" if m.input_per_m is not None else "·",
            f"{m.output_per_m:.2f}" if m.output_per_m is not None else "·",
            "👁" if m.vision else "·",
        )
    console.print(table)


@app.command()
def tui(
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    yolo: bool = typer.Option(False, "--yolo"),
) -> None:
    """🖼️  Full-screen Textual interface (experimental) — real sidebar,
    modal permission dialogs. 'talos chat' remains the classic CLI."""
    try:
        from talos.ui.tui_app import run_tui
    except ImportError:
        console.print("[red]textual not installed — pip install 'talos[tui]'[/]")
        raise typer.Exit(1)
    run_tui(model=model, yolo=yolo)


@app.command()
def link(path: str = typer.Argument(..., help="Agent dir to link, e.g. ~/.kiro")) -> None:
    """🔗 Link another agent's skills/agents/MCPs (kiro, cursor, claude…)."""
    from talos.integrations.linking import add_link

    console.print(add_link(path))


@app.command()
def links() -> None:
    """🔗 Show linked agent directories and what they contribute."""
    from talos.integrations.linking import discover_linked_mcp, discover_linked_skills, load_links

    linked = load_links()
    if not linked:
        console.print("[dim]no links — try: talos link ~/.kiro[/]")
        return
    for p in linked:
        console.print(f"  🔗 [cyan]{p}[/]")
    sk = discover_linked_skills()
    mc = discover_linked_mcp()
    console.print(f"[dim]→ {len(sk)} skill(s), {len(mc)} MCP server(s) "
                  "(deduped by name, first link wins)[/]")


@app.command()
def unlink(path: str = typer.Argument(...)) -> None:
    """🔗 Remove a linked agent directory."""
    from talos.integrations.linking import remove_link

    console.print(remove_link(path))


@app.command()
def version() -> None:
    """🏷️  Print the Talos version."""
    console.print(f"🤖 talos [bold cyan]{__version__}[/]")


@app.command()
def config() -> None:
    """⚙️  Show the effective configuration (API key masked)."""
    table = Table(title="⚙️  Talos configuration", show_header=True)
    table.add_column("setting", style="cyan")
    table.add_column("value")

    masked = (
        settings.api_key[:7] + "…" + settings.api_key[-4:]
        if len(settings.api_key) > 14
        else ("<unset>" if not settings.api_key else "•••")
    )
    table.add_row("base_url", settings.base_url or "<default — api.openai.com>")
    table.add_row("api_key", masked)
    table.add_row("model", settings.model)
    table.add_row("temperature", str(settings.temperature))
    table.add_row("max_iterations", str(settings.max_iterations))
    table.add_row("yolo", str(settings.yolo))
    table.add_row("sandbox", settings.sandbox)
    console.print(table)


# ── 📅 scheduled tasks ─────────────────────────────────────────────────
# Sub-typer for `talos schedule ...`. M49 ships the cron-only path; M50
# adds NL→cron with a human gate; M51 wires up chat-time surfacing.

schedule_app = typer.Typer(
    no_args_is_help=True,
    help="📅 Run prompts on a cron schedule (daemon + storage in .talos/schedules/).",
)
app.add_typer(schedule_app, name="schedule")


def _schedule_table(scheds) -> Table:
    """Pretty-print a list of schedules — shared between `list` and `show`."""
    from datetime import datetime

    from talos.lifecycle.scheduling import floor_for, next_fire

    table = Table(title="📅 schedules")
    table.add_column("id", style="cyan")
    table.add_column("cron", style="magenta")
    table.add_column("next fire")
    table.add_column("last fire", style="dim")
    table.add_column("✓", justify="center")
    table.add_column("prompt", style="dim")
    for s in scheds:
        try:
            nxt = next_fire(s, floor_for(s)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            nxt = "[red]?[/]"
        status = {
            "ok": "[green]✅[/]",
            "error": "[red]💥[/]",
            "skipped": "[yellow]⏭[/]",
        }.get(s.last_status, "·")
        prompt = s.prompt if len(s.prompt) < 50 else s.prompt[:47] + "…"
        table.add_row(
            s.id, s.cron, nxt, s.last_fire or "·", status, prompt,
        )
    return table


@schedule_app.command("add")
def schedule_add(
    prompt: str = typer.Argument(..., help="The prompt the agent will run on each fire."),
    when: Optional[str] = typer.Option(
        None, "--when", "-w",
        help='Cron OR natural language ("every morning at 9"). NL parses via the LLM.',
    ),
    cron: Optional[str] = typer.Option(
        None, "--cron", "-c",
        help='Cron expression — skips LLM parsing. Mutually exclusive with --when.',
    ),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Schedule id (defaults to a slug of the prompt)."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override the model just for this schedule."),
    yolo: bool = typer.Option(False, "--yolo", help="🛡️  Required if the schedule uses mutating tools — nobody's around to approve."),
    resume: bool = typer.Option(False, "--resume", help="🎟️  Use one rolling session that grows across fires (vs a fresh session each fire)."),
    tz: Optional[str] = typer.Option(None, "--tz", help="IANA timezone for cron interpretation (informational; not yet enforced)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the 'save this schedule?' confirmation prompt."),
) -> None:
    """➕ Add a scheduled task.

    Two ways to express the schedule:

      talos schedule add "summarize my inbox" --when "every morning at 9"
      talos schedule add "summarize my inbox" --cron "0 9 * * *"

    --when accepts cron OR natural language; cron syntax is tried first,
    and if it's not valid cron we ask the LLM to translate. Either way we
    show the resolved cron and the next 3 fire times and ask you to
    confirm — the human gate from the /plan flow, applied to scheduling.
    """
    import asyncio
    from datetime import datetime as _dt

    from talos.lifecycle.scheduling import (
        Schedule, list_schedules, parse_nl_to_cron, save_schedule, slugify,
        unique_id, upcoming_fires, validate_cron,
    )

    if cron and when:
        console.print("[red]use either --cron or --when, not both[/]")
        raise typer.Exit(1)
    if not cron and not when:
        console.print("[red]missing schedule — pass --when 'every morning at 9' "
                      "or --cron '0 9 * * *'[/]")
        raise typer.Exit(1)

    resolved: str
    if cron:
        try:
            resolved = validate_cron(cron)
        except (ValueError, RuntimeError) as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1)
    else:
        # 🗣️ --when path: try cron first (fast, free), fall back to the LLM.
        assert when is not None
        try:
            resolved = validate_cron(when)
        except ValueError:
            console.print(f"[dim]🗣️  parsing '{when}' via the model…[/]")
            try:
                async def _llm_call(system: str, user: str) -> str:
                    # late imports keep `talos --help` fast
                    from langchain_core.messages import (
                        HumanMessage as HM, SystemMessage as SM,
                    )
                    from talos.agent.llm import build_llm
                    from talos.agent.runtime import get_message_text

                    msg = await build_llm().ainvoke(
                        [SM(content=system), HM(content=user)]
                    )
                    return get_message_text(msg)

                resolved = asyncio.run(parse_nl_to_cron(when, _llm_call))
            except ValueError as exc:
                console.print(f"[red]🗣️  couldn't parse a cron from {when!r}: {exc}[/]")
                raise typer.Exit(1)
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]🗣️  LLM call failed: {exc}[/]")
                console.print(
                    "[dim]tip: pass a cron explicitly with --cron, "
                    "or check your TALOS_BASE_URL / TALOS_API_KEY in .env[/]"
                )
                raise typer.Exit(2)
        except RuntimeError as exc:  # croniter not installed
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(2)

    # 🚦 the human gate — show the resolved cron and the next 3 fires.
    console.print(f"[cyan]📅 resolved cron:[/] [magenta]{resolved}[/]")
    upcoming = upcoming_fires(resolved, _dt.now(), 3)
    console.print("[dim]next fires:[/]")
    for ts in upcoming:
        console.print(f"  [magenta]→[/] {ts.strftime('%Y-%m-%d %H:%M')}")
    if not yes:
        try:
            console.print(r"[yellow]save this schedule? \[Y/n] ›[/] ", end="")
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]cancelled[/]")
            raise typer.Exit(0)
        if answer and not answer.startswith("y"):
            console.print("[dim]cancelled[/]")
            raise typer.Exit(0)

    sid = unique_id(name or slugify(prompt), (s.id for s in list_schedules()))
    sched = Schedule(
        id=sid, prompt=prompt, cron=resolved, tz=tz, model=model, yolo=yolo,
        resume=resume,
    )
    save_schedule(sched)
    console.print(f"[green]📅 added [bold]{sid}[/][/] — {prompt}")
    console.print("[dim]start the daemon with: talos schedule run[/]")


@schedule_app.command("list")
def schedule_list() -> None:
    """📅 List scheduled tasks."""
    from talos.lifecycle.scheduling import list_schedules

    scheds = list_schedules()
    if not scheds:
        console.print("[dim]no schedules yet — try: talos schedule add 'do X' --cron '0 9 * * *'[/]")
        return
    console.print(_schedule_table(scheds))


@schedule_app.command("show")
def schedule_show(
    schedule_id: str = typer.Argument(..., help="Schedule id to show."),
    runs: int = typer.Option(5, "--runs", "-r", help="How many recent runs to list."),
) -> None:
    """🔎 Show one schedule plus its recent fires."""
    from talos.lifecycle.scheduling import get_schedule, list_runs

    sched = get_schedule(schedule_id)
    if sched is None:
        console.print(f"[red]no schedule named {schedule_id!r}[/]")
        raise typer.Exit(1)
    console.print(_schedule_table([sched]))
    history = list_runs(schedule_id, limit=runs)
    if not history:
        console.print("[dim]no runs yet[/]")
        return
    rtable = Table(title=f"📜 last {len(history)} run(s)")
    rtable.add_column("started", style="cyan")
    rtable.add_column("status", justify="center")
    rtable.add_column("dur")
    rtable.add_column("response", style="dim")
    icons = {"ok": "[green]✅[/]", "error": "[red]💥[/]", "skipped": "[yellow]⏭[/]"}
    for r in history:
        resp = (r.get("response") or "").splitlines()[0:1]
        text = (resp[0] if resp else "")[:80]
        dur = r.get("duration_s")
        rtable.add_row(
            r.get("started_at", "?"),
            icons.get(r.get("status"), "·"),
            f"{dur:.1f}s" if dur is not None else "·",
            text,
        )
    console.print(rtable)


@schedule_app.command("remove")
def schedule_remove(
    schedule_id: str = typer.Argument(..., help="Schedule id to remove."),
) -> None:
    """🗑️  Remove a schedule. Run history on disk is preserved."""
    from talos.lifecycle.scheduling import remove_schedule

    if remove_schedule(schedule_id):
        console.print(f"[green]🗑️  removed [bold]{schedule_id}[/][/]")
    else:
        console.print(f"[red]no schedule named {schedule_id!r}[/]")
        raise typer.Exit(1)


@schedule_app.command("run")
def schedule_run(
    tick: int = typer.Option(30, "--tick", help="Seconds between scheduler ticks."),
    once: bool = typer.Option(False, "--once", help="Run one tick and exit (useful for cron-driven setups)."),
) -> None:
    """🏃 Run the scheduler daemon — wakes on each tick and fires due schedules.

    Stop with Ctrl-C. Fires write to ``.talos/schedules/<id>/runs/<ts>.{md,json}``;
    on next ``talos chat`` the banner shows how many runs are unread.
    """
    import signal

    from talos.lifecycle.scheduling import daemon_loop

    stop = asyncio.Event()

    def _ask_stop(*_args) -> None:
        console.print("\n[yellow]🛑 stopping after current fires finish…[/]")
        stop.set()

    try:
        # SIGINT works on every platform; SIGTERM is POSIX-only — guard it.
        signal.signal(signal.SIGINT, _ask_stop)
        if hasattr(signal, "SIGTERM"):
            try:
                signal.signal(signal.SIGTERM, _ask_stop)
            except (ValueError, OSError):
                pass  # not on the main thread or unsupported
    except ValueError:
        pass

    def _log(msg: str) -> None:
        console.print(f"[dim]{msg}[/]")

    asyncio.run(
        daemon_loop(
            tick_seconds=tick,
            stop=stop,
            log=_log,
            max_ticks=1 if once else None,
        )
    )


# ── 🪞 self-knowledge ──────────────────────────────────────────────────
# `talos self show / refresh` — inspect and maintain Talos's manifest of
# its own source tree. The compact form is already in the system prompt;
# this is the human-facing view.

self_app = typer.Typer(
    no_args_is_help=True,
    help="🪞 Inspect Talos's knowledge of its own source tree.",
)
app.add_typer(self_app, name="self")


@self_app.command("show")
def self_show(
    package: Optional[str] = typer.Argument(
        None, help='Filter to one subpackage (e.g. "memory", "tools"). '
                   'Omit to show the full manifest.',
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Regenerate the manifest before showing it."),
    paths_only: bool = typer.Option(False, "--paths-only", help="Print just the file paths (one per line)."),
) -> None:
    """🪞 Print Talos's manifest of its own source tree."""
    from talos.lifecycle.self_knowledge import by_package, manifest

    facts = manifest(force_refresh=refresh)
    if not facts:
        console.print("[dim]🪞 no source files indexed — is this a fresh install?[/]")
        return
    if package:
        facts = [f for f in facts if f.package == package]
        if not facts:
            console.print(f"[red]no package named {package!r}[/]")
            raise typer.Exit(1)

    if paths_only:
        for f in facts:
            console.print(f.path)
        return

    # Group by package; render as a table per package.
    groups: dict[str, list] = {}
    for f in facts:
        groups.setdefault(f.package, []).append(f)
    for pkg in sorted(groups, key=lambda p: ("a" if p == "core" else "b", p)):
        items = groups[pkg]
        table = Table(
            title=f"🪞 {pkg}/ ({len(items)} file{'s' if len(items) != 1 else ''})",
            show_header=True, header_style="dim",
        )
        table.add_column("file", style="cyan", no_wrap=False)
        table.add_column("purpose")
        for f in items:
            table.add_row(f.path, f.purpose)
        console.print(table)


@self_app.command("refresh")
def self_refresh() -> None:
    """♻️  Force-regenerate the manifest cache (.talos/self/manifest.json)."""
    from talos.lifecycle.self_knowledge import manifest, manifest_file

    facts = manifest(force_refresh=True)
    console.print(
        f"[green]🪞 wrote {manifest_file()} — {len(facts)} module(s) indexed[/]"
    )


@self_app.command("read")
def self_read(
    file_path: str = typer.Argument(..., help='Path inside src/talos/, e.g. "memory/sessions.py".'),
) -> None:
    """📖 Print one file from Talos's source — the human-facing equivalent
    of what the agent's ``read_self`` tool does."""
    from talos.lifecycle.self_knowledge import deep_read

    try:
        text = deep_read(file_path)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(text, highlight=False)


@self_app.command("edit")
def self_edit(
    request: str = typer.Argument(..., help='What you want Talos to change about itself, in plain English.'),
    skip_tests: bool = typer.Option(False, "--skip-tests", help="Skip the test gate (faster; not recommended)."),
    keep_worktree: bool = typer.Option(False, "--keep-worktree", help="Don't delete the worktree afterwards — for poking around."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model the sub-agent should use."),
) -> None:
    """🔧 Propose a self-edit. Runs a sub-agent in an isolated git
    worktree, captures the diff, runs the test suite, and persists the
    candidate to .talos/self-edits/<id>/ for review.

    This produces a *candidate* — it does NOT apply anything to the main
    checkout. Use `talos self review` to inspect candidates and `talos
    self apply` (M54) to merge one in.
    """
    from talos.lifecycle.self_edit import default_sub_agent, run_self_edit

    def _runner(worktree, req):
        return default_sub_agent(worktree, req, model=model)

    def _log(msg: str) -> None:
        console.print(f"[dim]{msg}[/]")

    try:
        candidate = asyncio.run(run_self_edit(
            request,
            sub_agent_fn=_runner,
            skip_tests=skip_tests,
            keep_worktree=keep_worktree,
            log=_log,
        ))
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    icon = "✅" if candidate.test_passed else "❌"
    console.print(
        f"\n[green]📝 candidate [bold]{candidate.edit_id}[/]:[/] "
        f"{len(candidate.files_changed)} file(s) changed, tests {icon}"
    )
    if candidate.files_changed:
        for f in candidate.files_changed:
            console.print(f"  [cyan]·[/] {f}")
    if candidate.sub_agent_error:
        console.print(f"[yellow]⚠️ sub-agent error: {candidate.sub_agent_error}[/]")
    console.print(
        f"[dim]review with: talos self review {candidate.edit_id}[/]"
    )


@self_app.command("review")
def self_review(
    edit_id: Optional[str] = typer.Argument(None, help="Candidate id (omit to list)."),
    diff: bool = typer.Option(False, "--diff", help="Print the full diff."),
    tests: bool = typer.Option(False, "--tests", help="Print the full pytest output."),
) -> None:
    """📋 List self-edit candidates, or show one in detail."""
    from talos.lifecycle.self_edit import candidate_dir, list_candidates, load_candidate

    if edit_id is None:
        cands = list_candidates()
        if not cands:
            console.print("[dim]no self-edit candidates yet — try: talos self edit '...'[/]")
            return
        table = Table(title=f"📋 self-edit candidates ({len(cands)})")
        table.add_column("id", style="cyan", no_wrap=True)
        table.add_column("when", style="dim")
        table.add_column("✓", justify="center")
        table.add_column("files", justify="right")
        table.add_column("request", style="dim")
        for c in cands:
            req = c.request if len(c.request) < 60 else c.request[:57] + "…"
            icon = "[green]✅[/]" if c.test_passed else "[red]❌[/]"
            table.add_row(c.edit_id, c.created_at, icon,
                          str(len(c.files_changed)), req)
        console.print(table)
        return

    cand = load_candidate(edit_id)
    if cand is None:
        console.print(f"[red]no candidate named {edit_id!r}[/]")
        raise typer.Exit(1)
    icon = "[green]✅[/]" if cand.test_passed else "[red]❌[/]"
    console.print(f"[cyan]{cand.edit_id}[/]  ·  {icon}  ·  {cand.created_at}")
    console.print(f"\n[bold]Request:[/] {cand.request}")
    console.print(f"\n[bold]Files changed ({len(cand.files_changed)}):[/]")
    for f in cand.files_changed:
        console.print(f"  · {f}")
    if cand.sub_agent_error:
        console.print(f"\n[yellow]⚠️ sub-agent error:[/] {cand.sub_agent_error}")
    console.print(f"\n[dim]→ {candidate_dir(cand.edit_id)}[/]")
    if diff:
        console.print("\n[bold]Diff:[/]")
        console.print(cand.diff or "(empty)", highlight=False, markup=False)
    if tests:
        console.print("\n[bold]Tests:[/]")
        console.print(cand.test_output or "(none)", highlight=False, markup=False)


@self_app.command("apply")
def self_apply(
    edit_id: str = typer.Argument(..., help="Candidate id from `talos self review`."),
    force: bool = typer.Option(False, "--force", help="Override the protected-files refusal. Be careful."),
    no_commit: bool = typer.Option(False, "--no-commit", help="Apply the diff to the working tree but don't auto-commit."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """🧩 Apply a self-edit candidate to the main checkout.

    Requires the candidate to have passed tests; --force overrides
    protected-file refusal. After a successful apply you should
    restart any running Talos process — the in-memory code may no
    longer match what's on disk.
    """
    from talos.lifecycle.self_edit import apply_candidate, load_candidate

    cand = load_candidate(edit_id)
    if cand is None:
        console.print(f"[red]no candidate named {edit_id!r}[/]")
        raise typer.Exit(1)
    if not cand.test_passed:
        console.print("[red]🚫 refusing to apply — tests failed for this candidate[/]")
        console.print(f"[dim]see: talos self review {edit_id} --tests[/]")
        raise typer.Exit(2)
    if cand.verifier_verdict and cand.verifier_verdict.get("recommendation") == "reject":
        console.print("[red]🚫 refusing to apply — verifier recommended REJECT[/]")
        console.print(f"[dim]see: talos self review {edit_id}[/]")
        raise typer.Exit(2)
    if cand.protected_violations and not force:
        console.print(
            "[red]🛡️  refusing to apply — touches protected file(s):[/]"
        )
        for f in cand.protected_violations:
            console.print(f"  · {f}")
        console.print(
            "[dim]use --force to override (and read each file change first)[/]"
        )
        raise typer.Exit(3)

    console.print(f"[cyan]about to apply [bold]{edit_id}[/]:[/] {cand.request}")
    console.print(f"  · {len(cand.files_changed)} file(s) will change")
    if not yes:
        try:
            console.print(r"[yellow]proceed? \[y/N] ›[/] ", end="")
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]cancelled[/]")
            raise typer.Exit(0)
        if not answer.startswith("y"):
            console.print("[dim]cancelled[/]")
            raise typer.Exit(0)

    ok, msg = apply_candidate(edit_id, force=force, commit=not no_commit)
    if ok:
        console.print(f"[green]✅ {msg}[/]")
        console.print(
            "[yellow]restart any running Talos process — the in-memory code "
            "may no longer match what's on disk.[/]"
        )
    else:
        console.print(f"[red]💥 {msg}[/]")
        raise typer.Exit(1)
