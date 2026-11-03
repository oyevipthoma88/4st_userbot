"""Pure premium-emoji selection rules (no Telegram imports, unit-tested).

Every plain emoji is overlaid with a premium custom emoji. The pick is:
1. a premium emoji whose own fallback (alt) is the SAME emoji, when known,
   so ⚡ stays a lightning bolt, 👑 stays a crown, etc.;
2. otherwise a stable pick from the pool, so the same emoji always looks
   the same in every message instead of changing randomly each time.
"""
import zlib

_VS16 = "\ufe0f"
_MIN_VALID_ID = 10 ** 15  # real custom-emoji document IDs are 18-19 digits


def clean_pool(ids):
    """Drop duplicates and malformed IDs while keeping order."""
    seen, out = set(), []
    for raw in ids or ():
        try:
            i = int(raw)
        except (TypeError, ValueError):
            continue
        if i < _MIN_VALID_ID or i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


def normalize(emoji: str) -> str:
    return (emoji or "").replace(_VS16, "")


def build_alt_map(pairs):
    """pairs: iterable of (document_id, alt_emoji) -> {emoji: [ids]}."""
    m = {}
    for doc_id, alt in pairs:
        key = normalize(alt)
        if key:
            m.setdefault(key, []).append(int(doc_id))
    return m


def pick_premium_id(emoji, pool, alt_map=None):
    key = normalize(emoji)
    if not key or not pool:
        return None
    h = zlib.crc32(key.encode("utf-8"))
    matches = (alt_map or {}).get(key)
    if matches:
        return matches[h % len(matches)]
    return pool[h % len(pool)]
