import itertools
import random

import autoreact_engine as ar
import nc_engine as nc
import ow_engine as ow


def take(it, n):
    return list(itertools.islice(it, n))


# ── OW ──────────────────────────────────────────────────────────────────────
def test_plain_ow_fires_one_word_per_message():
    assert take(ow.build_feed("ow", ["AJAA", "BETA"], ["rahul"], ["x y"]), 3) == ["AJAA", "BETA", "AJAA"]


def test_nameow_puts_name_as_one_separate_unit_with_one_word():
    feed = ow.build_feed("nameow", ["aaja", "beta"], ["rahul ki maa"], [])
    assert take(feed, 2) == ["rahul ki maa aaja", "rahul ki maa beta"]


def test_lineow_fires_every_word_of_every_line_in_a_loop():
    feed = ow.build_feed("lineow", [], [], ["tu bhaag mat", "ruk ja"])
    assert take(feed, 6) == ["tu", "bhaag", "mat", "ruk", "ja", "tu"]


def test_missing_name_or_lines_is_reported_not_silently_looped():
    for variant, err in (("nameow", "no-name"), ("lineow", "no-lines")):
        try:
            ow.build_feed(variant, ["a"], [], [])
        except ValueError as e:
            assert str(e) == err
        else:
            raise AssertionError(variant)


def test_owtarget_replaces_and_can_be_removed():
    cfg = {}
    ow.set_names(cfg, 1, 5, ow.parse_names("rahul"))
    ow.set_names(cfg, 1, 5, ow.parse_names("aman | rohit"))
    assert ow.get_names(cfg, 1, 5) == ["aman", "rohit"]
    assert ow.clear_names(cfg, 1, 5) is True
    assert ow.get_names(cfg, 1, 5) == []


def test_owline_add_list_and_remove_by_number_range_text_all():
    cfg = {}
    assert ow.add_lines(cfg, 1, 5, ow.parse_lines("a b\nc d\ne f\ng h")) == (4, 4)
    assert ow.remove_lines(cfg, 1, 5, "2") == 1
    assert ow.remove_lines(cfg, 1, 5, "e f") == 1
    assert ow.get_lines(cfg, 1, 5) == ["a b", "g h"]
    assert ow.remove_lines(cfg, 1, 5, "all") == 2
    assert ow.get_lines(cfg, 1, 5) == []


def test_global_scope_is_fallback_and_chats_are_isolated():
    cfg = {}
    ow.set_names(cfg, 1, 0, ["g"], global_scope=True)
    ow.set_names(cfg, 1, 7, ["local"])
    assert ow.get_names(cfg, 1, 7) == ["local"]
    assert ow.get_names(cfg, 1, 8) == ["g"]
    assert ow.get_names(cfg, 2, 8) == []   # other account sees nothing


# ── Auto reaction ───────────────────────────────────────────────────────────
def on(**kw):
    s = ar.defaults(); s["on"] = True; s.update(kw); return s


def test_ar_same_mode_uses_only_that_emoji():
    assert ar.decide(on(mode="same", emoji="🔥"), 10, False) == ["🔥"]


def test_ar_who_filters_self_and_others():
    assert ar.decide(on(who="me"), 10, False) is None
    assert ar.decide(on(who="me"), 1, True)
    assert ar.decide(on(who="others"), 1, True) is None
    assert ar.decide(on(who="all"), 1, True)


def test_ar_users_mode_and_per_user_emoji_and_ignore():
    s = on(who="users", users={"10": "😂"})
    assert ar.decide(s, 11, False) is None
    assert ar.decide(s, 10, False)[0] == "😂"
    assert ar.decide(on(ignore=[10]), 10, False) is None


def test_ar_respects_chat_allowed_reactions():
    assert ar.decide(on(mode="same", emoji="🔥"), 1, False, allowed={"👍"}) is None
    assert ar.decide(on(pool=["🔥"]), 1, False, allowed={"👍"}) == ["👍"]


def test_ar_off_and_chance():
    assert ar.decide(ar.defaults(), 1, False) is None
    rng = random.Random(0)
    hits = sum(bool(ar.decide(on(chance=30), 1, False, rng=rng)) for _ in range(1000))
    assert 200 < hits < 400


def test_ar_chat_setting_wins_over_global():
    cfg = {}
    ar.edit(cfg, 1, 0, True)["on"] = True
    ar.edit(cfg, 1, 5)["on"] = False
    assert ar.resolve(cfg, 1, 5)["on"] is False
    assert ar.resolve(cfg, 1, 6)["on"] is True


def test_ar_extracts_glued_emoji():
    assert ar.extract_emojis("🔥❤️😂") == ["🔥", "❤️", "😂"]


# ── NC ──────────────────────────────────────────────────────────────────────
def test_nc_parse_all_commands_and_count():
    for cmd in ("raid", "frost", "venom", "void", "s", "shutup", "doggy"):
        assert nc.parse(f".{cmd}nc 7")[:2] == (cmd, 7)
    assert nc.parse(".raidnc") == ("raid", nc.DEFAULT_COUNT, None)
    assert nc.parse(".raidnc 99999")[1] == nc.MAX_COUNT
    assert nc.parse(".stopnc") is None


def test_nc_titles_exact_count_and_never_repeat_back_to_back():
    t = nc.build_titles("s", 25)
    assert len(t) == 25
    assert all(a != b for a, b in zip(t, t[1:]))
    assert all(len(x) <= nc.MAX_TITLE for x in t)


def test_nc_stop_commands():
    for c in (".stopall", ".stop", ".stopnc", ".stopraidnc", ".stopsnc"):
        assert nc.STOP_RE.match(c)
