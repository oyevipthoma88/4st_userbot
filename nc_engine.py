"""NC (name-change) engine — pure logic for .raidnc/.frostnc/... commands.

An NC run renames the current group <n> times, cycling through a themed
frame list, then restores the original title.  Frames are decorative titles
(optionally carrying the replied user's name via {name}).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

MAX_TITLE = 128
MAX_COUNT = 500
DEFAULT_COUNT = 10
MIN_DELAY = 1.0
DEFAULT_DELAY = 2.0

# command -> (theme key, label)
THEMES: Dict[str, str] = {
    "raid": "RAID",
    "frost": "FROST",
    "apex": "APEX",
    "void": "VOID",
    "s": "SNC",
    "shutup": "SHUTUP",
    "doggy": "DOGGY",
}

_FRAMES: Dict[str, List[str]] = {
    "raid": [
        "⚔️ RAID MODE ⚔️", "💥 RAID ON 💥", "🔥 RAID ACTIVE 🔥", "☠️ RAID ZONE ☠️",
        "⚡ RAID LIVE ⚡", "🩸 RAID STORM 🩸", "🚨 RAID ALERT 🚨", "👑 RAID KING 👑",
    ],
    "frost": [
        "❄️ FROST ❄️", "🧊 FROZEN 🧊", "☃️ FROST BITE ☃️", "🥶 ICE AGE 🥶",
        "❄️ WINTER ❄️", "🌨️ BLIZZARD 🌨️", "💎 FROST CORE 💎", "🧊 DEEP FREEZE 🧊",
    ],
    "apex": [
        "👑 APEX 👑", "🔱 APEX LORD 🔱", "⚡ APEX POWER ⚡", "🏔️ APEX PEAK 🏔️",
        "🦅 APEX HUNTER 🦅", "💎 APEX CORE 💎", "🔥 APEX RISE 🔥", "🐺 APEX PREDATOR 🐺",
    ],
    "void": [
        "🌑 VOID 🌑", "🕳️ THE VOID 🕳️", "🌌 VOID WALK 🌌", "⚫ NOTHING ⚫",
        "👁️ VOID EYE 👁️", "🌀 VOID RIFT 🌀", "🖤 DARK VOID 🖤", "🌑 ABYSS 🌑",
    ],
    "s": [
        "⚡ SNC ⚡", "🔥 SNC 🔥", "💫 SNC 💫", "✨ SNC ✨", "💥 SNC 💥", "🌟 SNC 🌟",
    ],
    "shutup": [
        "🤐 SHUT UP 🤐", "🔇 SILENCE 🔇", "🤫 SHHH 🤫", "🚫 QUIET 🚫",
        "🙊 NO TALK 🙊", "🤐 MUTE ZONE 🤐", "🔕 HUSH 🔕", "🤫 PEACE 🤫",
    ],
    "doggy": [
        "🐶 DOGGY 🐶", "🐕 DOGGY MODE 🐕", "🦮 WOOF 🦮", "🐾 DOGGY ZONE 🐾",
        "🐩 BARK 🐩", "🐕‍🦺 GOOD BOY 🐕‍🦺", "🦴 BONE TIME 🦴", "🐶 PUPPY 🐶",
    ],
}

# `.raidnc 20` / `.snc 5` / `.shutupnc` ...
CMD_RE = re.compile(
    r"(?is)^\.(" + "|".join(sorted(THEMES, key=len, reverse=True)) + r")nc(?:\s+(.*))?$")
# `.stopnc`, `.stopraidnc`, `.stopsnc`, `.stopall`, `.stop`
STOP_RE = re.compile(r"(?i)^\.(?:stop|stopm|stopall|stopnc|stop\w*nc)$")


def parse(text: str):
    """(theme, count, custom_title) from a command, or None when not NC.

    `.raidnc 20`            -> ('raid', 20, None)
    `.raidnc 20 My Title`   -> custom base title replaces the frames
    `.raidnc`               -> default count
    Count is clamped to 1..MAX_COUNT.
    """
    m = CMD_RE.match((text or "").strip())
    if not m:
        return None
    theme, rest = m.group(1).lower(), (m.group(2) or "").strip()
    count, custom = DEFAULT_COUNT, None
    if rest:
        head, _, tail = rest.partition(" ")
        if re.fullmatch(r"\d+", head):
            count = int(head)
            custom = tail.strip() or None
        else:
            custom = rest
    count = max(1, min(MAX_COUNT, count))
    return theme, count, custom


def build_titles(theme: str, count: int, name: Optional[str] = None,
                 custom: Optional[str] = None) -> List[str]:
    """Exactly `count` titles; no two consecutive titles are equal.

    Telegram rejects a rename to the same title, so identical neighbours are
    avoided by rotating frames and, if a theme is shorter than 2 frames or a
    custom title repeats, by appending a rolling number.
    """
    frames = _FRAMES.get(theme) or _FRAMES["raid"]
    titles: List[str] = []
    for i in range(count):
        if custom:
            base = custom
            title = f"{base} {i + 1}" if count > 1 else base
        else:
            base = frames[i % len(frames)]
            if name:
                base = f"{base} {name}"
            title = base
            if i >= len(frames) and count > len(frames):
                title = f"{base} #{i // len(frames) + 1}"
        title = title[:MAX_TITLE].rstrip()
        if titles and title == titles[-1]:
            title = (title[: MAX_TITLE - 2] + " ·").rstrip()
        titles.append(title)
    return titles
