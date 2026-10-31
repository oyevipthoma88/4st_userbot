# ══════════════════════════════════════════════════════════════════
# PLUGIN: Stranger Animations (ported from github.com/itzshukla)
# ══════════════════════════════════════════════════════════════════
# Loads the animation modules of STRANGER-USERBOT2.0 / STRANGER-OPUSERBOT2.0
# (GPL-3.0, vendored under plugins/_stranger_anims/) through a small
# Pyrogram -> Telethon shim, so every original animation works unchanged.
#
# Branding: the original author's name / username / links are replaced with
# the name (and @username) of the account that RUNS the command, so every
# logged-in user sees their own name inside the animations.
#
# File name starts with "aaa_" so it loads before the other animation plugins
# and can tell them which commands it owns (see COMMANDS).
import asyncio
import os
import re
import warnings

from telethon import events, errors

PLUGIN_NAME = "Stranger Animations"
PLUGIN_VERSION = "1.0.0"
PLUGIN_CMDS = [".animlist"]

_DIR = os.path.join(os.path.dirname(__file__), "_stranger_anims")
_SOURCES = [  # later files override earlier ones for the same command
    "zaid_animation.py.txt",
    "shukla_animation.py.txt",
]
_EMOJI_SOURCE = "shukla_emoji.py.txt"

# command -> async callable(PMsg).  Filled once at import time.
HANDLERS: dict = {}
COMMANDS: set = set()

# ───────────────────────── branding ─────────────────────────
_SMALLCAP_STRANGER = "sᴛʀᴀɴɢᴇʀ"
_SMALLCAP_SHIV = "sʜɪᴠᴀɴsʜ"
_TOKENS = [
    _SMALLCAP_STRANGER, _SMALLCAP_SHIV, "STRANGER", "Stranger", "stranger",
    "SHIVANSH474", "SHIVANSH", "Shivansh", "shivansh",
]


def _clean_name(name: str) -> str:
    name = re.sub(r"[`*_\[\]()]", "", name or "").strip()
    return name or "User"


def brand(text, name: str, username: str, user_id: int):
    """Swap the original author's identity for the running account's."""
    if not isinstance(text, str):
        return text
    name = _clean_name(name)
    tag = name.replace(" ", "_")
    link = f"https://t.me/{username}" if username else f"tg://user?id={user_id}"
    handle = f"@{username}" if username else name
    text = re.sub(r"https?://t\.me/SHIVANSH474", link, text, flags=re.I)
    text = re.sub(r"@SHIVANSH474", handle, text, flags=re.I)
    for tok in (_SMALLCAP_STRANGER, "STRANGER", "Stranger", "stranger"):
        text = text.replace("#_" + tok, "#_" + tag)
    for tok in _TOKENS:
        text = text.replace(tok, name)
    return text


# ───────────────────── Pyrogram → Telethon shim ─────────────────────
class _Filter:
    def __init__(self, commands=(), kind="x"):
        self.commands = list(commands)
        self.kind = kind

    def __and__(self, other):
        return _Filter(self.commands + other.commands)

    def __or__(self, other):
        return _Filter(self.commands + other.commands)


class _Filters:
    me = _Filter(kind="me")

    @staticmethod
    def command(names, prefix="."):
        if isinstance(names, str):
            names = [names]
        return _Filter([n.lower() for n in names], "command")

    @staticmethod
    def regex(*a, **k):
        return _Filter(kind="regex")

    @staticmethod
    def user(*a, **k):
        return _Filter(kind="user")


filters = _Filters()


class _Registry:
    """Stand-in for both `app` and `Client` (decorator use only)."""

    @staticmethod
    def on_message(flt=None, *a, **k):
        def deco(fn):
            cmds = getattr(flt, "commands", [])
            for c in cmds:
                HANDLERS[c] = fn
            return fn
        return deco


class FloodWait(Exception):
    x = 1


class _Chat:
    def __init__(self, cid):
        self.id = cid


class _User:
    def __init__(self, uid, is_self=True):
        self.id = uid
        self.is_self = is_self


class _Reply:
    def __init__(self, text, uid):
        self.text = text
        self.from_user = _User(uid, False)


class PMsg:
    """Minimal Pyrogram-like Message over a Telethon event."""

    def __init__(self, event, me, reply_text=None, reply_uid=0):
        self._ev = event
        self._me = me
        self.text = event.raw_text or ""
        self.chat = _Chat(event.chat_id)
        self.from_user = _User(me.id, True)
        self.outgoing = True
        self.entities = None
        self.forward_from = getattr(event.message, "fwd_from", None)
        self.command = self.text.lstrip(".").split()
        self.reply_to_message = _Reply(reply_text, reply_uid) if reply_text is not None else None

    def _b(self, text):
        return brand(text, getattr(self._me, "first_name", "") or "", getattr(self._me, "username", "") or "", self._me.id)

    async def edit(self, text="", **kw):
        text = self._b(text)
        for _ in range(2):
            try:
                await self._ev.edit(text, parse_mode="md", link_preview=False)
                break
            except errors.MessageNotModifiedError:
                break
            except errors.FloodWaitError as fw:
                await asyncio.sleep(min(fw.seconds, 30))
        return self

    edit_text = edit
    reply_text = edit

    async def delete(self, *a, **k):
        try:
            await self._ev.delete()
        except Exception:
            pass


class _ClientShim:
    def __init__(self, client):
        self._c = client

    async def send_video(self, chat_id, url, *a, **k):
        return await self._c.send_file(chat_id, url)

    async def send_photo(self, chat_id, url, *a, **k):
        return await self._c.send_file(chat_id, url)

    async def send_message(self, chat_id, text, *a, **k):
        return await self._c.send_message(chat_id, text, parse_mode="md")


