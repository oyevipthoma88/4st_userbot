import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _audio_body():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    return src.split("async def search_and_download_audio", 1)[1].split(
        "async def _search_and_download_audio_core", 1)[0]


def test_shruti_api_runs_before_any_slow_source():
    body = _audio_body()
    shruti = body.index("shruti_api.fetch_audio")
    assert shruti < body.index("music_sources.resolve_zero_disk_stream")
    assert shruti < body.index("music_sources.youtube_search_download")


def test_vplay_uses_shruti_stream_before_ytdlp():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    body = src.split("async def search_and_download_video", 1)[1].split(
        "async def _search_and_download_video_core", 1)[0]
    assert body.index("shruti_api.video_stream") < body.index("youtube_video_download")
    assert "timeout=300.0" not in body


def test_session_resolver_is_awaited():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "session_user_id = await _resolve_music_session_id(session_user_id)" in src


def test_extract_video_id(monkeypatch):
    sys.modules.setdefault("music_sources", type(sys)("music_sources"))
    sys.path.insert(0, str(ROOT))
    import shruti_api
    for u in ("https://youtu.be/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3",
              "https://youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"):
        assert shruti_api.extract_video_id(u) == "dQw4w9WgXcQ"
    assert shruti_api.extract_video_id("tum hi ho") is None
    monkeypatch.setenv("SHRUTI_API_KEY", "k")
    url = shruti_api.download_url("dQw4w9WgXcQ", "audio")
    assert "type=audio" in url and "api_key=k" in url and "/download?" in url
