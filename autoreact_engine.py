"""Auto-reaction engine (pure logic — no Telegram imports, fully testable).

Per core (account) + per chat settings, with a "*" global fallback:

    on      bool    master switch
    mode    random | same
    emoji   emoji used in `same` mode
    pool    custom pool for `random` mode ([] -> DEFAULT_POOL)
    who     all | me | others | users
              all     -> everybody's messages, including your own
              me      -> only your own messages
              others  -> everybody except you
              users   -> only the users added with `.ar user`
    users   {user_id: emoji or ""}  per-user emoji ("" -> normal mode/pool)
    ignore  [user_id]               never react to these users
    chance  1..100  probability per message
    delay   seconds minimum gap between two reactions in this chat
    bots    bool    react to bot messages
    multi   1..3    reactions per message (premium accounts)
"""
from __future__ import annotations

import random as _random
from typing import List, Optional

DEFAULT_POOL = [
    "👍", "❤", "🔥", "🥰", "👏", "😁", "🎉", "🤩", "💯", "😍",
    "🤣", "⚡", "🏆", "👌", "😎", "🫡", "💋", "🤝", "🙏", "😘",
]

GLOBAL_KEY = "*"
WHO_VALUES = ("all", "me", "others", "users")
MODES = ("random", "same")
MIN_GAP = 0.35  # Telegram-friendly floor between reactions in one chat


def defaults() -> dict:
    return {
        "on": False, "mode": "random", "emoji": "🔥", "pool": [], "who": "all",
        "users": {}, "ignore": [], "chance": 100, "delay": 0.0,
        "bots": True, "multi": 1,
    }


def norm_emoji(e: str) -> str:
    """Compare emoji ignoring the variation selector (❤ vs ❤️)."""
    return str(e or "").replace("\ufe0f", "").strip()


def emoji_variants(e: str) -> List[str]:
    """Spellings to try: as typed, without FE0F, with FE0F."""
    e = str(e or "").strip()
    out = []
    for v in (e, e.replace("\ufe0f", ""), e.replace("\ufe0f", "") + "\ufe0f"):
        if v and v not in out:
            out.append(v)
    return out


def is_emoji_token(tok: str) -> bool:
    """Loose check: non-ASCII, short, no letters/digits."""
    tok = str(tok or "").strip()
    if not tok or len(tok) > 16 or tok.isascii():
        return False
    return not any(ch.isalnum() and ch.isascii() for ch in tok)


def extract_emojis(text: str) -> List[str]:
    """Emoji tokens from free text; handles `🔥❤️😂` written without spaces."""
    out: List[str] = []
    for tok in str(text or "").split():
        if is_emoji_token(tok):
            # Split glued emoji on ZWJ-less boundaries conservatively: keep
            # the token whole if it is a single sequence, else split by char
            # (skipping modifiers) so "🔥❤️" becomes two reactions.
            parts, cur = [], ""
            for ch in tok:
                if ch in ("\ufe0f", "\u200d") or 0x1F3FB <= ord(ch) <= 0x1F3FF:
                    cur += ch
                    continue
                if cur and not cur.endswith("\u200d"):
                    parts.append(cur)
                    cur = ""
                cur += ch
            if cur:
                parts.append(cur)
            out.extend(parts)
    seen, uniq = set(), []
    for e in out:
        k = norm_emoji(e)
        if k and k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq


# ── storage ─────────────────────────────────────────────────────────────────
def _root(cfg: dict, me_id) -> dict:
    ar = cfg.setdefault("AUTO_REACT", {})
    if not isinstance(ar, dict):
        ar = cfg["AUTO_REACT"] = {}
    r = ar.setdefault(str(me_id), {})
    if not isinstance(r, dict):
        r = ar[str(me_id)] = {}
    return r


def scope_key(chat_id, global_scope: bool) -> str:
    return GLOBAL_KEY if global_scope else str(chat_id)


