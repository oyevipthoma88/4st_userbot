"""OW engine — pure helpers for .ow / .nameow / .lineow.

Kept free of Telegram imports so every rule has an executable test.

Commands
--------
.ow        plain ow.txt words, one word per message (no personalisation).
.nameow    (alias .naow) the regular .ow stream, with the saved target name
           dropped in between at phrase starts as ONE unit glued to that
           word: "AJAA", "BETA", "rahul TERI", "MA", ... (see _name_weave)
.lineow    every saved .addowline line is split into words and the words are
           fired one by one, forever, line after line:
           line "tu bhaag mat" -> "tu", "bhaag", "mat", next line ...

Storage (cfg, per core / per chat, "*" = every chat)
    OW_TARGET_WORDS[core][chat] -> [name, ...]   (names rotate if several)
    OW_LINES[core][chat]        -> [line, ...]
"""
from __future__ import annotations

import itertools
import re
from typing import Iterator, List

MAX_NAMES = 20
MAX_LINES = 300
MAX_TEXT = 400

_INVISIBLE_RE = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2060-\u206F\uFEFF]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"[ \t\u00a0]+")

VARIANTS = ("ow", "nameow", "lineow")
GLOBAL_KEY = "*"


def clean_text(value) -> str:
    """Light clean: drop invisible/control chars, collapse spaces.

    Unlike the legacy cleaner this KEEPS emoji, fonts and symbols the user
    typed on purpose — the saved text must come back exactly as written.
    """
    s = str(value or "")
    s = _INVISIBLE_RE.sub("", s)
    s = _CTRL_RE.sub("", s)
    s = _WS_RE.sub(" ", s)
    return s.strip()[:MAX_TEXT]


def split_words(line: str) -> List[str]:
    """Words of one line (whitespace separated, newlines never glue words)."""
    return [w for w in clean_text(str(line).replace("\n", " ")).split(" ") if w]


def parse_lines(arg: str) -> List[str]:
    """Lines of an .addowline argument. One non-empty line each."""
    out = []
    for raw in str(arg or "").replace("\r", "\n").split("\n"):
        c = clean_text(raw)
        if c:
            out.append(c)
    return out


def parse_names(arg: str) -> List[str]:
    """`.owtarget rahul` -> ['rahul'];  `a | b` -> two rotating names.

    A name with spaces ("rahul ki maa") stays ONE unit.
    """
    names = []
    for part in str(arg or "").replace("\n", "|").split("|"):
        c = clean_text(part)
        if c:
            names.append(c)
    return _dedupe(names)[:MAX_NAMES]


def _dedupe(items: List[str]) -> List[str]:
    seen, out = set(), []
    for it in items:
        k = it.casefold()
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


# ── storage helpers ─────────────────────────────────────────────────────────
def _bucket(cfg: dict, key: str, core_id) -> dict:
    root = cfg.setdefault(key, {})
    if not isinstance(root, dict):
        root = cfg[key] = {}
    b = root.setdefault(str(core_id), {})
    if not isinstance(b, dict):
        b = root[str(core_id)] = {}
    return b


def _scope(chat_id, global_scope: bool) -> str:
    return GLOBAL_KEY if global_scope else str(chat_id)


def get_names(cfg: dict, core_id, chat_id) -> List[str]:
    b = (cfg.get("OW_TARGET_WORDS") or {}).get(str(core_id)) or {}
    items = b.get(str(chat_id)) or b.get(GLOBAL_KEY) or []
    return [c for c in (clean_text(x) for x in items) if c]


def get_lines(cfg: dict, core_id, chat_id) -> List[str]:
    b = (cfg.get("OW_LINES") or {}).get(str(core_id)) or {}
    items = b.get(str(chat_id)) or b.get(GLOBAL_KEY) or []
    return [c for c in (clean_text(x) for x in items) if c]


def set_names(cfg, core_id, chat_id, names, global_scope=False) -> List[str]:
    """Replace the target name(s) — `.owtarget` sets, it never piles up."""
    b = _bucket(cfg, "OW_TARGET_WORDS", core_id)
    b[_scope(chat_id, global_scope)] = list(names)[:MAX_NAMES]
    return b[_scope(chat_id, global_scope)]


def clear_names(cfg, core_id, chat_id, global_scope=False) -> bool:
    b = _bucket(cfg, "OW_TARGET_WORDS", core_id)
    return b.pop(_scope(chat_id, global_scope), None) is not None


