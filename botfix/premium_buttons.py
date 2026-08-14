"""Premium inline-button factory — shared across all bots.

Copied from the Melody_music reference (`utils/buttons.py` + `utils/inline.py`
+ `utils/emoji_map.py`) so every bot in this account builds inline buttons the
same way:

  * a real premium (animated) custom-emoji icon on the button
    (`icon_custom_emoji_id=`) resolved from the glyph in the label
  * coloured button styles (`style=` -> PRIMARY / SUCCESS / DANGER)
  * exactly ONE emoji per button: when the premium icon is attached the plain
    unicode copy is stripped out of the label, so a supported client never
    shows the icon twice

NEVER CRASHES
-------------
Neither Pyrogram nor Telethon accepts `style=` / `icon_custom_emoji_id=` on
every version/fork, so the installed constructor signature is inspected once
at import time and any unsupported keyword is silently dropped. Buttons render
premium + coloured where the running library supports it, and as ordinary
buttons everywhere else.

USAGE
-----
Pyrogram::

    from premium_buttons import ikb as InlineKeyboardButton

Telethon::

    from premium_buttons import Button      # instead of `from telethon import Button`

Every existing call site keeps working unchanged.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import re
from typing import Any, Optional

# ── glyph -> real premium custom-emoji id (reference pack) ────────────────
EMOJI_ID_MAP: dict = {
    "\u274c": "6276044051024189481",
    "\u2705": "6273947849285704577",
    "\U0001f4cb": "6176895538449880631",
    "\u25b6": "6197420598746945336",
    "\u26a0": "6287305455274301148",
    "\U0001f3b6": "6127406790666623284",
    "\U0001f3b5": "6199293238847740460",
    "\u26a1": "6294202146254493635",
    "\U0001f501": "6253379044746731429",
    "\U0001f916": "6271317151752133080",
    "\U0001f4ca": "6201763545122609648",
    "\u23f9": "6201638037588285856",
    "\U0001f5bc": "6210578145158895902",
    "\u23f8": "6172610144635983353",
    "\u23ed": "6168060795016976899",
    "\U0001f451": "6208343392070409520",
    "\U0001f4d6": "6197426719075342898",
    "\U0001f4e2": "6201920766695445331",
    "\U0001f534": "6203887329141068076",
    "\U0001f4e1": "6203899763071390887",
    "\U0001f504": "6203908026588468321",
    "\U0001f50a": "6203912944326022034",
    "\u23ea": "6204052023957000629",
    "\U0001f50d": "6204096219170478363",
    "\u2139": "6204218161881944539",
    "\U0001f527": "5819027790921470358",
    "\U0001f5d1": "5857492186584585341",
    "\U0001f4c3": "5819194817904644827",
    "\U0001f3d3": "6199693070238227698",
    "\U0001f64b": "5818699805743911235",
    "\U0001f525": "5818792267799860284",
    "\U0001f535": "5859413634693730666",
    "\u23f1": "6131862213645832542",
    "\u2795": "6129932579329021278",
    "\U0001f507": "6131724740332623282",
    "\u23e9": "6131841924220333142",
    "\U0001f6ab": "6131785123277838173",
    "\U0001f7e2": "6334598469746952256",
    "\U0001f389": "5353006311543945010",
    "\U0001f502": "5353064555595447861",
    "\U0001f500": "5285338659413846416",
    "\U0001f310": "5260567255145539253",
    "\u23f3": "5276352986535194063",
    "\U0001f338": "5258500400918587241",
    "\U0001f464": "6161123246012896003",
    "\U0001f3a4": "6244266124871996378",
    "\U0001f3ac": "6057763354796103071",
    "\U0001f4e6": "6185832600888677406",
    "\U0001f680": "6188280616283279847",
    "\U0001f39b": "6161003777202590184",
    "\U0001f4bb": "6217504643212120834",
    "\U0001f528": "6190280451840544341",
    "\U0001f40d": "6226435550962782605",
    "\U0001f36a": "5287532554478438337",
    "\U0001f480": "5287400531478731028",
    "\U0001f552": "5287725119337156381",
    "\U0001f4c8": "5287384326567117767",
    "\U0001f3e0": "5375256333786314730",
    "\u2699": "5264919362686461875",
    "\U0001f4f9": "5264749389355721331",
    "\U0001f50c": "5265175733579325008",
    "\U0001f511": "5375483949873130451",
    "\U0001f4de": "5375402113566277518",
    "\u2601": "5334579080777974551",
    "\U0001f9f9": "5334665744628075397",
    "\U0001f9fe": "6066392644173439803",
    "\U0001f7e3": "6066848773995241206",
    "\U0001f508": "6066562059158429517",
    "\U0001f3a8": "6066776592774866103",
    "\u2728": "6066689331924311243",
    "\U0001f49b": "5001650241242400445",
    "\U0001f64f": "5003707856994699035",
    "\U0001f465": "5001675727578334656",
    "\u25c0": "5001463710812735147",
    "\U0001f3f7": "5001522169612600902",
    "\U0001fae1": "6255572871091849620",
    "\U0001f44b": "6255793039705377676",
    "\u23f0": "6197058614608270294",
    "\u2192": "6167958321392262626",
    "\u265b": "6127656684748806757",
    "\u2735": "6125239923831217642",
    "\u27a1": "6127546175240280564",
    "\U0001f3a7": "6125150373763094821",
}

# Extra owner ids (playing card / brand pack) used to decorate any glyph that
# is not in the map above.
EMOJI_POOL: list = [
    "5258389041006518073", "5980953710157632545", "5818753711878442644",
    "5980787993139481991", "6179327439127188875", "5891184096192763888",
    "5467887731705136739", "5911274703367968100", "5979016967669944073",
    "6291574588342016102", "5368509223632118184",
]


def _env_pool() -> list:
    raw = os.getenv("PREMIUM_EMOJI_IDS") or os.getenv("EMOJI_IDS") or ""
    return [p for p in re.split(r"[,\s]+", raw) if p.strip().isdigit()]


EMOJI_POOL = _env_pool() or EMOJI_POOL


# ── plain-unicode glyph removal ───────────────────────────────────────────
_GLYPH_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\u2190-\u21FF"
    "\u2139\u24C2\u203C\u2049\u2122"
    "\u3030\u303D\u3297\u3299"
    "\u2300-\u27BF"
    "\u2B00-\u2BFF"
    "\u2600-\u26FF"
    "\uFE0F\uFE0E\u200D\u20E3"
    "]+"
)

def assign_pool_id(glyph: str) -> Optional[str]:
    """Stable premium id for a glyph that has no dedicated mapping."""
    if not EMOJI_POOL:
        return None
    cached = EMOJI_ID_MAP.get(glyph)
    if cached:
        return cached
    digest = hashlib.md5(glyph.encode("utf-8", "ignore")).hexdigest()
    chosen = EMOJI_POOL[int(digest, 16) % len(EMOJI_POOL)]
    EMOJI_ID_MAP[glyph] = chosen
    return chosen


def icon_id_for(label: Any) -> Optional[str]:
    """Premium custom-emoji id for the first emoji glyph found in `label`."""
    if not label or not isinstance(label, str):
        return None
    for ch in label:
        if ch.isspace() or ch.isalnum():
            continue
        # Only real emoji/pictograph glyphs get a premium icon — plain
        # punctuation ("-", "|", "\u25b7") must never pull a random id.
        if ch not in EMOJI_ID_MAP and not _GLYPH_RE.fullmatch(ch):
            continue
        emoji_id = (
            EMOJI_ID_MAP.get(ch)
            or EMOJI_ID_MAP.get(ch + "\ufe0f")
            or EMOJI_ID_MAP.get(ch.rstrip("\ufe0f"))
            # Pool fallback only for true pictographic emoji, so text-art
            # labels ("\u25b7", "II", "\u2023\u2023I") stay exactly as written.
            or (assign_pool_id(ch) if ord(ch) >= 0x1F000 else None)
        )
        if emoji_id:
            return str(emoji_id)
    return None




def strip_glyphs(text: str) -> str:
    """Label text with plain unicode emoji/pictographs removed."""
    cleaned = _GLYPH_RE.sub(" ", text or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


# ── button colour styles ──────────────────────────────────────────────────
class _StyleFallback(str):
    """Stand-in for a `ButtonStyle` enum on builds that do not ship one."""


class _Styles:
    DEFAULT = _StyleFallback("default")
    PRIMARY = _StyleFallback("primary")
    SUCCESS = _StyleFallback("success")
    DANGER = _StyleFallback("danger")
    WARNING = _StyleFallback("warning")
    SECONDARY = _StyleFallback("secondary")


def _load_styles():
    for mod, name in (
        ("pyrogram.enums", "ButtonStyle"),
        ("telethon.tl.types", "ButtonStyle"),
        ("telethon.enums", "ButtonStyle"),
    ):
        try:
            return getattr(__import__(mod, fromlist=[name]), name)
        except Exception:
            continue
    return _Styles


ButtonStyle = _load_styles()

STYLE_DEFAULT = getattr(ButtonStyle, "DEFAULT", None)
STYLE_PRIMARY = getattr(ButtonStyle, "PRIMARY", STYLE_DEFAULT)
STYLE_SUCCESS = getattr(ButtonStyle, "SUCCESS", STYLE_DEFAULT)
STYLE_DANGER = getattr(ButtonStyle, "DANGER", STYLE_DEFAULT)
STYLE_WARNING = getattr(ButtonStyle, "WARNING", STYLE_DEFAULT)

_DANGER_WORDS = (
    "close", "stop", "cancel", "delete", "remove", "ban", "back", "off",
    "end", "exit", "\u2716", "\u274c", "\u23f9", "\U0001f5d1",
)
_SUCCESS_WORDS = (
    "play", "resume", "start", "add", " on", "approve", "enable", "yes",
    "save", "done", "\u25b6", "\u2705", "\U0001f7e2",
)


def infer_style(text: Any) -> Any:
    """Pick a sensible button colour from the label when none was given."""
    low = (text or "").lower() if isinstance(text, str) else ""
    if any(w in low for w in _DANGER_WORDS):
        return STYLE_DANGER
    if any(w in low for w in _SUCCESS_WORDS):
        return STYLE_SUCCESS
    return STYLE_PRIMARY


def _params(func: Any) -> set:
    try:
        return set(inspect.signature(func).parameters)
    except (TypeError, ValueError):  # pragma: no cover - exotic builds
        return set()


def _decorate(text: Any, kwargs: dict, supported: set) -> Any:
    """Attach style/icon when supported and de-duplicate the glyph."""
    if not isinstance(text, str):
        return text
    if "style" in supported:
        if kwargs.get("style") is None:
            chosen = infer_style(text)
            if chosen is not None:
                kwargs["style"] = chosen
    else:
        kwargs.pop("style", None)

    # Pyrogram forks take `icon_custom_emoji_id=<str>`; Telethon >= 1.44 takes
    # `icon=<int>` inside its KeyboardButtonStyle. Use whichever exists.
    icon_key = (
        "icon_custom_emoji_id" if "icon_custom_emoji_id" in supported
        else ("icon" if "icon" in supported else None)
    )
    emoji_id = kwargs.get("icon_custom_emoji_id") or kwargs.get("icon")
    kwargs.pop("icon_custom_emoji_id", None)
    kwargs.pop("icon", None)
    if icon_key:
        emoji_id = emoji_id or icon_id_for(text)
        if emoji_id:
            try:
                kwargs[icon_key] = (
                    int(emoji_id) if icon_key == "icon" else str(emoji_id)
                )
            except (TypeError, ValueError):
                kwargs.pop(icon_key, None)
            else:
                # The premium icon now carries the emoji — drop the duplicate
                # plain unicode copy, but never leave the label empty.
                cleaned = strip_glyphs(text)
                if cleaned:
                    text = cleaned
    return text


# ── Pyrogram / python-telegram-bot: drop-in InlineKeyboardButton ──────────
def _load_button_class():
    """Whichever inline-button class this bot's library provides."""
    for mod, name in (
        ("pyrogram.types", "InlineKeyboardButton"),
        ("telegram", "InlineKeyboardButton"),
    ):
        try:
            return getattr(__import__(mod, fromlist=[name]), name)
        except Exception:
            continue
    return None


