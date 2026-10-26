import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gmute_and_gban_are_enforced_for_new_and_edited_messages():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "async def inc_edit_track(event):" in source
    assert "@client.on(events.MessageEdited(incoming=True))" in source
    assert "Enforce global mute/ban on incoming messages" in source
    assert "await client.delete_messages(cid, [event.id], revoke=True)" in source


def test_global_moderation_commands_protect_self_and_owner():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "Owner ya self account ko gban nahi kar sakte" in source
    assert "Owner ya self account ko gmute nahi kar sakte" in source
    assert "UnblockRequest(id=_target)" in source
    assert "BlockRequest(id=sid)" in source


def test_how_help_includes_examples_and_unknown_command_suggestions():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "HOW COMMAND GUIDE" in source
    assert '"gmute":' in source and '"gban":' in source
    assert "difflib.get_close_matches" in source
    assert "Kya aapka matlab tha:" in source


def test_command_index_lists_global_moderation_commands():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    match = re.search(r"_CODE_COMMAND_INDEX = \((.*?)\)", source, re.DOTALL)
    assert match
    for command in ("gban", "gmute", "ungban", "ungmute", "gbanlist", "gmutelist"):
        assert f"'{command}'" in match.group(1)