def add_lines(cfg, core_id, chat_id, lines, global_scope=False):
    """Append lines (case-insensitive de-dupe). Returns (added, total)."""
    b = _bucket(cfg, "OW_LINES", core_id)
    k = _scope(chat_id, global_scope)
    cur = list(b.get(k, []))
    known = {x.casefold() for x in cur}
    added = 0
    for ln in lines:
        if ln.casefold() not in known and len(cur) < MAX_LINES:
            cur.append(ln)
            known.add(ln.casefold())
            added += 1
    b[k] = cur
    return added, len(cur)


def remove_lines(cfg, core_id, chat_id, selector: str, global_scope=False) -> int:
    """Remove by 1-based number(s) ("2", "1 3 4", "2-5"), exact text, or "all".

    Returns how many lines were removed.
    """
    b = _bucket(cfg, "OW_LINES", core_id)
    k = _scope(chat_id, global_scope)
    cur = list(b.get(k, []))
    sel = clean_text(selector)
    if not cur or not sel:
        return 0
    if sel.lower() in ("all", "clear", "reset", "off"):
        b.pop(k, None)
        return len(cur)
    idx = set()
    tokens = sel.split(" ")
    if all(re.fullmatch(r"\d+(-\d+)?", t) for t in tokens):
        for t in tokens:
            if "-" in t:
                a, z = (int(x) for x in t.split("-"))
                idx.update(range(min(a, z) - 1, max(a, z)))
            else:
                idx.add(int(t) - 1)
    else:
        idx = {i for i, x in enumerate(cur) if x.casefold() == sel.casefold()}
    idx = {i for i in idx if 0 <= i < len(cur)}
    if not idx:
        return 0
    b[k] = [x for i, x in enumerate(cur) if i not in idx]
    if not b[k]:
        b.pop(k, None)
    return len(idx)


# ── payload feeds ───────────────────────────────────────────────────────────
def build_feed(variant: str, base_words: List[str], names: List[str],
               lines: List[str]) -> Iterator[str]:
    """Endless iterator of message texts for the chosen OW variant.

    Raises ValueError when the variant has nothing to fire, so callers can
    tell the user BEFORE starting a loop that would do nothing.
    """
    if variant == "lineow":
        words = [w for ln in lines for w in split_words(ln)]
        if not words:
            raise ValueError("no-lines")
        return itertools.cycle(words)
    words = [clean_text(w) for w in base_words if clean_text(w)]
    if not words:
        raise ValueError("no-words")
    if variant == "nameow":
        if not names:
            raise ValueError("no-name")
        return _name_weave(words, names)
    return itertools.cycle(words)


NAME_MIN_GAP = 3   # at least this many plain OW words between two names
NAME_MAX_GAP = 6   # never more than this many plain words without the name
_END_PUNCT = tuple(".!?,;:।|…")


def _is_boundary(prev_word: str) -> bool:
    """A phrase ended right before this spot ("BACCHE." / "chal!")."""
    return bool(prev_word) and prev_word.rstrip().endswith(_END_PUNCT)


def _name_weave(words: List[str], names: List[str]) -> Iterator[str]:
    """Regular OW word stream with the target name dropped in at the right spot.

    * every message is still the normal ow.txt word, in order;
    * the name is ONE unit glued in front of the word that starts a phrase:
      "rahul TERI", never "ra hul" or "TERI rahul MA";
    * a phrase start = start of ow.txt, or right after a word ending in . ! ? , etc.;
    * the name never comes twice within NAME_MIN_GAP words and never waits
      more than NAME_MAX_GAP words (so it keeps appearing in the middle);
    * several names rotate.
    """
    n = len(words)
    gap = NAME_MAX_GAP  # so the very first message carries the name
    ni = 0
    i = 0
    while True:
        w = words[i % n]
        prev = words[(i - 1) % n] if i else ""
        boundary = (i % n == 0) or _is_boundary(prev)
        if gap >= NAME_MAX_GAP or (boundary and gap >= NAME_MIN_GAP):
            yield f"{names[ni % len(names)]} {w}"
            ni += 1
            gap = 0
        else:
            yield w
            gap += 1
        i += 1


def feed_error_text(err: str) -> str:
    return {
        "no-name": "Pehle target ka naam set karo: <code>.owtarget rahul</code>",
        "no-lines": "Pehle line add karo: <code>.addowline tu bhaag mat</code>",
        "no-words": "ow.txt khali hai.",
    }.get(err, "OW start nahi ho paya.")