_PyroButton = _load_button_class()

_PYRO_PARAMS = _params(getattr(_PyroButton, "__init__", None)) if _PyroButton else set()
SUPPORTS_ICON: bool = bool({"icon_custom_emoji_id", "icon"} & _PYRO_PARAMS)
SUPPORTS_STYLE: bool = "style" in _PYRO_PARAMS


def ikb(text: Any = "", *args: Any, style: Any = None, icon: Optional[str] = None,
        auto_icon: bool = True, **kwargs: Any):
    """`InlineKeyboardButton` drop-in with premium icon + colour style."""
    if _PyroButton is None:  # pragma: no cover
        raise ImportError("no Telegram library (pyrogram/python-telegram-bot) installed")
    if style is not None:
        kwargs["style"] = style
    if icon is not None:
        kwargs["icon_custom_emoji_id"] = icon
    if not auto_icon:
        kwargs.setdefault("icon_custom_emoji_id", None)
        if kwargs["icon_custom_emoji_id"] is None:
            kwargs.pop("icon_custom_emoji_id")
            text = _decorate(text, kwargs, _PYRO_PARAMS - {"icon_custom_emoji_id"})
            return _safe(_PyroButton, text, *args, **kwargs)
    text = _decorate(text, kwargs, _PYRO_PARAMS)
    return _safe(_PyroButton, text, *args, **kwargs)