def edit(cfg: dict, me_id, chat_id, global_scope: bool = False) -> dict:
    """Mutable settings dict for a scope (created with defaults when new)."""
    r = _root(cfg, me_id)
    k = scope_key(chat_id, global_scope)
    cur = r.get(k)
    if not isinstance(cur, dict):
        cur = defaults()
        r[k] = cur
    for dk, dv in defaults().items():
        cur.setdefault(dk, dv if not isinstance(dv, (dict, list)) else type(dv)())
    return cur


def resolve(cfg: dict, me_id, chat_id) -> Optional[dict]:
    """Active settings for a chat: the chat entry wins over the global one."""
    r = (cfg.get("AUTO_REACT") or {}).get(str(me_id)) or {}
    for k in (str(chat_id), GLOBAL_KEY):
        s = r.get(k)
        if isinstance(s, dict):
            return s
    return None


# ── decision ────────────────────────────────────────────────────────────────
def decide(settings: Optional[dict], sender_id, is_me: bool, is_bot: bool = False,
           rng=None, allowed: Optional[set] = None) -> Optional[List[str]]:
    """Ordered emoji candidates to try for one message, or None to skip.

    `allowed` = normalised emoji the chat accepts (None = unknown/all).
    """
    if not settings or not settings.get("on"):
        return None
    rng = rng or _random
    try:
        sid = int(sender_id) if sender_id is not None else None
    except (TypeError, ValueError):
        sid = None
    ignore = {str(x) for x in settings.get("ignore", [])}
    if sid is not None and str(sid) in ignore:
        return None
    if is_bot and not settings.get("bots", True):
        return None
    users = settings.get("users") or {}
    listed = sid is not None and str(sid) in {str(k) for k in users}
    who = settings.get("who", "all")
    if who == "me" and not is_me:
        return None
    if who == "others" and is_me:
        return None
    if who == "users" and not listed:
        return None
    try:
        chance = max(1, min(100, int(settings.get("chance", 100))))
    except (TypeError, ValueError):
        chance = 100
    if chance < 100 and rng.random() * 100 >= chance:
        return None

    def keep(seq):
        if allowed is None:
            return seq
        return [e for e in seq if norm_emoji(e) in allowed]

    cands: List[str] = []
    if listed:
        for k, v in users.items():
            if str(k) == str(sid) and v:
                cands.append(v)
    if settings.get("mode") == "same" and settings.get("emoji"):
        cands.append(settings["emoji"])
    else:
        pool = list(settings.get("pool") or DEFAULT_POOL)
        rng.shuffle(pool)
        cands.extend(pool)
    cands = keep(cands)
    if not cands and allowed:
        # Nothing of ours is allowed here: fall back to any allowed reaction
        # for random mode only (a fixed `same` emoji must never be swapped).
        if settings.get("mode") != "same":
            pool = sorted(allowed)
            rng.shuffle(pool)
            cands = pool
    seen, out = set(), []
    for e in cands:
        k = norm_emoji(e)
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out[:6] or None


def pick_many(cands: List[str], n: int) -> List[str]:
    """First n distinct candidates (multi-reaction)."""
    return list(cands[:max(1, min(3, int(n or 1)))])


def describe(settings: Optional[dict], scope: str) -> str:
    if not settings:
        return f"{scope}: <b>OFF</b> (not configured)"
    pool = settings.get("pool") or []
    return (
        f"{scope}: <b>{'ON' if settings.get('on') else 'OFF'}</b>\n"
        f"  mode: <code>{settings.get('mode')}</code>"
        + (f" {settings.get('emoji')}" if settings.get("mode") == "same" else "")
        + f"\n  who: <code>{settings.get('who')}</code>"
        f"  chance: <code>{settings.get('chance')}%</code>"
        f"  delay: <code>{settings.get('delay')}s</code>"
        f"  multi: <code>{settings.get('multi')}</code>\n"
        f"  pool: {' '.join(pool) if pool else '<i>default</i>'}\n"
        f"  users: <code>{len(settings.get('users') or {})}</code>"
        f"  ignored: <code>{len(settings.get('ignore') or [])}</code>"
        f"  bots: <code>{'yes' if settings.get('bots', True) else 'no'}</code>"
    )
