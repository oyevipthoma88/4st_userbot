from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_main_resolves_youtube_api_before_direct_race():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    body = source.split("async def search_and_download_audio", 1)[1].split(
        "async def _search_and_download_audio_core", 1
    )[0]
    api_pos = body.index("music_sources.yt_api_search")
    direct_pos = body.index("music_sources.resolve_zero_disk_stream")
    assert api_pos < direct_pos
    assert "youtube_target=_youtube_target" in body
    assert "timeout=1.2" in body


def test_youtube_direct_result_is_not_probed_twice():
    source = (ROOT / "music_sources.py").read_text(encoding="utf-8")
    direct = source.split("async def youtube_direct_stream", 1)[1].split(
        "async def resolve_zero_disk_stream", 1
    )[0]
    resolver = source.split("async def resolve_zero_disk_stream", 1)[1].split(
        "async def try_extra_sources", 1
    )[0]
    assert 'result["probe_verified"] = True' in direct
    assert 'result.get("probe_verified") or' in resolver
    assert "await _direct_stream_is_playable(" in resolver
    assert "youtube_direct_stream(youtube_target or q" in resolver