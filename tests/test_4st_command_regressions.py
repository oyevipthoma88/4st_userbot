from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_ow_data_has_no_legacy_token_or_emoji():
    emoji_ranges = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]")
    for path in (ROOT / "data" / "ow.txt", ROOT / "botfix" / "data" / "ow.txt"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?i)(?<!\w)4st(?!\w)", text)
        assert not emoji_ranges.search(text)


def test_runtime_ow_cleaner_is_used_for_ow_path():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "if os.path.abspath(file_path) == os.path.abspath(OW_PATH):" in source
    assert "lines = [_clean_ow_line(line) for line in lines]" in source


def test_owtarget_stores_exact_user_text_and_firing_uses_custom_word():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    target_block = source.split("elif re.match(r\"(?i)^\\.(?:addow|owtarget)", 1)[1].split(
        'elif re.match(r"(?i)^\\.mastersync', 1
    )[0]
    assert "resolve_target" not in target_block
    assert "new_words = [_arg]" in target_block
    assert "target_name = _arg" in target_block
    assert "_humanize_ow_line(" in source
    assert "_ow_custom[_cust_idx % len(_ow_custom)]" in source


def test_cmndrmall_resets_aliases_and_text_commands():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 're.match(r"(?i)^\\.cmndrmall$"' in source
    assert 'cfg.setdefault("CUSTOM_CMDS", {})[my_id_str] = {}' in source
    assert 'cfg.setdefault("CMD_ALIASES", {})[my_id_str] = {}' in source
    assert "'cmndrmall'" in source


def test_each_how_response_gets_an_example_and_stop_keys_match_tasks():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'if "Example:" not in doc:' in source
    assert 'istate.active_tasks[_fkey] = asyncio.current_task()' in source
    assert 'task = istate.active_tasks.get(tgt) or istate.active_tasks.get("tagall")' in source
    assert 'stop_cmd[4:] if stop_cmd.startswith("stop") else ""' in source