def _safe(factory: Any, text: Any, *args: Any, **kwargs: Any):
    try:
        return factory(text, *args, **kwargs)
    except TypeError:
        kwargs.pop("style", None)
        kwargs.pop("icon_custom_emoji_id", None)
        return factory(text, *args, **kwargs)


InlineKeyboardButton = ikb


# ── Telethon: drop-in Button ──────────────────────────────────────────────
try:  # pragma: no cover
    from telethon import Button as _RawButton
except Exception:
    _RawButton = None

_INLINE_PARAMS = _params(getattr(_RawButton, "inline", None)) if _RawButton else set()
_URL_PARAMS = _params(getattr(_RawButton, "url", None)) if _RawButton else set()


class Button:
    """Drop-in replacement for `telethon.Button` with premium buttons."""

    @staticmethod
    def inline(text: Any = "", data: Any = None, **kwargs: Any):
        text = _decorate(text, kwargs, _INLINE_PARAMS)
        return _safe(_RawButton.inline, text, data, **kwargs)

    @staticmethod
    def url(text: Any = "", url: Any = None, **kwargs: Any):
        text = _decorate(text, kwargs, _URL_PARAMS)
        return _safe(_RawButton.url, text, url, **kwargs)

    def __getattr__(self, item):  # pragma: no cover - instance access
        return getattr(_RawButton, item)


if _RawButton is not None:
    for _name in dir(_RawButton):
        if not _name.startswith("_") and not hasattr(Button, _name):
            setattr(Button, _name, getattr(_RawButton, _name))


__all__ = [
    "Button",
    "ButtonStyle",
    "EMOJI_ID_MAP",
    "InlineKeyboardButton",
    "STYLE_DANGER",
    "STYLE_DEFAULT",
    "STYLE_PRIMARY",
    "STYLE_SUCCESS",
    "STYLE_WARNING",
    "SUPPORTS_ICON",
    "SUPPORTS_STYLE",
    "icon_id_for",
    "ikb",
    "infer_style",
    "strip_glyphs",
]
