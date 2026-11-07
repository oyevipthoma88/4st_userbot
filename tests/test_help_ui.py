import help_ui


def _cmds(html):
    return html


def test_core_owner_help_hides_main_owner_commands():
    html = help_ui.render_help("X", 1, role="owner", is_owner=True, is_main_owner=False)
    for c in help_ui.MAIN_OWNER_ONLY:
        assert f"<code>{c}</code>" not in html, c
    assert "SCAN" not in html
    assert "OWNER CONTROL" in html


def test_main_owner_help_shows_everything():
    html = help_ui.render_help("X", 1, role="owner", is_owner=True, is_main_owner=True)
    for c in help_ui.MAIN_OWNER_ONLY:
        assert f"<code>{c}</code>" in html, c
    assert "MASTER CONTROL" in html


def test_member_help_has_no_owner_tools_and_fits_telegram_limit():
    html = help_ui.render_help("X", 1, role="member", is_owner=False, is_main_owner=False)
    assert "<code>.growadd</code>" not in html and "<code>.scan</code>" not in html
    assert "<code>.playforce</code>" not in html
    sudo = help_ui.render_help("X", 1, role="sudo_2", is_owner=False, is_main_owner=False)
    assert "<code>.playforce</code>" in sudo
    big = help_ui.render_help("X", 1, role="owner", is_owner=True, is_main_owner=True)
    assert len(big) < 4096


def test_ping_signal_and_timeout():
    assert help_ui.signal_bars(50) == 5
    assert help_ui.signal_bars(700) == 2
    assert help_ui.signal_bars("timeout") == 0
    out = help_ui.render_ping("X", 1, ms="timeout", uptime="1s", music="m", safe="s", py="Py")
    assert "Timeout" in out
    out = help_ui.render_ping("X", 1, ms=42.5, uptime="1s", music="m", safe="s", py="Py")
    assert "42.5 ms" in out and "▰▰▰▰▰" in out
