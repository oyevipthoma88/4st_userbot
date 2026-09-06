from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_audio_fallback_has_a_practical_configurable_budget():
    for path in (ROOT / "main.py", ROOT / "botfix" / "main.py"):
        source = path.read_text()
        assert 'os.environ.get("MUSIC_RESOLVE_TIMEOUT", "40")' in source
        assert "timeout=_remaining" in source
        assert "hard 10s budget" not in source


def test_timeout_is_documented_in_environment_templates():
    for path in (ROOT / ".env.example", ROOT / "botfix" / ".env.example"):
        assert "MUSIC_RESOLVE_TIMEOUT=40" in path.read_text()
