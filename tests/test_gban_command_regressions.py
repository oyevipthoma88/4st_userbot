import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_global_moderation_command_patterns_match_plain_commands():
    patterns = {
        "gban": r"(?i)^\.gban(?:\s+(.*))?$",
        "gmute": r"(?i)^\.gmute(?:\s+(.*))?$",
        "ungban": r"(?i)^\.ungban(?:\s+(.*))?$",
        "ungmute": r"(?i)^\.ungmute(?:\s+(.*))?$",
        "gbanlist": r"(?i)^\.gbanlist$",
        "gmutelist": r"(?i)^\.gmutelist$",
    }
    examples = {
        "gban": ".gban @member spam",
        "gmute": ".gmute 12345 reason",
        "ungban": ".ungban @member",
        "ungmute": ".ungmute 12345",
        "gbanlist": ".gbanlist",
        "gmutelist": ".gmutelist",
    }
    for command, pattern in patterns.items():
        match = re.match(pattern, examples[command])
        assert match, command
        if command in ("gban", "gmute", "ungban", "ungmute"):
            assert match.group(1)


def test_runtime_global_moderation_handlers_are_authorized_and_persisted():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    block = source.split("# ─────── GBAN ───────", 1)[1].split(
        'elif re.match(r"(?i)^\\.how', 1
    )[0]
    for command in ("gban", "gmute", "ungban", "ungmute", "gbanlist", "gmutelist"):
        assert f"^\\.{command}" in block
        assert "verify_privileges(event, client=client)" in block
    assert "save_config(cfg)" in block
    assert "github_store.push_config_async(cfg" not in block
