from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_audio_fallback_has_a_practical_configurable_budget():
    for path in (ROOT / "main.py",):
        source = path.read_text()
        assert 'os.environ.get("MUSIC_RESOLVE_TIMEOUT", "40")' in source
        assert "resolve_budget - (time.perf_counter() - started)" in source
        assert "hard 10s budget" not in source


def test_direct_result_is_not_probed_twice_before_playback():
    source = (ROOT / "main.py").read_text()
    body = source.split("async def search_and_download_audio", 1)[1].split(
        "async def _search_and_download_audio_core", 1
    )[0]
    assert "resolve_zero_disk_stream" in body
    assert "_direct_stream_is_playable" not in body


def test_voice_prejoin_overlaps_normal_play_search():
    source = (ROOT / "main.py").read_text()
    body = source.split('re.match(r"(?i)^\\.play(?:\\s+(.+))?$", text)', 1)[1].split(
        're.match(r"(?i)^\\.(skip|cut)$", text)', 1
    )[0]
    assert body.index("asyncio.create_task(_prejoin_vc_bg())") < body.index(
        "search_and_download_audio(query)"
    )


def test_timeout_is_documented_in_environment_templates():
    for path in (ROOT / ".env.example",):
        if not path.exists():
            continue
        assert "MUSIC_RESOLVE_TIMEOUT=40" in path.read_text()
