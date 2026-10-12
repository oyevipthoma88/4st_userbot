"""Regression tests for the masked-ProcessLookupError root fix.

Log symptom that motivated these:
    [MUSIC_PLAY_ERR] -> ProcessLookupError() [attempt 0..3] — rebuilding PyTgCalls.
    [MUSIC_TIMING]   -> voice play took 9.16s | total=12.65s | ok=False

PyTgCalls runs ffprobe on the source first; when ffprobe fails it decodes an
empty stdout (JSONDecodeError) and then kills an already-dead process, which
asyncio reports as a bare ProcessLookupError(). The real fault is the source,
so rebuilding the engine four times only burned ~9s and never played anything.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_own_ffprobe_gate_exists():
    s = (ROOT / "music_sources.py").read_text()
    assert "async def ffprobe_stream_ok(" in s
    # must ask ffprobe for JSON exactly like PyTgCalls does
    assert '"-of", "json"' in s
    assert "HTTP_STREAM_FFMPEG_FLAGS" in s
    assert "LOCAL_FILE_FFMPEG_FLAGS" in s


def test_every_zero_disk_provider_passes_the_real_probe():
    s = (ROOT / "music_sources.py").read_text()
    assert "await _direct_stream_is_playable(\n                    url, HTTP_STREAM_FFMPEG_FLAGS, logger)" in s
    assert 'result["probe_verified"] = True' in s


def test_remote_probe_budget_is_small_enough_for_5s_play():
    s = (ROOT / "music_sources.py").read_text()
    assert "-probesize 1000000 -analyzeduration 1500000" in s
    # the old 5 MB remote probe (seconds of CDN download) must be gone from
    # the HTTP flag set
    assert "-probesize 5000000 -analyzeduration 5000000 " not in s


def test_malformed_headers_flag_is_not_passed_to_ffmpeg():
    s = (ROOT / "main.py").read_text()
    assert "-headers 'User-Agent:" not in s


def test_play_probes_before_pytgcalls():
    s = (ROOT / "main.py").read_text()
    assert "class StreamProbeFailed(Exception):" in s
    assert "raise StreamProbeFailed(_probe_why)" in s
    assert "except StreamProbeFailed as _probe_err:" in s
    # gate must run BEFORE tgcalls.play()
    assert s.index("raise StreamProbeFailed(_probe_why)") < s.index("await tgcalls.play(chat_id, stream)")


def test_process_lookup_error_switches_source_instead_of_rebuild_loop():
    s = (ROOT / "main.py").read_text()
    assert "async def _play_via_source_switch(" in s
    block = s.split("except ProcessLookupError:", 1)[1].split("except StreamProbeFailed", 1)[0]
    assert "_play_via_source_switch" in block
    assert "if _attempt >= 1:" in block
    assert "Max retries reached — giving up." not in block


def test_disk_fallback_is_allowed_by_default():
    s = (ROOT / "main.py").read_text()
    body = s.split("async def search_and_download_audio", 1)[1].split(
        "async def _search_and_download_audio_core", 1)[0]
    assert "music_sources.youtube_search_download" in body
    assert "MUSIC_ALLOW_SLOW_FALLBACK" not in body


def test_verified_direct_track_is_not_probed_twice():
    s = (ROOT / "main.py").read_text()
    assert "self.probe_verified = False" in s
    assert 't.probe_verified = bool(direct.get("probe_verified"))' in s
    assert 'if not getattr(track, "probe_verified", False):' in s


def test_slow_probe_is_not_treated_as_broken_stream():
    s = (ROOT / "music_sources.py").read_text()
    assert 'return True, "ffprobe slow (allowed through)"' in s
