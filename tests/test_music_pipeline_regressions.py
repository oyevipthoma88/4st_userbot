from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_audio_download_does_not_force_web_only_client():
    source = (ROOT / "music_sources.py").read_text()
    start = source.index("async def youtube_search_download")
    body = source[start:source.index("async def youtube_video_download", start)]
    assert "_cloud_download_sync, download_target, target, True, logger" in body
    assert "[(fmt, [client], False)]" not in body
    assert "player_client=[\"web\"]" in body  # retained only in the regression explanation


def test_youtube_api_is_used_for_audio_selection():
    source = (ROOT / "music_sources.py").read_text()
    start = source.index("async def youtube_search_download")
    body = source[start:source.index("async def youtube_video_download", start)]
    assert "yt_api_enabled()" in body
    assert "yt_api_search(query, logger=logger, video=False)" in body
    assert 'download_target = api_hit["url"]' in body


def test_resolver_timeout_and_cookie_templates_exist():
    source = (ROOT / "main.py").read_text()
    assert 'os.environ.get("MUSIC_RESOLVE_TIMEOUT", "40")' in source
    assert source.index("load_dotenv()") < source.index("import music_sources")
    for path in (ROOT / ".env.example", ROOT / "botfix" / ".env.example"):
        text = path.read_text()
        assert "MUSIC_RESOLVE_TIMEOUT=40" in text
        assert "YTDLP_COOKIES" in text
