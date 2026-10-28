"""Regressions for the stop / ow-target / speed fixes.

These are source-level checks (main.py cannot be imported without Telegram
credentials), matching the style of the other tests in this folder.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")


def test_stop_handles_stopall_and_stopghost():
    assert "stopall" in SOURCE and "stopghost" in SOURCE
    assert 'elif tgt in ("", "m", "all"):' in SOURCE
    assert "def _mod_stop_all(istate)" in SOURCE


def test_every_long_running_module_is_stop_flag_aware():
    for probe in (
        '_mod_should_stop(istate, "raid", _dest)',
        '_mod_should_stop(istate, "spam", dest)',
        "_mod_should_stop(istate, mode, dest)",
        "_mod_should_stop(istate, _fkey, chat_id)",
    ):
        assert probe in SOURCE, probe


def test_ow_variants_share_one_engine():
    assert '_word_idx % 3 == 0' not in SOURCE
    assert "_ow_engine.build_feed(" in SOURCE
    assert "(ow|fuck|nameow|lineow)" in SOURCE
    # multi-line .addowline / .owtarget arguments must parse (DOTALL)
    assert 'r"(?is)^\\.(?:addowline|owline)' in SOURCE


def test_ow_start_replaces_older_loop_and_flood_is_per_account():
    assert "_old.cancel()" in SOURCE
    assert "_FLOOD_STATE.get((me_id, chat_id))" in SOURCE
    assert "_FLOOD_QUEUE_MAX = 50" in SOURCE


def test_speed_floor_and_clamp():
    assert "_MIN_SEND_DELAY = 0.02" in SOURCE
    assert "spd = max(_MIN_SEND_DELAY, min(spd, 60.0))" in SOURCE
    assert "max(0.05, base * random.uniform(0.78, 1.32))" not in SOURCE


def test_self_lookup_is_cached_on_hot_paths():
    assert "async def get_me_cached(client" in SOURCE
    assert "me_id  = (await get_me_cached(client)).id" in SOURCE
    # No hot-path handler may do a fresh network self-lookup per update.
    assert "istate = get_isolated_state((await client.get_me()).id)" not in SOURCE