async def _edit_or_reply(message, text, *a, **k):
    await message.edit(text)
    return message


def _get_text(message):
    t = message.text
    if t and " " in t:
        parts = t.split(None, 1)
        return parts[1] if len(parts) > 1 else None
    return None


class _Requests:
    """requests with a hard timeout so a dead GIF API cannot hang the loop."""

    def __getattr__(self, name):
        import requests as _r
        fn = getattr(_r, name)
        if name in ("get", "post"):
            def wrapped(*a, **k):
                k.setdefault("timeout", 8)
                return fn(*a, **k)
            return wrapped
        return fn


def _read(name):
    with open(os.path.join(_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


_IMPORT_RE = re.compile(
    r"^(from\s+(\.\.\.|pyrogram|Zaid|SHUKLA)[\w\.\s]*\s+import\s+.*|import\s+requests)\s*$", re.M)


def _exec_source(name, extra=None):
    src = _read(name)
    src = _IMPORT_RE.sub("", src)
    src = src.replace("await ok.edit(", "await op.edit(")      # upstream NameError
    src = src.replace("return await ok.edit(", "return await op.edit(")
    ns = {
        "__name__": "stranger_anim_" + name.split(".")[0],
        "Client": _Registry, "app": _Registry, "filters": filters,
        "Message": PMsg, "FloodWait": FloodWait,
        "edit_or_reply": _edit_or_reply, "eor": _edit_or_reply,
        "get_text": _get_text, "requests": _Requests(),
        "add_command_help": lambda *a, **k: None,
        "cdz": filters.command, "SUDO_USER": [],
        "MEMES": _MEMES,
    }
    ns.update(extra or {})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        exec(compile(src, name, "exec"), ns)
    return ns


def _load_memes():
    ns = {}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            exec(compile(_read("constants.py.txt"), "constants", "exec"), ns)
        memes = ns["MEMES"]
        for _attr, _val in (("GAMBAR_KONTOL", "8✊===D"), ("GAMBAR_TITIT", "( . )( . )")):
            if not hasattr(memes, _attr):
                setattr(memes, _attr, _val)
        return memes
    except Exception:
        class _Empty:
            GAMBAR_KONTOL = "8==D"
            GAMBAR_TITIT = "( . )( . )"
        return _Empty


_MEMES = _load_memes()
_LOAD_ERRORS: list = []

for _src in _SOURCES:
    try:
        _exec_source(_src)
    except Exception as _e:  # keep going with whatever loaded
        _LOAD_ERRORS.append(f"{_src}: {_e!r}")

_ANIM_LOVE = HANDLERS.get("love")
try:
    _before = dict(HANDLERS)
    _exec_source(_EMOJI_SOURCE)
    _EMOJI_LOVE = HANDLERS.get("love")
    HANDLERS["cmoji"] = _EMOJI_LOVE
    # `.love` (no text) keeps the heart animation; `.love <text>` emojifies.
    if _ANIM_LOVE and _EMOJI_LOVE:
        async def _love_router(message, _a=_ANIM_LOVE, _e=_EMOJI_LOVE):
            return await (_e if _get_text(message) else _a)(None, message)
        HANDLERS["love"] = None
        _LOVE_ROUTER = _love_router
    else:
        _LOVE_ROUTER = None
except Exception as _e:
    _LOAD_ERRORS.append(f"{_EMOJI_SOURCE}: {_e!r}")
    _LOVE_ROUTER = None

COMMANDS = {c for c in HANDLERS} | {"animlist"}


# ───────────────────────── Telethon wiring ─────────────────────────
def setup(client, cfg=None, bot_logger=None, **kwargs):
    log = bot_logger or (lambda tag, msg: print(f"[{tag}] {msg}"))
    shim = _ClientShim(client)
    state = {"me": None}

    async def _me():
        if state["me"] is None:
            state["me"] = await client.get_me()
        return state["me"]

    names = "|".join(sorted((re.escape(c) for c in COMMANDS), key=len, reverse=True))
    pattern = rf"(?i)^\.({names})(?:\s+([\s\S]*))?$"

    @client.on(events.NewMessage(outgoing=True, pattern=pattern))
    async def _dispatch(event):
        cmd = event.pattern_match.group(1).lower()
        me = await _me()
        if cmd == "animlist":
            shown = ", ".join(f".{c}" for c in sorted(COMMANDS))
            nm = _clean_name(getattr(me, "first_name", ""))
            try:
                await event.edit(
                    f"🎭 **{nm}'s Animations ({len(COMMANDS)}):**\n\n{shown}", parse_mode="md")
            except Exception:
                pass
            return
        reply_text, reply_uid = None, 0
        try:
            if event.is_reply:
                r = await event.get_reply_message()
                reply_text, reply_uid = (r.raw_text or ""), (r.sender_id or 0)
        except Exception:
            pass
        pm = PMsg(event, me, reply_text, reply_uid)
        handler = HANDLERS.get(cmd)
        try:
            if cmd == "love" and _LOVE_ROUTER:
                await _LOVE_ROUTER(pm)
            elif handler:
                await handler(shim, pm)
        except Exception as e:
            log("ANIM_ERR", f".{cmd}: {e!r}")

    for err in _LOAD_ERRORS:
        log("PLUGIN_WARN", f"{PLUGIN_NAME}: {err}")
    log("PLUGIN", f"{PLUGIN_NAME} loaded — {len(COMMANDS)} commands (branded per account)")
