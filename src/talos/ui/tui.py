"""🪟 The inline command menu — Kiro-style, no popup.

prompt_toolkit's default completion menu is a floating popup box. Modern
agent CLIs (kiro, claude code) instead render suggestions in a fixed
window UNDER the input line: type ``/`` and the menu appears in place,
↑/↓ move a highlight, the window never grows — it scrolls within its
fixed height and shows a ``+N more`` tail that updates as you move.

Three prompt_toolkit primitives make this work:

- **bottom_toolbar** — a reserved strip under the prompt, re-rendered on
  every keystroke; we draw the menu there ourselves
- **KeyBindings + Condition filters** — ↑/↓/TAB/Enter act on the menu
  only while it's visible; otherwise they keep their normal meaning
  (history, submit)
- **Style classes** — the 💗 selection bar

The lesson worth keeping: you don't need a full-screen TUI framework for
this — a prompt with a self-drawn toolbar gets you 90% of the elegance
at 10% of the complexity.
"""

import time

from prompt_toolkit.application import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

MENU_ROWS = 5  # the fixed window height — it never grows, it scrolls

# ⚒ the forge spinner — sparks fly off the hammer (ours, not kiro's dots)
SPINNER_FRAMES = ("⚒      ", "⚒ ✦    ", "⚒ ·✦   ", "⚒  ·✦  ", "⚒   ·✦ ", "⚒    ·✧", "⚒     ·")


class StatusState:
    """Shared mutable status: the Runtime writes .text, the prompt's
    toolbar renders it. One owner of the screen bottom = zero flicker —
    the agent's 'thinking' line and your input line never fight again."""

    def __init__(self):
        self.text = ""

    def render(self):
        if not self.text:
            return ""
        frame = SPINNER_FRAMES[int(time.monotonic() * 6) % len(SPINNER_FRAMES)]
        return FormattedText([("class:status", f" {frame} {self.text}")])

STYLE = Style.from_dict(
    {
        "": "#f0e6d2",                          # ✍️ input text: warm highlight
        "prompt": "bold #ffd75f",               # → the golden arrow
        "rprompt": "#6c6c6c",                   # 📊 stats pinned to the right
        "bottom-toolbar": "noreverse",          # kill the default reverse video
        "menu-row": "#9e9e9e",
        "menu-sel": "bg:#ff5fd7 #1c1c1c bold",  # 💗 the pink bar
        "menu-desc": "#6c6c6c",
        "menu-desc-sel": "bg:#ff5fd7 #3a3a3a",
        "menu-more": "#5f5f5f italic",
        "status": "#c97f2e italic",             # ⚒ molten-bronze status line
    }
)


def _commands() -> list[tuple[str, str]]:
    from talos.ui.commands import BUILTINS, custom_commands

    cmds = dict(BUILTINS)
    for name in custom_commands():
        cmds.setdefault(name, "custom command (.talos/commands)")
    return sorted(cmds.items())


class CommandMenu:
    """Menu state (selection index) + rendering."""

    def __init__(self):
        self.index = 0

    def matches(self, text: str) -> list[tuple[str, str]]:
        if not text.startswith("/") or " " in text:
            return []
        return [(n, d) for n, d in _commands() if n.startswith(text)]

    def active(self) -> bool:
        try:
            return bool(self.matches(get_app().current_buffer.text))
        except Exception:
            return False

    def selected(self) -> str | None:
        try:
            m = self.matches(get_app().current_buffer.text)
        except Exception:
            return None
        return m[self.index % len(m)][0] if m else None

    def render(self, text: str):
        m = self.matches(text)
        if not m:
            return ""
        self.index %= len(m)
        # scroll the fixed window so the selection stays visible
        start = max(0, min(self.index - MENU_ROWS + 1, len(m) - MENU_ROWS))
        visible = m[start : start + MENU_ROWS]
        width = max(len(n) for n, _ in m) + 2

        rows: list[tuple[str, str]] = []
        for i, (name, desc) in enumerate(visible, start):
            sel = i == self.index
            rows.append(
                ("class:menu-sel" if sel else "class:menu-row", f" {name:<{width}}")
            )
            rows.append(
                ("class:menu-desc-sel" if sel else "class:menu-desc", f"{desc[:70]}\n")
            )
        below = len(m) - (start + len(visible))
        if below > 0:
            rows.append(("class:menu-more", f"  ↓ +{below} more"))
        elif start > 0:
            rows.append(("class:menu-more", f"  ↑ +{start} above"))
        else:
            rows.append(("class:menu-more", "  ↑↓ move · TAB/enter select"))
        return FormattedText(rows)


def build_session(stats=None, status: StatusState | None = None):
    """A PromptSession with the inline menu wired in.

    ``stats``: optional zero-arg callable returning a short string (session
    tokens · cost). It renders as the *right prompt* — pinned to the right
    edge of the input line, always current, never polluting the transcript.

    ``status``: a StatusState; while the agent works, its text renders in
    the bottom toolbar (with the ⚒ forge spinner). The toolbar shows the
    command menu when you're typing a slash command, the status otherwise.
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.cursor_shapes import CursorShape

    menu = CommandMenu()
    kb = KeyBindings()
    menu_on = Condition(menu.active)

    @kb.add("up", filter=menu_on)
    def _up(event):
        menu.index -= 1
        event.app.invalidate()  # state changed but the buffer didn't —
                                # without this the toolbar never repaints

    @kb.add("down", filter=menu_on)
    def _down(event):
        menu.index += 1
        event.app.invalidate()

    def _accept(event):
        sel = menu.selected()
        if sel:
            buffer = event.app.current_buffer
            buffer.text = sel + " "
            buffer.cursor_position = len(buffer.text)

    kb.add("tab", filter=menu_on)(_accept)

    # Enter = "take the highlighted command" while the menu is open…
    exact = Condition(
        lambda: get_app().current_buffer.text.strip() == (menu.selected() or "")
    )

    @kb.add("enter", filter=menu_on & ~exact)
    def _enter(event):
        _accept(event)
    # …but an exact match falls through to normal Enter (submits the line).

    def toolbar():
        rendered = menu.render(session.default_buffer.text)
        if rendered:
            return rendered  # the menu wins while you're picking a command
        if status is not None:
            return status.render()
        return ""

    session = PromptSession(
        message=[("class:prompt", "→ ")],
        key_bindings=kb,
        style=STYLE,
        cursor=CursorShape.BLOCK,    # ▮ the filled-block cursor
        bottom_toolbar=toolbar,
        refresh_interval=0.4,        # ticks the spinner animation
        rprompt=(lambda: [("class:rprompt", stats() or "")]) if stats else None,
    )
    # any edit resets the highlight to the top hit
    session.default_buffer.on_text_changed += lambda _buf: setattr(menu, "index", 0)
    return session
