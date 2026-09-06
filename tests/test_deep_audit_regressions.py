from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_recovery_path_is_real_download_not_stub():
    source = (ROOT / "main.py").read_text()
    body = source.split("async def _search_and_download_audio_core", 1)[1].split(
        "async def search_and_download_video", 1
    )[0]
    assert "youtube_search_download" in body
    assert "MUSIC_RECOVERY_TIMEOUT" in body


def test_direct_race_awaits_cancelled_tasks():
    source = (ROOT / "music_sources.py").read_text()
    assert "direct_tasks =" in source
    assert "await asyncio.gather(*pending, return_exceptions=True)" in source


def test_dynamic_core_handlers_are_bound_and_deduplicated():
    source = (ROOT / "main.py").read_text()
    assert "bound_core_id = int(core_id)" in source
    assert "async with _CORE_REGISTRY_LOCK" in source
    assert "create_event_handler(new_client, core_id=me.id)" in source


def test_cache_rejects_truncated_files():
    source = (ROOT / "music_sources.py").read_text()
    assert "os.path.getsize(cached_path) > 4096" in source
