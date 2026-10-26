from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "main.py").read_text(encoding="utf-8")


def test_ping_help_no_longer_bypass_privilege_gate():
    assert "_is_ping_help" not in SRC
    assert "if not music_bypass and not await verify_privileges(" in SRC


def test_global_owner_role_requires_master_sync_on_other_cores():
    block = SRC.split("def is_sender_owner(", 1)[1].split("def get_sender_role(", 1)[0]
    assert '_cfg.get("MASTER_SYNC", False)' in block


def test_sudo_bucket_ids_are_int_normalized():
    block = SRC.split("async def verify_privileges(", 1)[1].split("def is_sender_owner(", 1)[0]
    assert "return sender in _allowed" in block
