from premium_emoji import clean_pool, build_alt_map, pick_premium_id

POOL = [6172738808971268732, 4929483658114368660, 6001569493048891375]


def test_pool_drops_duplicates_and_bogus_ids():
    assert clean_pool([POOL[0], POOL[0], 14047402874, "x", POOL[1]]) == [POOL[0], POOL[1]]


def test_same_emoji_always_gets_same_premium_icon():
    assert pick_premium_id("⚡", POOL) == pick_premium_id("⚡", POOL)
    assert pick_premium_id("❤️", POOL) == pick_premium_id("❤", POOL)


def test_matching_premium_emoji_is_preferred():
    alt = build_alt_map([(111, "👑"), (222, "⚡️")])
    assert pick_premium_id("👑", POOL, alt) == 111
    assert pick_premium_id("⚡", POOL, alt) == 222
    assert pick_premium_id("🎵", POOL, alt) in POOL
