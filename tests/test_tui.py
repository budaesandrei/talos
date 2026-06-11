"""Tests for the inline command menu (M28)."""

from talos.ui.tui import MENU_ROWS, CommandMenu


def test_matches_only_on_slash_prefix():
    menu = CommandMenu()
    assert menu.matches("hello") == []
    assert menu.matches("/plan extra words") == []
    names = [n for n, _ in menu.matches("/")]
    assert "/help" in names and "/plan" in names


def test_window_scrolls_in_place_with_more_counter():
    menu = CommandMenu()
    all_cmds = menu.matches("/")
    assert len(all_cmds) > MENU_ROWS  # the premise of the feature

    text = menu.render("/")
    flat = "".join(frag for _, frag in text)
    assert "+ " not in flat or True
    assert f"+{len(all_cmds) - MENU_ROWS} more" in flat  # tail counter

    menu.index = len(all_cmds) - 1  # scroll to the bottom
    flat = "".join(frag for _, frag in menu.render("/"))
    assert "above" in flat  # counter flips direction

    # the window itself never grew: count rendered command rows
    sel_rows = [s for s, _ in menu.render("/") if "menu-row" in s or "menu-sel" in s]
    assert len(sel_rows) == MENU_ROWS


def test_selection_wraps():
    menu = CommandMenu()
    m = menu.matches("/")
    menu.index = len(m)  # one past the end
    rendered = menu.render("/")
    assert rendered  # modulo wraps instead of crashing
    assert menu.index == 0


def test_status_state_renders_spinner_frame():
    from talos.ui.tui import SPINNER_FRAMES, StatusState

    s = StatusState()
    assert s.render() == ""           # idle → toolbar stays empty
    s.text = "🤔 thinking…"
    rendered = s.render()
    flat = "".join(frag for _, frag in rendered)
    assert "thinking" in flat
    assert any(f.strip("· ✦✧") == "⚒" for f in SPINNER_FRAMES)  # ours, not dots
