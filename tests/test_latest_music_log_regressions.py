from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_bundled_bgutil_is_not_injected():
    s=(ROOT/"music_sources.py").read_text()
    assert "_BGUTIL_PLUGIN_DIR = \"\"" in s
    assert "_sys_top.path.insert(0, _BGUTIL_PLUGIN_DIR)" not in s
    assert "duplicate provider" in s

def test_direct_cdns_race_with_download_fallback():
    s=(ROOT/"music_sources.py").read_text()
    assert "zero_disk_piped_lookup" in s
    assert "zero_disk_invidious_lookup" in s
    assert "zero_disk_jiosaavn_lookup" in s
    assert "direct_tasks =" in s
    assert "download_task = asyncio.create_task(_parallel_download())" in s

def test_parallel_errors_are_actionable():
    s=(ROOT/"music_sources.py").read_text()
    assert 'type(exc).__name__' in s


def test_music_is_not_public_by_default():
    s = (ROOT / "main.py").read_text()
    assert 'os.environ.get("MUSIC_PUBLIC", "0")' in s
    assert "music_bypass = _music_command and (_MUSIC_PUBLIC or _music_open)" in s
    # The only per-chat opt-in is the persisted .forall map.
    assert 'open_chats[str(chat_id)]' in s


def test_music_public_override_is_documented_as_explicit():
    for path in (ROOT / ".env.example", ROOT / "botfix" / ".env.example"):
        assert "MUSIC_PUBLIC=0" in path.read_text()
    assert "MUSIC_PUBLIC=1" in (ROOT / "README.md").read_text()
