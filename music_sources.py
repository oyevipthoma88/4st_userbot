"""
Multi-source music resolver — YouTube + 12 extra no-cookie/login-free sources.

YouTube is supported via a 5-client jugad chain (Android → Embed → TV → mweb → Web)
requiring no cookies and no login for public songs. Plus 12 extra fallback sources
(SoundCloud, Audius, Internet Archive, etc.) that also need no account or cookies.
main.py races all sources in parallel; whichever responds first with a usable file
wins ("fastest source with good quality wins", not a strict waterfall).

Every function here is defensive: network/library errors are caught and
logged through the `logger` callback passed in by main.py, and every
resolver returns `None` on failure instead of raising, so the calling chain
in main.py can just try the next one.

Sources implemented (no cookies, no login required):
  0. YouTube              - yt-dlp with Android/TV/Embed/mweb/Web client jugad chain
                           (15 techniques: client cycling, force IPv4, FFmpeg reconnect,
                            audio-only, geo bypass, retry, search→ID→extract, auto-update)
  1. Direct media URLs   - any .mp3/.mp4/.ogg/.aac/.m4a/.wav/.flac/.opus,
                            m3u8/HLS, Icecast/Shoutcast/radio stream URL
  2. SoundCloud          - yt-dlp's built-in `scsearch` extractor
  3. Bandcamp            - public search-suggest API -> yt-dlp's native
                            Bandcamp extractor
  4. Mixcloud            - public REST search API -> yt-dlp's native
                            Mixcloud extractor
  5. Audius              - public REST API (api.audius.co)
  6. Internet Archive    - public `advancedsearch` + metadata API (also
                            used as a Musopen public-domain classical proxy)
  7. Wikimedia Commons   - public MediaWiki search API, audio namespace
  8. Openverse           - public CC-licensed audio search API
  9. HearThis.at         - public REST search API
  10. ccMixter           - public JSON query API (CC-licensed remixes)
  11. Jamendo            - public API, only if JAMENDO_CLIENT_ID is set
  12. Pixabay Music      - optional, only if PIXABAY_API_KEY is set
  13. iTunes metadata refine - public, no-key search used to clean up messy
                                queries before trying the sources above.
  14. Generic link resolver (`resolve_link`) - handles pasted links to
      Vocaroo, Catbox, Pixeldrain, Gofile, Google Drive, Dropbox, GitHub
      Raw/Releases, podcast RSS feeds, and any other public URL — first via
      a direct ffmpeg pull, then via yt-dlp's own (non-YouTube) extractor
      matching as a last resort. Audiomack, Free Music Archive, Pixabay
      Music and Bensound links are also playable through this generic path
      even though they don't have a dedicated no-key *search* API.

YouTube playback preserves the provider's playable m4a/webm/opus source
container by default, avoiding an extra FFmpeg transcode before playback.
Set MUSIC_TRANSCODE_AUDIO=1 when a normalized Opus output is required. Long-tail
fallback sources may still be converted when their provider requires it; where
a source offers it, the highest practical bitrate/resolution version is picked
instead of a default/preview-quality stream.

A tiny on-disk "stream cache" is also provided so repeat requests for the
same song reuse the already-downloaded file instead of re-fetching it.
"""

import asyncio
import contextlib
import json
import os
import sys as _sys_top

# bgutil is intentionally disabled. Heroku yt-dlp already discovers a provider
# from site-packages; injecting the bundled copy caused duplicate provider
# registration ("BgUtilHTTP already registered") on every music request.
_BGUTIL_PLUGIN_DIR = ""

# ── Cloud-host / IP-block detection (ported from Musicbot helpers/youtube.py) ──
# On Heroku/Railway/Render the YouTube CDN (googlevideo.com) is IP-blocked.
# Detect at startup and skip CDN streaming — go straight to yt-dlp download.
_ON_CLOUD_HOST: bool = bool(
    os.environ.get("DYNO")                    # Heroku
    or os.environ.get("RAILWAY_ENVIRONMENT")  # Railway
    or os.environ.get("RENDER_SERVICE_ID")    # Render
    or os.environ.get("FLY_APP_NAME")         # Fly.io
    or os.environ.get("K_SERVICE")            # Google Cloud Run
)
import random
import re
import shutil as _shutil
import subprocess
import sys as _sys
import threading
import time
import unicodedata
import urllib.parse as _urlparse
from contextlib import asynccontextmanager

# ── ffmpeg / ffprobe resolution (shared with main.py) ───────────────────────
# See ffmpeg_setup.py for the root-cause writeup. The key invariant: we only
# ever report an ffmpeg directory that contains a WORKING ffmpeg *and* a
# working ffprobe, otherwise yt-dlp postprocessing dies with
# "Postprocessing: ffprobe and ffmpeg not found".
from ffmpeg_setup import (
    ensure_ffmpeg as _ensure_ffmpeg,
    ffmpeg_opts   as _ffmpeg_opts,
    audio_pp      as _audio_pp,
)
import ffmpeg_setup as _ffsetup

_ensure_ffmpeg()
_MS_FFMPEG_BIN  = _ffsetup.FFMPEG_BIN  or "ffmpeg"
_MS_FFPROBE_BIN = _ffsetup.FFPROBE_BIN or "ffprobe"
_MS_FFMPEG_DIR  = _ffsetup.FFMPEG_DIR

# YouTube already provides playable m4a/webm/opus audio. Re-encoding every
# download to Opus adds an avoidable FFmpeg pass after the network download.
# Preserve the source container by default for faster first playback; set
# MUSIC_TRANSCODE_AUDIO=1 when a normalized Opus output is explicitly needed.
def _youtube_audio_opts() -> dict:
    flag = os.environ.get("MUSIC_TRANSCODE_AUDIO", "0").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return _audio_pp("opus", "192")
    return {**_ffmpeg_opts(), "postprocessors": []}

# ── ROOT FIX (music play nahi hota) #1 — dead-mirror cooldown ───────────────
# Piped/Invidious public mirrors are mostly dead (502/404/401/DNS/SSL).  Har
# .play par poori list dobara try hoti thi → 25-40s har song me barbaad, aur
# main.py ka 120s wait_for timeout hit ho jaata tha, isliye kuch bhi nahi
# bajta tha.  Ek baar fail hone par host 30 min ke liye skip ho jaata hai.
_DEAD_HOSTS: dict = {}
_DEAD_HOST_TTL = 1800.0


def _host_dead(url: str) -> bool:
    exp = _DEAD_HOSTS.get(url)
    if not exp:
        return False
    if exp > time.time():
        return True
    _DEAD_HOSTS.pop(url, None)
    return False


def _mark_host_dead(url: str) -> None:
    _DEAD_HOSTS[url] = time.time() + _DEAD_HOST_TTL


def _live_hosts(hosts):
    """Skip hosts that failed recently; never return an empty list."""
    hosts = list(hosts or [])
    live = [h for h in hosts if not _host_dead(h)]
    return live if live else hosts


# ── ROOT FIX (music play nahi hota) #2 — downloaded file resolution ────────
# Purana code sirf `out_tmpl.replace("%(ext)s", ext)` se file dhoondta tha.
# Agar outtmpl me koi bhi doosra yt-dlp field (%(id)s, %(title)s ...) hai, ya
# postprocessor ne koi aur extension likhi hai, to path match hi nahi karta —
# file disk par hone ke baavjood download "fail" maana jaata tha aur .play
# chup-chaap kuch nahi bajata tha.  Ab yt-dlp ka apna reported filepath use
# hota hai, phir template, phir glob.
def _resolve_downloaded_file(info: dict | None, out_tmpl: str,
                             exts=("opus", "mp3", "m4a", "webm", "ogg", "mp4", "aac")):
    import glob as _glob

    def _ok(path):
        try:
            return bool(path) and os.path.exists(path) and os.path.getsize(path) > 4096
        except Exception:
            return False

    # 1) yt-dlp ka apna reported path (postprocessing ke baad wala bhi)
    for req in (info or {}).get("requested_downloads") or []:
        for key in ("filepath", "_filename", "filename"):
            cand = req.get(key)
            if _ok(cand):
                return cand
    cand = (info or {}).get("filepath") or (info or {}).get("_filename")
    if _ok(cand):
        return cand

    # 2) Template + known extensions
    for ext in exts:
        cand = out_tmpl.replace("%(ext)s", ext)
        if _ok(cand):
            return cand

    # 3) Glob — har yt-dlp field ko wildcard bana do
    pattern = re.sub(r"%\([^)]+\)[a-zA-Z]", "*", out_tmpl)
    matches = [m for m in _glob.glob(pattern) if _ok(m)]
    if matches:
        # sabse recent + playable extension ko prefer karo
        matches.sort(key=lambda m: (os.path.splitext(m)[1].lstrip(".") in exts,
                                    os.path.getmtime(m)))
        return matches[-1]
    return None


def _ms_find_bin(name: str) -> str | None:
    if name == "ffmpeg":
        return _ffsetup.FFMPEG_BIN
    if name == "ffprobe":
        return _ffsetup.FFPROBE_BIN
    return _shutil.which(name)
# ─────────────────────────────────────────────────────────────────────────────

# ── YouTube cookie support via env var ───────────────────────────────────────
# Set YTDLP_COOKIES in Heroku Config Vars (or .env for local) to the full
# Netscape/HTTP cookie file content (from browser extension like "Get cookies.txt LOCALLY").
# The bot writes the content to a temp file at startup and passes it to yt-dlp.
# This lets age-restricted / geo-locked / bot-checked videos play normally.
#
# How to get cookies:
#   1. Log into YouTube in Chrome/Firefox
#   2. Install "Get cookies.txt LOCALLY" extension
#   3. Export cookies for youtube.com (Netscape format)
#   4. Copy the full file content → set as YTDLP_COOKIES env var
#
# Leave YTDLP_COOKIES unset to keep the current no-cookie behavior.
# Env aliases: YTDLP_COOKIES (this bot) and YT_COOKIES (Melody_music naming).
# Value may be plain Netscape text, a browser-extension JSON array, or base64
# of either — all four shapes are accepted.
_YTDLP_COOKIES_ENV = (
    os.environ.get("YTDLP_COOKIES", "").strip()
    or os.environ.get("YT_COOKIES", "").strip()
    or os.environ.get("COOKIES", "").strip()
)

# ROOT-CAUSE FIX (log 2026-08-23: every cloud_dl rung, including web_safari
# +cookies, dies with "Sign in to confirm you're not a bot"):
# yt-dlp WRITES the cookie jar back to `cookiefile` on close. This bot runs
# several yt-dlp extractions in parallel (download + prefetch + stream warm),
# so two writers truncate/interleave the SAME cookie file. The surviving
# first line stops being the "# Netscape HTTP Cookie File" header and the
# refreshed __Secure-3PSID / SAPISID / LOGIN_INFO cookies get corrupted →
# every subsequent request is anonymous → bot-gate on every rung.
# Fix (from Melody_music): keep the normalized cookie text in MEMORY as the
# single source of truth and hand every yt-dlp run its OWN throwaway copy.
_YTDLP_COOKIE_TEXT: str = ""
_YTDLP_COOKIE_DIR = "/tmp/yt_cookies_runs"
_YTDLP_COOKIE_LOCK = threading.Lock()
_YTDLP_COOKIE_TTL_SECONDS = 900


def _normalize_cookie_text(raw: str) -> str:
    """Return a clean Netscape cookie file body, or '' if nothing usable."""
    # Heroku Config Var CLI paste bug: literal \n / \t instead of real ones.
    content = raw.replace("\\n", "\n").replace("\\t", "\t")
    lines_out: list[str] = ["# Netscape HTTP Cookie File", ""]
    for line in content.splitlines():
        stripped = line.rstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\t" not in stripped:
            parts = stripped.split()
            if len(parts) == 7:
                stripped = "\t".join(parts)
        if stripped.count("\t") == 6:
            lines_out.append(stripped)
    if len(lines_out) <= 2:
        return ""
    return "\n".join(lines_out) + "\n"


def _cookies_json_to_netscape(raw: str) -> str:
    """Convert a browser-extension JSON cookie array to Netscape text."""
    try:
        import json as _json
        data = _json.loads(raw)
    except Exception:
        return ""
    if not isinstance(data, list):
        return ""
    out = ["# Netscape HTTP Cookie File", ""]
    for c in data:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        domain = str(c.get("domain") or "")
        if not domain:
            continue
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(c.get("path") or "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = c.get("expirationDate") or c.get("expires") or 0
        try:
            exp = str(int(float(exp)))
        except Exception:
            exp = "0"
        out.append("\t".join([domain, flag, path, secure, exp,
                              str(c["name"]), str(c.get("value", ""))]))
    return ("\n".join(out) + "\n") if len(out) > 2 else ""


def _decode_cookie_env(raw: str) -> str:
    """Accept Netscape text, JSON array, or base64 of either."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.lstrip().startswith("["):
        got = _cookies_json_to_netscape(raw)
        if got:
            return got
    got = _normalize_cookie_text(raw)
    if got:
        return got
    # base64 (Heroku-friendly single-line paste)
    try:
        import base64 as _b64
        decoded = _b64.b64decode(raw + "=" * (-len(raw) % 4), validate=False)
        text = decoded.decode("utf-8", "strict")
    except Exception:
        return ""
    if text.lstrip().startswith("["):
        got = _cookies_json_to_netscape(text)
        if got:
            return got
    return _normalize_cookie_text(text)


def _init_yt_cookies():
    global _YTDLP_COOKIE_TEXT
    if not _YTDLP_COOKIES_ENV:
        return
    _YTDLP_COOKIE_TEXT = _decode_cookie_env(_YTDLP_COOKIES_ENV)


_init_yt_cookies()


def _reap_cookie_copies() -> None:
    cutoff = time.time() - _YTDLP_COOKIE_TTL_SECONDS
    try:
        for name in os.listdir(_YTDLP_COOKIE_DIR):
            path = os.path.join(_YTDLP_COOKIE_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def cookiefile_for_run() -> "str | None":
    """Fresh private copy of the cookie jar for one yt-dlp run (or None).

    Never returns the master path — yt-dlp may write back to it and corrupt
    the jar for every concurrent extraction.
    """
    if not _YTDLP_COOKIE_TEXT:
        return None
    with _YTDLP_COOKIE_LOCK:
        try:
            os.makedirs(_YTDLP_COOKIE_DIR, exist_ok=True)
            _reap_cookie_copies()
            path = os.path.join(
                _YTDLP_COOKIE_DIR,
                f"c_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.txt",
            )
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(_YTDLP_COOKIE_TEXT)
            os.replace(tmp, path)
            return path
        except OSError:
            return None


# Back-compat shim: any caller that still reads _YTDLP_COOKIE_FILE gets a
# fresh private copy (never the master).
class _CookieFileProxy:
    def __bool__(self) -> bool:
        return bool(_YTDLP_COOKIE_TEXT)

    def __str__(self) -> str:
        return cookiefile_for_run() or ""

    def __fspath__(self) -> str:
        return cookiefile_for_run() or ""


_YTDLP_COOKIE_FILE = _CookieFileProxy() if _YTDLP_COOKIE_TEXT else None


def _cookie_opts() -> dict:
    """Return ``{"cookiefile": path}`` with a fresh per-run copy, or empty."""
    p = cookiefile_for_run()
    return {"cookiefile": p} if p else {}
# ─────────────────────────────────────────────────────────────────────────────

try:
    import requests
except ImportError:
    requests = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# ── Residential/rotating proxy support (YT_PROXY) ────────────────────────────
# Heroku/cloud IPs are permanently flagged by YouTube ("Sign in to confirm
# you're not a bot") on EVERY InnerTube client — android, web_safari, tv,
# default, web_embedded, tv_simply all fail from the same datacenter IP even
# with a valid PO-token. The only two real cures are (a) logged-in cookies and
# (b) routing yt-dlp through a non-datacenter proxy. Melody_music exposes both;
# this bot only had cookies, so we add YT_PROXY here and apply it to EVERY
# yt-dlp instance (no matter which of the ~15 call sites builds the opts dict).
_YT_PROXY = (
    os.environ.get("YT_PROXY", "").strip()
    or os.environ.get("YTDLP_PROXY", "").strip()
    or os.environ.get("HTTP_PROXY_YT", "").strip()
)

if yt_dlp is not None and _YT_PROXY:
    _OrigYoutubeDL = yt_dlp.YoutubeDL

    class _ProxiedYoutubeDL(_OrigYoutubeDL):  # type: ignore[misc]
        def __init__(self, params=None, *a, **kw):
            params = dict(params or {})
            if not params.get("proxy"):
                params["proxy"] = _YT_PROXY
            super().__init__(params, *a, **kw)

    yt_dlp.YoutubeDL = _ProxiedYoutubeDL  # type: ignore[assignment]

try:
    import aiohttp as _aiohttp
except ImportError:
    _aiohttp = None


@asynccontextmanager
async def _make_aiohttp_session():
    """Yield a short-lived aiohttp ClientSession.
    Falls back to a dummy context manager if aiohttp is not installed."""
    if _aiohttp is None:
        raise RuntimeError("aiohttp not installed — pip install aiohttp")
    connector = _aiohttp.TCPConnector(ssl=False)
    async with _aiohttp.ClientSession(connector=connector) as sess:
        yield sess


# ── YouTube Data API v3 fast search (env: YOUTUBE_API_KEY) ──────────────
#
# Bina API key ke query→video resolve karne ke liye yt-dlp ko "ytsearch1:"
# scrape karni padti hai (ya Piped/Invidious mirrors, jo aksar dead hote hain)
# — ye 5-40s kha jaata hai. Ek YouTube Data API v3 key hone par search
# official endpoint se ~200-400ms me resolve ho jaati hai, aur download
# seedha watch?v=<id> par chalta hai (direct-URL path = sabse fast).
#
# Env:
#   YOUTUBE_API_KEY  (alias: YT_API_KEY / YOUTUBE_API_KEYS)
#   Comma/space se multiple keys de sakte ho — quota khatam hone par
#   agli key par apne aap rotate ho jaata hai.
_YT_API_KEYS: list[str] = [
    k.strip() for k in re.split(
        r"[,\s]+",
        (os.environ.get("YOUTUBE_API_KEY")
         or os.environ.get("YOUTUBE_API_KEYS")
         or os.environ.get("YT_API_KEY")
         or ""),
    ) if k.strip()
]
_YT_API_DEAD: dict[str, float] = {}     # key → cooldown expiry (quota/403)
_YT_API_CACHE: dict[str, tuple[float, dict]] = {}   # query → (ts, result)
_YT_API_CACHE_TTL = 3600.0


def yt_api_enabled() -> bool:
    return bool(_YT_API_KEYS)


def _yt_api_live_keys() -> list[str]:
    now = time.time()
    return [k for k in _YT_API_KEYS if _YT_API_DEAD.get(k, 0) < now]


async def yt_api_search(query: str, logger=None, video: bool = False) -> dict | None:
    """Resolve a search query to a YouTube video via the Data API v3.

    Returns ``{"video_id", "url", "title", "channel", "duration"}`` or None.
    Never raises — har failure par None, taki purana ladder chalta rahe.
    """
    logger = logger or (lambda *a: None)
    q = (query or "").strip()
    if not q or not _yt_api_live_keys() or _aiohttp is None:
        return None

    ck = ("v::" if video else "a::") + q.lower()
    hit = _YT_API_CACHE.get(ck)
    if hit and (time.time() - hit[0]) < _YT_API_CACHE_TTL:
        return hit[1]

    params = {
        "part": "snippet",
        "q": q,
        "type": "video",
        "maxResults": "5",
        "videoEmbeddable": "true",
        "safeSearch": "none",
    }
    if not video:
        # Music category → covers/lyric-video noise kam, sahi gaana upar aata hai.
        params["videoCategoryId"] = "10"

    timeout = _aiohttp.ClientTimeout(total=8)
    for key in _yt_api_live_keys():
        try:
            async with _make_aiohttp_session() as sess:
                async with sess.get(
                    "https://www.googleapis.com/youtube/v3/search",
                    params={**params, "key": key}, timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                    else:
                        body = ""
                        with contextlib.suppress(Exception):
                            body = (await resp.text())[:300]
                        low = body.lower()
                        # Retire the key ONLY when Google actually says it is
                        # dead (quotaExceeded / keyInvalid / API key not valid
                        # / accessNotConfigured). Transient HTTP 400s (bad
                        # param, region block, hiccup) must NOT nuke the key
                        # — Melody bot ka same fix; warna ek transient 400
                        # saari keys 1 ghante ke liye cooldown me daal deta
                        # tha aur music bajna hi band ho jaata tha.
                        fatal = (
                            "quota" in low
                            or "keyinvalid" in low
                            or "api key not valid" in low
                            or "accessnotconfigured" in low
                            or "api_key_invalid" in low
                            or "keyexpired" in low
                        )
                        if resp.status == 429 or (resp.status in (400, 403) and fatal):
                            _YT_API_DEAD[key] = time.time() + (
                                300 if resp.status == 429 else 3600
                            )
                            logger("MUSIC_YT_API",
                                   f"key retired (HTTP {resp.status}): {body[:120]}")
                        else:
                            logger("MUSIC_YT_API",
                                   f"transient HTTP {resp.status} (key kept): {body[:120]}")
                        continue

            items = [i for i in (data.get("items") or [])
                     if (i.get("id") or {}).get("videoId")]
            if not items:
                return None
            top = items[0]
            vid = top["id"]["videoId"]
            snip = top.get("snippet") or {}
            out = {
                "video_id": vid,
                "url":      f"https://www.youtube.com/watch?v={vid}",
                "title":    snip.get("title") or q,
                "channel":  snip.get("channelTitle") or "",
                "duration": 0,
                "alt_ids":  [i["id"]["videoId"] for i in items[1:4]],
            }

            # Duration ek extra videos.list call se (1 quota unit, sasta).
            with contextlib.suppress(Exception):
                async with _make_aiohttp_session() as sess:
                    async with sess.get(
                        "https://www.googleapis.com/youtube/v3/videos",
                        params={"part": "contentDetails", "id": vid, "key": key},
                        timeout=timeout,
                    ) as r2:
                        if r2.status == 200:
                            vd = await r2.json()
                            iso = (((vd.get("items") or [{}])[0].get("contentDetails")
                                    or {}).get("duration") or "")
                            m = re.match(
                                r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
                            if m:
                                d, h, mi, se = (int(x or 0) for x in m.groups())
                                out["duration"] = d * 86400 + h * 3600 + mi * 60 + se

            _YT_API_CACHE[ck] = (time.time(), out)
            logger("MUSIC_YT_API", f"⚡ {out['title'][:50]!r} → {vid}")
            return out
        except Exception:
            continue
    return None


# ── Query / title normalization ─────────────────────────────────────────

def normalize_query(text: str) -> str:
    """Collapse whitespace/case/accents so 'Believer  - Imagine Dragons' and
    'believer imagine dragons' hit the same cache/dedupe bucket."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_DIRECT_AUDIO_EXT_RE = re.compile(
    r"\.(mp3|ogg|oga|aac|m4a|wav|flac|opus)(\?.*)?$", re.IGNORECASE
)
_HLS_RE = re.compile(r"\.m3u8(\?.*)?$", re.IGNORECASE)


# ── Silence-frame priming asset (jugad #9 support) ──────────────────────
# main.py plays this tiny silent clip immediately before the real track so
# the voice-chat channel is already "warmed up" by the time real audio
# arrives, instead of eating the first fraction of a second while it syncs.
# Generated once via ffmpeg's anullsrc filter (no bundled binary asset
# needed) and cached on disk next to this module so every account/session
# can reuse the same file — it's silent, so nothing about it is per-session.
_SILENCE_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
SILENCE_FRAME_PATH = os.path.join(_SILENCE_CACHE_DIR, "silence_frame.opus")


def _ensure_silence_frame() -> str | None:
    """Generates SILENCE_FRAME_PATH on disk if it doesn't already exist.
    Returns the path, or None if ffmpeg isn't available/failed (callers
    already treat priming as best-effort and swallow failures)."""
    try:
        if os.path.exists(SILENCE_FRAME_PATH) and os.path.getsize(SILENCE_FRAME_PATH) > 0:
            return SILENCE_FRAME_PATH
        os.makedirs(_SILENCE_CACHE_DIR, exist_ok=True)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-t", "0.5", "-c:a", "libopus", "-b:a", "16k", SILENCE_FRAME_PATH],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        if proc.returncode == 0 and os.path.exists(SILENCE_FRAME_PATH):
            return SILENCE_FRAME_PATH
    except Exception:
        pass
    return None


# Generate it once at import time — main.py starts fast enough (this takes
# well under a second) and every account's music engine needs the same file.
_ensure_silence_frame()


def is_url(text: str) -> bool:
    return bool(text and _URL_RE.match(text.strip()))


def is_direct_media_url(text: str) -> bool:
    """True for a direct audio file URL, an HLS (.m3u8) stream, or an
    Icecast/Shoutcast style stream URL — anything ffmpeg can pull straight
    off the wire without a site-specific extractor."""
    if not is_url(text):
        return False
    t = text.strip()
    return bool(_DIRECT_AUDIO_EXT_RE.search(t) or _HLS_RE.search(t))


# ── Stream cache (dedupe repeat downloads) ──────────────────────────────

class StreamCache:
    """Maps a normalized query -> the file already downloaded for it, so
    replaying the same song doesn't re-download it every time. Backed by a
    small JSON index file inside the music cache directory."""

    def __init__(self, cache_dir: str, logger=None):
        self.cache_dir = cache_dir
        self.index_path = os.path.join(cache_dir, "_stream_cache_index.json")
        self.logger = logger or (lambda tag, msg: None)
        self._index = {}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.index_path):
                with open(self.index_path, "r", encoding="utf-8") as f:
                    self._index = json.load(f)
        except Exception as e:
            self.logger("STREAM_CACHE_ERR", f"Failed to load cache index: {e}")
            self._index = {}

    def _save(self):
        try:
            with open(self.index_path, "w", encoding="utf-8") as f:
                json.dump(self._index, f)
        except Exception as e:
            self.logger("STREAM_CACHE_ERR", f"Failed to save cache index: {e}")

    def get(self, query: str):
        key = normalize_query(query)
        entry = self._index.get(key)
        if not entry:
            return None
        if not os.path.exists(entry.get("file_path", "")):
            # Cached file was cleaned up on disk — drop the stale entry.
            self._index.pop(key, None)
            self._save()
            return None
        return entry

    def put(self, query: str, title: str, file_path: str, duration: int,
            is_video: bool, thumbnail, source: str):
        key = normalize_query(query)
        self._index[key] = {
            "title": title,
            "file_path": file_path,
            "duration": duration,
            "is_video": is_video,
            "thumbnail": thumbnail,
            "source": source,
            "cached_at": time.time(),
        }
        self._save()


# ── Duplicate detection for the play queue ──────────────────────────────

def is_duplicate_in_queue(title: str, current, queue) -> bool:
    """True if a track with the same (normalized) title is already playing
    or already queued, so we skip adding an exact repeat back-to-back."""
    key = normalize_query(title)
    if not key:
        return False
    if current is not None and normalize_query(getattr(current, "title", "")) == key:
        return True
    for t in queue:
        if normalize_query(getattr(t, "title", "")) == key:
            return True
    return False


# ── Direct media URL (mp3/ogg/aac/m3u8/Icecast/Shoutcast/etc.) ──────────

async def download_direct_media(url: str, out_tmpl: str, logger=None):
    """Pull a direct audio URL / HLS stream straight through ffmpeg. Returns
    (title, file_path, duration) or None."""
    logger = logger or (lambda tag, msg: None)
    mp3_path = out_tmpl.replace("%(ext)s", "mp3")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", url, "-vn", "-ar", "44100", "-ac", "2",
            "-b:a", "320k", mp3_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=120)
        if proc.returncode != 0 or not os.path.exists(mp3_path):
            return None
        title = os.path.basename(url.split("?")[0]) or "Direct Stream"
        duration = await _probe_duration(mp3_path)
        return title, mp3_path, duration
    except Exception as e:
        logger("MUSIC_DL_ERR", f"Direct media download failed: {e}")
        return None


async def _probe_duration(path: str) -> int:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        return int(float(out.decode().strip() or 0))
    except Exception:
        return 0


# ── SoundCloud client_id helper ─────────────────────────────────────────────
# yt-dlp's SoundCloud extractor auto-extracts client_id from SC's web JS.
# That process breaks whenever SC updates their page (happens several times/year).
# We implement our own extraction + 1-hour in-process cache so both the zero-disk
# and disk SoundCloud paths stay alive even when yt-dlp's version is stale.

_SC_CLIENT_ID_CACHE: dict = {"id": None, "ts": 0.0}

def _sc_get_client_id(logger=None) -> str | None:
    """Extract a working SoundCloud client_id from their web JS (1-hour cache)."""
    _log = logger or (lambda *a: None)
    now = time.time()
    if _SC_CLIENT_ID_CACHE["id"] and (now - _SC_CLIENT_ID_CACHE["ts"]) < 3600:
        return _SC_CLIENT_ID_CACHE["id"]
    if requests is None:
        return None
    try:
        r = requests.get("https://soundcloud.com", timeout=12,
                         headers={"User-Agent": random_ua()})
        r.raise_for_status()
        # Find JS asset URLs embedded in the page
        js_urls = re.findall(
            r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', r.text)
        if not js_urls:
            # Fallback: find any sndcdn asset
            js_urls = re.findall(r'"(https://[^"]+sndcdn\.com/assets/[^"]+\.js)"', r.text)
        for url in reversed(js_urls[-8:]):   # try last 8 assets (config likely near end)
            try:
                js = requests.get(url, timeout=8,
                                  headers={"User-Agent": random_ua()}).text
                # client_id is a 32-char alphanum string
                m = (re.search(r'client_id\s*[=:]\s*["\']([a-zA-Z0-9]{32})["\']', js) or
                     re.search(r'["\']client_id["\']\s*,\s*["\']([a-zA-Z0-9]{32})["\']', js))
                if m:
                    cid = m.group(1)
                    _SC_CLIENT_ID_CACHE["id"] = cid
                    _SC_CLIENT_ID_CACHE["ts"]  = now
                    _log("MUSIC_DL", f"SoundCloud client_id refreshed: {cid[:8]}…")
                    return cid
            except Exception:
                continue
    except Exception as e:
        _log("MUSIC_DL_ERR", f"sc_client_id_extract: {e}")
    return None


async def soundcloud_search_download(query: str, out_tmpl: str, ydl_common: dict,
                                      logger=None):
    """SoundCloud public tracks via direct SC API v2 — bypasses yt-dlp's
    broken client_id extraction (yt-dlp's `scsearch1:` depends on scraping
    SC's web page for a client_id, which breaks frequently).
    Strategy:
      1. Extract client_id from SC's web JS (cached 1h).
      2. Search via SC API v2.
      3. Get the progressive-MP3 stream URL.
      4. Download with requests directly (no ffmpeg needed for plain MP3).
    Falls back to yt-dlp scsearch if the direct API fails."""
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None

    # ── Step 1: get client_id ─────────────────────────────────────────────
    client_id = _sc_get_client_id(logger)

    # ── Step 2: search + get stream URL via SC API v2 ─────────────────────
    def _resolve_via_api():
        if not client_id:
            return None
        try:
            search_resp = requests.get(
                "https://api-v2.soundcloud.com/search/tracks",
                params={"q": query, "client_id": client_id,
                        "limit": 5, "offset": 0, "linked_partitioning": 1},
                headers={"User-Agent": random_ua()}, timeout=10,
            )
            search_resp.raise_for_status()
            tracks = search_resp.json().get("collection") or []
            if not tracks:
                return None
            # Pick best title match
            best = max(
                tracks,
                key=lambda t: token_match_score(
                    query, f"{t.get('title','')} {t.get('user',{}).get('username','')}"
                ),
                default=None,
            )
            if not best:
                best = tracks[0]
            title = best.get("title") or query
            if looks_like_live_or_reaction(title, query):
                if len(tracks) > 1:
                    best = tracks[1]
                    title = best.get("title") or query
            # Get stream transcoding URL (progressive = direct mp3/m4a)
            transcodings = (best.get("media") or {}).get("transcodings") or []
            prog = next(
                (t for t in transcodings
                 if (t.get("format") or {}).get("protocol") == "progressive"),
                None,
            )
            if not prog:
                prog = transcodings[0] if transcodings else None
            if not prog:
                return None
            # Resolve the actual CDN URL
            tc_resp = requests.get(
                prog["url"],
                params={"client_id": client_id},
                headers={"User-Agent": random_ua()}, timeout=10,
            )
            tc_resp.raise_for_status()
            stream_url = tc_resp.json().get("url")
            if not stream_url:
                return None
            return {
                "title": title,
                "stream_url": stream_url,
                "duration": int(best.get("duration") or 0) // 1000,
                "thumbnail": best.get("artwork_url"),
            }
        except Exception as e:
            logger("MUSIC_DL_ERR", f"sc_api_resolve: {e}")
            return None

    meta = await asyncio.to_thread(_resolve_via_api)

    if not meta:
        # Fall back to yt-dlp scsearch (might fail with client_id issue but worth trying)
        if yt_dlp is None:
            return None
        def _ydl_fallback():
            opts = {
                **ydl_common,
                "format": "bestaudio/best",
                "outtmpl": out_tmpl,
                **_audio_pp("mp3", "192"),
            }
            opts.pop("cookiefile", None)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(f"scsearch1:{query}", download=True)
                    if isinstance(info, dict) and "entries" in info:
                        info = ([e for e in info["entries"] if e] or [None])[0]
                    if not info:
                        return None
                    fpath = out_tmpl.replace("%(ext)s", "mp3")
                    if not os.path.exists(fpath):
                        return None
                    return {
                        "title": info.get("title", query),
                        "file_path": fpath,
                        "duration": int(info.get("duration") or 0),
                        "thumbnail": info.get("thumbnail"),
                        "source": "soundcloud",
                    }
            except Exception as e:
                logger("MUSIC_DL_ERR", f"SoundCloud yt-dlp fallback: {e}")
                return None
        return await asyncio.to_thread(_ydl_fallback)

    # ── Step 3: download stream → disk ────────────────────────────────────
    def _fetch():
        try:
            mp3_path = out_tmpl.replace("%(ext)s", "sc.mp3")
            with requests.get(
                meta["stream_url"],
                headers={"User-Agent": random_ua()},
                timeout=60, stream=True,
            ) as r:
                r.raise_for_status()
                with open(mp3_path, "wb") as f:
                    for chunk in r.iter_content(131072):
                        if chunk:
                            f.write(chunk)
            if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 10240:
                return mp3_path
            return None
        except Exception as e:
            logger("MUSIC_DL_ERR", f"sc_fetch: {e}")
            return None

    file_path = await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=60)
    if not file_path:
        return None
    logger("MUSIC_DL", f"SoundCloud ✓ {meta['title']!r}")
    return {
        "title":     meta["title"],
        "file_path": file_path,
        "duration":  meta["duration"],
        "thumbnail": meta["thumbnail"],
        "source":    "soundcloud",
    }


# ── Audius (public REST API, no auth) ───────────────────────────────────

_AUDIUS_HOSTS_FALLBACK = [
    "https://discoveryprovider.audius.co",
    "https://audius-discovery-1.altego.net",
    "https://discoveryprovider2.audius.co",
    "https://discoveryprovider3.audius.co",
]
_audius_host_cache = None


def _audius_hosts():
    global _audius_host_cache
    if _audius_host_cache:
        return _audius_host_cache
    hosts = []
    if requests is not None:
        try:
            resp = requests.get("https://api.audius.co", timeout=8)
            data = resp.json().get("data") or []
            hosts.extend(h.rstrip("/") for h in data if isinstance(h, str))
        except Exception:
            pass
    for fb in _AUDIUS_HOSTS_FALLBACK:
        if fb not in hosts:
            hosts.append(fb)
    _audius_host_cache = hosts
    return hosts


async def audius_search_download(query: str, out_tmpl: str, logger=None):
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None

    def _resolve():
        for host in _audius_hosts():
            try:
                resp = requests.get(
                    f"{host}/v1/tracks/search",
                    params={"query": query, "app_name": "TelegramMusicBot"},
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue
                results = (resp.json() or {}).get("data") or []
                if not results:
                    continue
                track = results[0]
                stream_url = (
                    f"{host}/v1/tracks/{track['id']}/stream"
                    "?app_name=TelegramMusicBot"
                )
                return track, stream_url
            except Exception as e:
                logger("MUSIC_DL_ERR", f"Audius host {host} failed: {e}")
                continue
        return None

    try:
        resolved = await asyncio.to_thread(_resolve)
        if not resolved:
            return None
        track, stream_url = resolved

        # Audius may serve mp3, m4a, ogg, opus, or an HLS mux depending
        # on the track.  Detect the real format from the Content-Type
        # header so ffmpeg demuxes it correctly, then always re-encode to
        # a clean 320 kbps MP3 — the same normalisation step used by
        # every other source (Archive.org, Wikimedia, Openverse) here.
        raw_base = out_tmpl.replace("%(ext)s", "audius_raw")
        mp3_path = out_tmpl.replace("%(ext)s", "mp3")

        def _fetch():
            with requests.get(stream_url, stream=True, timeout=120,
                               allow_redirects=True) as r:
                r.raise_for_status()
                ct = (r.headers.get("content-type") or "").lower()
                if "m4a" in ct or "mp4" in ct or "aac" in ct:
                    ext = "m4a"
                elif "ogg" in ct or "vorbis" in ct or "opus" in ct:
                    ext = "ogg"
                elif "webm" in ct:
                    ext = "webm"
                elif "flac" in ct:
                    ext = "flac"
                else:
                    # Default to mp3; ffmpeg will figure out the real mux.
                    ext = "mp3"
                raw_path = f"{raw_base}.{ext}"
                with open(raw_path, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            fh.write(chunk)
                return raw_path

        raw_file = await asyncio.to_thread(_fetch)
        if not raw_file or not os.path.exists(raw_file):
            return None

        # Re-encode to clean MP3 regardless of input format.
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", raw_file,
            "-vn", "-ar", "44100", "-ac", "2", "-b:a", "320k",
            mp3_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=120)
        try:
            os.remove(raw_file)
        except Exception:
            pass

        # Reject suspiciously tiny outputs (error pages, empty manifests).
        if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 4096:
            return None

        title  = track.get("title") or query
        artist = (track.get("user") or {}).get("name")
        return {
            "title":     f"{title} - {artist}" if artist else title,
            "file_path": mp3_path,
            "duration":  int(track.get("duration") or 0),
            "thumbnail": (track.get("artwork") or {}).get("480x480"),
            "source":    "audius",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"Audius fallback failed: {e}")
        return None

# ── Internet Archive (public, no auth) ──────────────────────────────────

async def archive_org_search_download(query: str, out_tmpl: str, logger=None):
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None

    def _resolve():
        try:
            resp = requests.get(
                "https://archive.org/advancedsearch.php",
                params={
                    "q": f'{query} AND mediatype:(audio)',
                    "fl[]": "identifier,title",
                    "rows": 1,
                    "output": "json",
                },
                timeout=12,
            )
            docs = ((resp.json() or {}).get("response") or {}).get("docs") or []
            if not docs:
                return None
            identifier = docs[0].get("identifier")
            title = docs[0].get("title") or query
            if not identifier:
                return None
            meta_resp = requests.get(f"https://archive.org/metadata/{identifier}", timeout=12)
            files = (meta_resp.json() or {}).get("files") or []
            audio_files = [
                f for f in files
                if (f.get("format") or "").lower() in
                ("vbr mp3", "mp3", "128kbps mp3", "flac", "ogg vorbis")
                and f.get("name")
            ]
            if not audio_files:
                return None
            best = audio_files[0]
            url = f"https://archive.org/download/{identifier}/{best['name']}"
            duration = best.get("length")
            try:
                duration = int(float(duration)) if duration else 0
            except Exception:
                duration = 0
            return title, url, duration
        except Exception as e:
            logger("MUSIC_DL_ERR", f"Internet Archive search failed: {e}")
            return None

    try:
        resolved = await asyncio.to_thread(_resolve)
        if not resolved:
            return None
        title, url, duration = resolved
        raw_ext = url.rsplit(".", 1)[-1].lower() or "bin"
        raw_path = out_tmpl.replace("%(ext)s", f"ia_{raw_ext}")

        def _fetch():
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(raw_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)

        await asyncio.to_thread(_fetch)
        mp3_path = out_tmpl.replace("%(ext)s", "mp3")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", raw_path, "-vn", "-ar", "44100", "-ac", "2",
            "-b:a", "320k", mp3_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await proc.wait()
        try:
            os.remove(raw_path)
        except Exception:
            pass
        if not os.path.exists(mp3_path):
            return None
        return {
            "title": title,
            "file_path": mp3_path,
            "duration": duration,
            "thumbnail": None,
            "source": "archive.org",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"Internet Archive fallback failed: {e}")
        return None


async def archive_org_video_search_download(query: str, out_tmpl: str, logger=None):
    """Internet Archive also hosts a large public/CC-licensed video
    library (movies, TV, educational and public-domain clips). Since
    YouTube search has been removed and there is no equally strong free,
    no-login general video-search API to replace it with, this is the
    best-effort text-search path for `.vplay <query>` — reply-to-video and
    direct video links are handled elsewhere and aren't limited by this."""
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None

    def _resolve():
        try:
            resp = requests.get(
                "https://archive.org/advancedsearch.php",
                params={
                    "q": f'{query} AND mediatype:(movies)',
                    "fl[]": "identifier,title",
                    "rows": 1,
                    "output": "json",
                },
                timeout=12,
            )
            docs = ((resp.json() or {}).get("response") or {}).get("docs") or []
            if not docs:
                return None
            identifier = docs[0].get("identifier")
            title = docs[0].get("title") or query
            if not identifier:
                return None
            meta_resp = requests.get(f"https://archive.org/metadata/{identifier}", timeout=12)
            files = (meta_resp.json() or {}).get("files") or []
            video_files = [
                f for f in files
                if (f.get("format") or "").lower() in
                ("512kb mp4", "h.264", "mpeg4", "matroska")
                and f.get("name") and str(f.get("name")).lower().endswith((".mp4", ".mkv"))
            ]
            if not video_files:
                return None
            best = video_files[0]
            url = f"https://archive.org/download/{identifier}/{best['name']}"
            duration = best.get("length")
            try:
                duration = int(float(duration)) if duration else 0
            except Exception:
                duration = 0
            return title, url, duration
        except Exception as e:
            logger("MUSIC_DL_ERR", f"Internet Archive video search failed: {e}")
            return None

    try:
        resolved = await asyncio.to_thread(_resolve)
        if not resolved:
            return None
        title, url, duration = resolved
        raw_ext = url.rsplit(".", 1)[-1].lower() or "mp4"
        raw_path = out_tmpl.replace("%(ext)s", f"iav_{raw_ext}")

        def _fetch():
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(raw_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)

        await asyncio.to_thread(_fetch)
        mp4_path = out_tmpl.replace("%(ext)s", "mp4")
        if raw_path == mp4_path:
            pass
        else:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", raw_path, "-c:v", "libx264", "-c:a", "aac",
                "-b:a", "192k", mp4_path,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            await proc.wait()
            try:
                os.remove(raw_path)
            except Exception:
                pass
        if not os.path.exists(mp4_path):
            return None
        return {
            "title": title,
            "file_path": mp4_path,
            "duration": duration,
            "thumbnail": None,
            "source": "archive.org",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"Internet Archive video fallback failed: {e}")
        return None


# ── Jamendo (public API — optional, only if JAMENDO_CLIENT_ID is set) ──

async def jamendo_search_download(query: str, out_tmpl: str, logger=None):
    logger = logger or (lambda tag, msg: None)
    client_id = os.environ.get("JAMENDO_CLIENT_ID", "").strip()
    if requests is None or not client_id:
        return None

    def _resolve():
        try:
            resp = requests.get(
                "https://api.jamendo.com/v3.0/tracks/",
                params={
                    "client_id": client_id,
                    "format": "json",
                    "limit": 1,
                    "search": query,
                    "audioformat": "mp32",
                },
                timeout=10,
            )
            results = (resp.json() or {}).get("results") or []
            if not results:
                return None
            track = results[0]
            return track
        except Exception as e:
            logger("MUSIC_DL_ERR", f"Jamendo search failed: {e}")
            return None

    try:
        track = await asyncio.to_thread(_resolve)
        if not track or not track.get("audio"):
            return None
        mp3_path = out_tmpl.replace("%(ext)s", "mp3")

        def _fetch():
            with requests.get(track["audio"], stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(mp3_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)

        await asyncio.to_thread(_fetch)
        if not os.path.exists(mp3_path):
            return None
        return {
            "title": f"{track.get('name', query)} - {track.get('artist_name', '')}".strip(" -"),
            "file_path": mp3_path,
            "duration": int(track.get("duration") or 0),
            "thumbnail": track.get("image"),
            "source": "jamendo",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"Jamendo fallback failed: {e}")
        return None


# ── Bandcamp (public search-suggest API + yt-dlp's native extractor) ────

async def bandcamp_search_download(query: str, out_tmpl: str, logger=None):
    """Bandcamp has no `bcsearch:` prefix in yt-dlp, so we resolve the query
    to a track/album URL ourselves via Bandcamp's public search-suggest
    endpoint, then hand that URL to yt-dlp's native Bandcamp extractor
    (which yt-dlp already ships) to actually download the audio."""
    logger = logger or (lambda tag, msg: None)
    if requests is None or yt_dlp is None:
        return None

    def _resolve_url():
        try:
            resp = requests.get(
                "https://bandcamp.com/api/nusearch/2/autocomplete",
                params={"q": query, "size": 5},
                timeout=10,
                headers={"User-Agent": random_ua()},
            )
            if not resp.content or resp.status_code != 200:
                return None
            try:
                data = resp.json()
            except Exception:
                return None
            results = (data or {}).get("results") or []
            for r in results:
                if r.get("type") in ("t", "a") and r.get("url"):  # track / album
                    return r["url"]
            return None
        except Exception as e:
            logger("MUSIC_DL_ERR", f"Bandcamp search failed: {e}")
            return None

    def _download(url):
        opts = {
            "quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "noplaylist": True,
            **_audio_pp("mp3", "320"),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if isinstance(info, dict) and "entries" in info:
                entries = [e for e in info["entries"] if e]
                if not entries:
                    return None
                info = entries[0]
            return info

    try:
        url = await asyncio.to_thread(_resolve_url)
        if not url:
            return None
        info = await asyncio.to_thread(_download, url)
        fpath = out_tmpl.replace("%(ext)s", "mp3")
        if not info or not os.path.exists(fpath):
            return None
        return {
            "title": info.get("title", query),
            "file_path": fpath,
            "duration": int(info.get("duration") or 0),
            "thumbnail": info.get("thumbnail"),
            "source": "bandcamp",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"Bandcamp fallback failed: {e}")
        return None


# ── Mixcloud (public REST search API + yt-dlp's native extractor) ───────

async def mixcloud_search_download(query: str, out_tmpl: str, logger=None):
    """Mixcloud's public `/search/` API resolves a query to a cloudcast
    URL, then yt-dlp's native Mixcloud extractor downloads the audio.
    Best suited to DJ sets / mixes / radio shows; regular studio tracks are
    less commonly hosted here than on the music-specific sources above."""
    logger = logger or (lambda tag, msg: None)
    if requests is None or yt_dlp is None:
        return None

    def _resolve_url():
        try:
            resp = requests.get(
                "https://api.mixcloud.com/search/",
                params={"q": query, "type": "cloudcast", "limit": 1},
                timeout=15,
            )
            if resp.status_code != 200 or not resp.content:
                return None
            try:
                data = resp.json()
            except Exception:
                return None
            results = (data or {}).get("data") or []
            if not results:
                return None
            url = results[0].get("url")
            if not url:
                return None
            return url
        except Exception as e:
            logger("MUSIC_DL_ERR", f"Mixcloud search failed: {e}")
            return None

    def _download(url):
        opts = {
            "quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "noplaylist": True,
            **_audio_pp("mp3", "320"),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        url = await asyncio.to_thread(_resolve_url)
        if not url:
            return None
        info = await asyncio.to_thread(_download, url)
        fpath = out_tmpl.replace("%(ext)s", "mp3")
        if not info or not os.path.exists(fpath):
            return None
        return {
            "title": info.get("title", query),
            "file_path": fpath,
            "duration": int(info.get("duration") or 0),
            "thumbnail": info.get("thumbnail"),
            "source": "mixcloud",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"Mixcloud fallback failed: {e}")
        return None


# ── Wikimedia Commons (public MediaWiki search API, no auth) ────────────

async def wikimedia_commons_search_download(query: str, out_tmpl: str, logger=None):
    """Searches Commons' File namespace for audio files (ogg/opus/flac —
    Commons doesn't host mp3 due to patent history) and re-encodes the
    best match to mp3 for consistent playback."""
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None

    def _resolve():
        try:
            resp = requests.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srnamespace": 6,  # File namespace
                    "srsearch": f'{query} filetype:audio',
                    "srlimit": 5,
                    "format": "json",
                },
                timeout=10,
                headers={"User-Agent": random_ua()},
            )
            hits = ((resp.json() or {}).get("query") or {}).get("search") or []
            for hit in hits:
                title = hit.get("title", "")
                if not re.search(r"\.(ogg|oga|opus|flac|wav)$", title, re.IGNORECASE):
                    continue
                info_resp = requests.get(
                    "https://commons.wikimedia.org/w/api.php",
                    params={
                        "action": "query", "titles": title,
                        "prop": "imageinfo", "iiprop": "url",
                        "format": "json",
                    },
                    timeout=10,
                    headers={"User-Agent": random_ua()},
                )
                pages = ((info_resp.json() or {}).get("query") or {}).get("pages") or {}
                for page in pages.values():
                    imageinfo = page.get("imageinfo") or []
                    if imageinfo and imageinfo[0].get("url"):
                        return title.replace("File:", ""), imageinfo[0]["url"]
            return None
        except Exception as e:
            logger("MUSIC_DL_ERR", f"Wikimedia Commons search failed: {e}")
            return None

    try:
        resolved = await asyncio.to_thread(_resolve)
        if not resolved:
            return None
        title, url = resolved
        raw_ext = url.rsplit(".", 1)[-1].lower() or "bin"
        raw_path = out_tmpl.replace("%(ext)s", f"wm_{raw_ext}")

        def _fetch():
            with requests.get(url, stream=True, timeout=60,
                               headers={"User-Agent": random_ua()}) as r:
                r.raise_for_status()
                with open(raw_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)

        await asyncio.to_thread(_fetch)
        mp3_path = out_tmpl.replace("%(ext)s", "mp3")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", raw_path, "-vn", "-ar", "44100", "-ac", "2",
            "-b:a", "320k", mp3_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await proc.wait()
        try:
            os.remove(raw_path)
        except Exception:
            pass
        if not os.path.exists(mp3_path):
            return None
        duration = await _probe_duration(mp3_path)
        return {
            "title": title,
            "file_path": mp3_path,
            "duration": duration,
            "thumbnail": None,
            "source": "wikimedia_commons",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"Wikimedia Commons fallback failed: {e}")
        return None


# ── Openverse (public CC-licensed audio search API, no auth) ────────────

async def openverse_search_download(query: str, out_tmpl: str, logger=None):
    """Openverse aggregates CC-licensed audio (Jamendo, Free Music Archive,
    WFMU, etc.) behind one search API and gives a direct file URL back."""
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None

    def _resolve():
        try:
            resp = requests.get(
                "https://api.openverse.org/v1/audio/",
                params={"q": query, "page_size": 1},
                timeout=10,
                headers={"User-Agent": random_ua()},
            )
            results = (resp.json() or {}).get("results") or []
            if not results:
                return None
            item = results[0]
            url = item.get("url")
            if not url:
                return None
            return item
        except Exception as e:
            logger("MUSIC_DL_ERR", f"Openverse search failed: {e}")
            return None

    try:
        item = await asyncio.to_thread(_resolve)
        if not item:
            return None
        url = item["url"]
        raw_ext = url.rsplit(".", 1)[-1].split("?")[0].lower() or "bin"
        raw_path = out_tmpl.replace("%(ext)s", f"ov_{raw_ext}")

        def _fetch():
            with requests.get(url, stream=True, timeout=60,
                               headers={"User-Agent": random_ua()}) as r:
                r.raise_for_status()
                with open(raw_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)

        await asyncio.to_thread(_fetch)
        mp3_path = out_tmpl.replace("%(ext)s", "mp3")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", raw_path, "-vn", "-ar", "44100", "-ac", "2",
            "-b:a", "320k", mp3_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await proc.wait()
        try:
            os.remove(raw_path)
        except Exception:
            pass
        if not os.path.exists(mp3_path):
            return None
        duration = await _probe_duration(mp3_path)
        title = item.get("title") or query
        creator = item.get("creator")
        return {
            "title": f"{title} - {creator}" if creator else title,
            "file_path": mp3_path,
            "duration": duration or int(item.get("duration") or 0) // 1000,
            "thumbnail": None,
            "source": "openverse",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"Openverse fallback failed: {e}")
        return None


# ── HearThis.at (public REST search API, no auth) ───────────────────────

async def hearthis_search_download(query: str, out_tmpl: str, logger=None):
    """HearThis.at is an open, cookie-free streaming site (mixes, podcasts,
    original tracks). Its public search API returns a direct stream_url we
    can pull straight through ffmpeg."""
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None

    def _resolve():
        try:
            resp = requests.get(
                "https://api-v2.hearthis.at/search/",
                params={"t": query},
                timeout=10,
                headers={"User-Agent": random_ua()},
            )
            results = resp.json() or []
            if isinstance(results, dict):
                results = results.get("results") or []
            for item in results:
                url = item.get("stream_url") or item.get("download_url")
                if url:
                    return item, url
            return None
        except Exception as e:
            logger("MUSIC_DL_ERR", f"HearThis.at search failed: {e}")
            return None

    try:
        resolved = await asyncio.to_thread(_resolve)
        if not resolved:
            return None
        item, url = resolved
        mp3_path = out_tmpl.replace("%(ext)s", "mp3")
        raw_path = out_tmpl.replace("%(ext)s", "ht_raw")

        def _fetch():
            with requests.get(url, stream=True, timeout=60,
                               headers={"User-Agent": random_ua()}) as r:
                r.raise_for_status()
                with open(raw_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)

        await asyncio.to_thread(_fetch)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", raw_path, "-vn", "-ar", "44100", "-ac", "2",
            "-b:a", "320k", mp3_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await proc.wait()
        try:
            os.remove(raw_path)
        except Exception:
            pass
        if not os.path.exists(mp3_path):
            return None
        duration = int(item.get("duration") or 0) or await _probe_duration(mp3_path)
        title  = item.get("title") or query
        artist = item.get("user_name") or (item.get("user") or {}).get("username")
        return {
            "title": f"{title} - {artist}" if artist else title,
            "file_path": mp3_path,
            "duration": duration,
            "thumbnail": item.get("thumb") or item.get("artwork_url"),
            "source": "hearthis.at",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"HearThis.at fallback failed: {e}")
        return None


# ── ccMixter (public JSON query API, no auth) ────────────────────────────

async def ccmixter_search_download(query: str, out_tmpl: str, logger=None):
    """ccMixter hosts CC-licensed remixes/samples with a free, key-free
    JSON query API that returns a direct download URL per track."""
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None

    def _resolve():
        try:
            resp = requests.get(
                "http://ccmixter.org/api/query",
                params={"f": "json", "search": query, "limit": 1, "sort": "rank"},
                timeout=(10, 25),   # (connect, read) — ccMixter reads slowly
                headers={"User-Agent": random_ua()},
            )
            if not resp.content or resp.status_code != 200:
                return None
            try:
                results = resp.json() or []
            except Exception:
                return None
            if not results:
                return None
            item = results[0]
            files = item.get("files") or []
            url = None
            if files and isinstance(files, list):
                url = files[0].get("download_url")
            url = url or item.get("file_page_url")
            if not url:
                return None
            return item, url
        except Exception as e:
            logger("MUSIC_DL_ERR", f"ccMixter search failed: {e}")
            return None

    try:
        resolved = await asyncio.to_thread(_resolve)
        if not resolved:
            return None
        item, url = resolved
        mp3_path = out_tmpl.replace("%(ext)s", "mp3")
        raw_path = out_tmpl.replace("%(ext)s", "cc_raw")

        def _fetch():
            # ccMixter's CDN hotlink-protects file downloads — a bare request
            # with no Referer (or one that doesn't look like ccmixter.org)
            # gets a 403 even though the exact same URL works fine from a
            # browser tab that was navigated there from the site itself.
            headers = {
                "User-Agent": random_ua(),
                "Referer": "http://ccmixter.org/",
                "Accept": "*/*",
            }
            with requests.get(url, stream=True, timeout=60,
                               headers=headers, allow_redirects=True) as r:
                r.raise_for_status()
                with open(raw_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)

        await asyncio.to_thread(_fetch)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", raw_path, "-vn", "-ar", "44100", "-ac", "2",
            "-b:a", "320k", mp3_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await proc.wait()
        try:
            os.remove(raw_path)
        except Exception:
            pass
        if not os.path.exists(mp3_path):
            return None
        title = item.get("upload_name") or query
        artist = item.get("user_name")
        return {
            "title": f"{title} - {artist}" if artist else title,
            "file_path": mp3_path,
            "duration": await _probe_duration(mp3_path),
            "thumbnail": item.get("small_image_url") or item.get("image_url"),
            "source": "ccmixter",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"ccMixter fallback failed: {e}")
        return None


# ── Musopen / public-domain classical (via Internet Archive collection) ─

async def musopen_collection_search_download(query: str, out_tmpl: str, logger=None):
    """Musopen doesn't expose a documented public search API, but its own
    public-domain classical catalogue is mirrored inside Internet Archive's
    `musopen` collection — searching that collection is the reliable,
    key-free way to reach the same royalty-free recordings."""
    logger = logger or (lambda tag, msg: None)
    return await archive_org_search_download(
        f'{query} AND collection:musopen', out_tmpl, logger)


# ── Pixabay Music (public API — optional, only if PIXABAY_API_KEY is set) ─

async def pixabay_music_search_download(query: str, out_tmpl: str, logger=None):
    logger = logger or (lambda tag, msg: None)
    api_key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if requests is None or not api_key:
        return None

    def _resolve():
        try:
            resp = requests.get(
                "https://pixabay.com/api/videos/",  # music search lives on the same
                params={"key": api_key, "q": query},
                timeout=10,
            )
            return resp.json()
        except Exception as e:
            logger("MUSIC_DL_ERR", f"Pixabay search failed: {e}")
            return None

    # Pixabay's public API does not currently expose a dedicated `/music/`
    # endpoint for third-party keys; kept as a defensive optional hook —
    # returns None cleanly instead of raising if unsupported for this key.
    try:
        data = await asyncio.to_thread(_resolve)
        if not data or not data.get("hits"):
            return None
        return None
    except Exception as e:
        logger("MUSIC_DL_ERR", f"Pixabay fallback failed: {e}")
        return None


# ── Generic link resolution (Vocaroo/Catbox/Pixeldrain/Gofile/GDrive/─────
# ── Dropbox/GitHub raw+releases/HLS/Icecast/Shoutcast/Radio/Podcast RSS/──
# ── any public URL) ───────────────────────────────────────────────────────

_YOUTUBE_HOST_RE = re.compile(
    r"(?:^|\.)(?:youtube\.com|youtu\.be|youtube-nocookie\.com|music\.youtube\.com)$",
    re.IGNORECASE,
)


def is_youtube_url(text: str) -> bool:
    """True for any youtube.com / youtu.be link. YouTube support has been
    intentionally removed from this bot (login/cookie walls made it
    unreliable) — links to it are rejected up front instead of being
    silently attempted and failing."""
    if not is_url(text):
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(text.strip()).hostname or "").lower()
        return bool(_YOUTUBE_HOST_RE.search(host))
    except Exception:
        return False


def normalize_share_link(url: str) -> str:
    """Rewrites common cloud-storage 'share' links into their direct
    download form so ffmpeg/requests can pull the raw bytes without a
    browser session:
      • Google Drive  .../file/d/<id>/view      -> uc?export=download&id=<id>
      • Dropbox       ...dropbox.com/s/<path>   -> ?dl=1 (forces raw bytes)
    """
    u = url.strip()
    try:
        m = re.search(r"drive\.google\.com/file/d/([\w-]+)", u)
        if m:
            return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
        m = re.search(r"drive\.google\.com/open\?id=([\w-]+)", u)
        if m:
            return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
        if "dropbox.com" in u:
            if "dl=0" in u:
                return u.replace("dl=0", "dl=1")
            if "dl=1" not in u:
                sep = "&" if "?" in u else "?"
                return f"{u}{sep}dl=1"
    except Exception:
        pass
    return u


async def podcast_rss_resolve(url: str, logger=None):
    """If `url` is a podcast RSS feed, returns the first <enclosure> audio
    URL found in it, else None. Uses only the standard library so no extra
    dependency is required."""
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None

    def _resolve():
        import xml.etree.ElementTree as ET
        try:
            resp = requests.get(url, timeout=12, headers={"User-Agent": random_ua()})
            ctype = (resp.headers.get("content-type") or "").lower()
            body = resp.text
            looks_like_feed = (
                "xml" in ctype or "rss" in ctype or
                body.lstrip()[:5] == "<?xml" or "<rss" in body[:2000]
            )
            if not looks_like_feed:
                return None
            root = ET.fromstring(resp.content)
            for item in root.iter("item"):
                enclosure = item.find("enclosure")
                if enclosure is not None and enclosure.get("url"):
                    title_el = item.find("title")
                    title = title_el.text if title_el is not None else None
                    return enclosure.get("url"), title
            return None
        except Exception as e:
            logger("MUSIC_DL_ERR", f"Podcast RSS parse failed: {e}")
            return None

    return await asyncio.to_thread(_resolve)


async def github_release_asset_url(url: str, logger=None):
    """If `url` points at a GitHub releases page (not a raw asset link
    already), resolve it to the first release asset's direct download URL
    via GitHub's public, key-free REST API."""
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None
    m = re.search(r"github\.com/([^/]+)/([^/]+)/releases(?:/tag/([^/?#]+))?", url)
    if not m:
        return None
    owner, repo, tag = m.group(1), m.group(2), m.group(3)

    def _resolve():
        try:
            api = (f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
                   if tag else
                   f"https://api.github.com/repos/{owner}/{repo}/releases/latest")
            resp = requests.get(api, timeout=10, headers={"User-Agent": random_ua()})
            data = resp.json() or {}
            assets = data.get("assets") or []
            for a in assets:
                name = (a.get("name") or "").lower()
                if re.search(r"\.(mp3|m4a|wav|flac|ogg|opus|mp4|mkv)$", name):
                    return a.get("browser_download_url")
            if assets:
                return assets[0].get("browser_download_url")
            return None
        except Exception as e:
            logger("MUSIC_DL_ERR", f"GitHub release resolve failed: {e}")
            return None

    return await asyncio.to_thread(_resolve)


async def generic_yt_dlp_url_download(url: str, out_tmpl: str, ydl_common: dict,
                                       logger=None, want_video: bool = False):
    """Last-resort link handler: hands the URL to yt-dlp's own extractor
    matching (Vocaroo, Pixeldrain, Gofile, Catbox, Audiomack, and any other
    of the 1800+ sites yt-dlp natively understands) *except* YouTube, which
    is explicitly refused above this call. No login/cookies are used."""
    logger = logger or (lambda tag, msg: None)
    if yt_dlp is None or is_youtube_url(url):
        return None

    def _download():
        opts = {
            **ydl_common,
            "outtmpl": out_tmpl,
            "noplaylist": True,
        }
        if want_video:
            opts["format"] = "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best"
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = "bestaudio/best"
            opts.update(_audio_pp("mp3", "320"))
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if isinstance(info, dict) and "entries" in info:
                entries = [e for e in info["entries"] if e]
                if not entries:
                    return None
                info = entries[0]
            return info

    try:
        info = await asyncio.to_thread(_download)
        ext = "mp4" if want_video else "mp3"
        fpath = out_tmpl.replace("%(ext)s", ext)
        if not info or not os.path.exists(fpath):
            return None
        return {
            "title": info.get("title") or url,
            "file_path": fpath,
            "duration": int(info.get("duration") or 0),
            "thumbnail": info.get("thumbnail"),
            "source": "link",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"Generic link resolve failed: {e}")
        return None


async def resolve_link(url: str, out_tmpl: str, ydl_common: dict, logger=None,
                        want_video: bool = False):
    """Unified 'paste any link and it plays' resolver used for both
    `.play <link>` and `.vplay <link>`. Order of attempts:
      1. Reject YouTube outright (removed source — see is_youtube_url).
      2. Normalize known share-link formats (Google Drive / Dropbox).
      3. Podcast RSS feed? -> resolve to its first audio enclosure.
      4. GitHub releases page? -> resolve to the first asset's raw URL.
      5. Pull it straight through ffmpeg (covers direct mp3/mp4/CDN links,
         GitHub raw, HLS .m3u8, Icecast/Shoutcast/radio streams, and the
         normalized Drive/Dropbox links from step 2).
      6. Fall back to yt-dlp's own extractor matching (Vocaroo, Pixeldrain,
         Gofile, Catbox, Audiomack, SoundCloud/Bandcamp/Mixcloud links
         pasted directly, Internet Archive links, etc.) — still no
         YouTube, enforced a second time inside the helper itself.
    Returns a dict (title/file_path/duration/thumbnail/source) or None.
    """
    logger = logger or (lambda tag, msg: None)
    if is_youtube_url(url):
        return {"error": "youtube_unsupported"}

    url = normalize_share_link(url)

    rss = await podcast_rss_resolve(url, logger)
    if rss:
        audio_url, title = rss
        direct = await download_direct_media(audio_url, out_tmpl, logger)
        if direct:
            t, fpath, duration = direct
            return {"title": title or t, "file_path": fpath, "duration": duration,
                     "thumbnail": None, "source": "podcast_rss"}

    gh_asset = await github_release_asset_url(url, logger)
    if gh_asset:
        direct = await download_direct_media(gh_asset, out_tmpl, logger)
        if direct:
            t, fpath, duration = direct
            return {"title": t, "file_path": fpath, "duration": duration,
                     "thumbnail": None, "source": "github_release"}

    direct = await download_direct_media(url, out_tmpl, logger)
    if direct:
        t, fpath, duration = direct
        return {"title": t, "file_path": fpath, "duration": duration,
                 "thumbnail": None, "source": "direct_link"}

    return await generic_yt_dlp_url_download(url, out_tmpl, ydl_common, logger,
                                               want_video=want_video)


# ── iTunes metadata refine (public, no auth) ────────────────────────────

async def refine_query_via_itunes(query: str, logger=None):
    """Cleans up a messy query ('believer song imagine dragons lyrics') into
    a canonical 'Track - Artist' string using Apple's public, key-free
    iTunes Search API. This mirrors the "metadata search -> alternate
    source match" step from the fallback spec: we only ever use this to
    build a cleaner search string for the sources above, never to stream
    from Apple/Spotify/Deezer directly (that requires paid, authenticated
    APIs)."""
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None

    def _resolve():
        try:
            resp = requests.get(
                "https://itunes.apple.com/search",
                params={"term": query, "media": "music", "limit": 1},
                timeout=8,
            )
            results = (resp.json() or {}).get("results") or []
            if not results:
                return None
            r0 = results[0]
            track = r0.get("trackName")
            artist = r0.get("artistName")
            if track and artist:
                return f"{track} {artist}"
            return None
        except Exception as e:
            logger("MUSIC_DL_ERR", f"iTunes metadata refine failed: {e}")
            return None

    return await asyncio.to_thread(_resolve)


# ── JioSaavn (public search API, no auth — jugad #8) ────────────────────
# saavn.dev is a well-known community-run wrapper around JioSaavn's own
# (undocumented, key-less) search/song endpoints. It's a strong source for
# Hindi/Bollywood/Indian-regional tracks that YouTube/SoundCloud/Bandcamp
# often don't have clean matches for.
_JIOSAAVN_API_HOSTS = [
    # saavn.dev removed — DNS failing on Heroku as of Jul 2026
    "https://jiosaavn-api-two-nu.vercel.app/api",
    "https://jiosaavn-api-eta.vercel.app/api",
    "https://saavn-api.vercel.app/api",
    "https://jiosaavn-api-five.vercel.app/api",
    "https://jiosaavn-api-sigma.vercel.app/api",
    "https://jiosaavn-api-nu.vercel.app/api",
    "https://jiosaavn-api-production.up.railway.app/api",
]


async def jiosaavn_search_download(query: str, out_tmpl: str, logger=None):
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None

    def _resolve():
        for host in _JIOSAAVN_API_HOSTS:
            try:
                resp = requests.get(
                    f"{host}/search/songs",
                    params={"query": query, "limit": 1},
                    headers={"User-Agent": random_ua()},
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue
                body    = resp.json() or {}
                results = ((body.get("data") or {}).get("results")
                           or (body.get("data") or {}).get("songs") or [])
                if not results:
                    continue
                song = results[0]
                # downloadUrl is a list of {quality, url} — pick the highest.
                dl_list = song.get("downloadUrl") or song.get("download_url") or []
                if not dl_list:
                    continue
                stream_url = dl_list[-1].get("url") or dl_list[-1].get("link")
                if not stream_url:
                    continue
                title = song.get("name") or song.get("song") or query
                return song, title, stream_url
            except Exception as e:
                logger("MUSIC_DL_ERR", f"JioSaavn host {host} failed: {e}")
                continue
        return None

    try:
        resolved = await asyncio.to_thread(_resolve)
        if not resolved:
            return None
        song, title, stream_url = resolved

        raw_path = out_tmpl.replace("%(ext)s", "jiosaavn_raw.m4a")
        mp3_path = out_tmpl.replace("%(ext)s", "mp3")

        def _fetch():
            with requests.get(stream_url, stream=True, timeout=120,
                               headers={"User-Agent": random_ua()},
                               allow_redirects=True) as r:
                r.raise_for_status()
                with open(raw_path, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            fh.write(chunk)

        await asyncio.to_thread(_fetch)
        if not os.path.exists(raw_path) or os.path.getsize(raw_path) < 4096:
            return None

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", raw_path,
            "-fflags", "nobuffer", "-analyzeduration", "0",
            "-vn", "-ar", "44100", "-ac", "2", "-b:a", "320k", mp3_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        with contextlib.suppress(Exception):
            os.remove(raw_path)
        if not os.path.exists(mp3_path):
            return None

        duration = await _probe_duration(mp3_path)
        return {
            "title":     title,
            "file_path": mp3_path,
            "duration":  duration,
            "thumbnail": (song.get("image") or [{}])[-1].get("url")
                         if isinstance(song.get("image"), list) else song.get("image"),
            "source":    "jiosaavn",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"JioSaavn fallback failed: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# BACKUP SOURCE #1 — iTunes / Apple Music preview
# ──────────────────────────────────────────────────────────────────────────────
# Completely free, no API key required. Apple's iTunes Search API is public and
# unrestricted. Every result includes a 30-second AAC preview URL that is served
# directly from Apple's CDN — no login, no cookies, no bot-check.
# Coverage: virtually every mainstream song ever commercially released worldwide,
# including all Bollywood, Hindi, and Indian regional tracks.
# Limitation: 30-second previews only (not full songs). Used as a reliable
# fallback when every full-song source fails — 30 s is enough to demonstrate
# a match and gives the user something to listen to right away.

async def itunes_preview_search_download(query: str, out_tmpl: str, logger=None) -> dict | None:
    """Apple iTunes Search API — free, no key, global. Returns 30-second AAC
    preview. Best-in-class coverage for Bollywood and all commercial music."""
    logger = logger or (lambda *a: None)
    try:
        import urllib.parse as _up
        search_url = (
            "https://itunes.apple.com/search"
            f"?term={_up.quote(query)}&entity=song&country=in&limit=5&explicit=Yes"
        )

        def _resolve():
            try:
                resp = requests.get(search_url, timeout=10)
                resp.raise_for_status()
                results = resp.json().get("results", [])
                if not results:
                    return None
                # Pick the result whose trackName best matches the query
                best = max(
                    results,
                    key=lambda r: token_match_score(query, r.get("trackName", "")),
                    default=None,
                )
                if not best:
                    return None
                preview_url = best.get("previewUrl")
                if not preview_url:
                    return None
                return {
                    "title": f"{best.get('artistName','')} — {best.get('trackName','')}".strip(" —"),
                    "preview_url": preview_url,
                    "duration": int(best.get("trackTimeMillis", 0) / 1000) or 30,
                    "thumbnail": best.get("artworkUrl100"),
                }
            except Exception as e:
                logger("MUSIC_DL_ERR", f"itunes_resolve: {e}")
                return None

        meta = await asyncio.wait_for(asyncio.to_thread(_resolve), timeout=12)
        if not meta:
            return None

        def _fetch():
            try:
                raw_path = out_tmpl.replace("%(ext)s", "itunes_raw.m4a")
                resp = requests.get(meta["preview_url"], timeout=20, stream=True)
                resp.raise_for_status()
                with open(raw_path, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        if chunk:
                            f.write(chunk)
                # Re-encode to mp3 for consistency
                mp3_path = out_tmpl.replace("%(ext)s", "itunes.mp3")
                subprocess.run(
                    ["ffmpeg", "-y", "-i", raw_path,
                     "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k", mp3_path],
                    capture_output=True, timeout=30,
                )
                with contextlib.suppress(OSError):
                    os.remove(raw_path)
                if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 2048:
                    return mp3_path
                return None
            except Exception as e:
                logger("MUSIC_DL_ERR", f"itunes_fetch: {e}")
                return None

        file_path = await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=35)
        if not file_path:
            return None
        logger("MUSIC_DL", f"iTunes preview ✓ {meta['title']!r}")
        return {
            "title":     meta["title"],
            "file_path": file_path,
            "duration":  meta["duration"],
            "thumbnail": meta["thumbnail"],
            "source":    "itunes",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"itunes_outer: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# BACKUP SOURCE #2 — Deezer preview
# ──────────────────────────────────────────────────────────────────────────────
# Deezer's public REST API requires no authentication for search. Every track
# result includes a 30-second MP3 preview URL served from Deezer's own CDN.
# Strong global catalog; complements iTunes by providing a second independent
# 30-second fallback from a different CDN.

async def deezer_preview_search_download(query: str, out_tmpl: str, logger=None) -> dict | None:
    """Deezer public API — free, no key, 30-second MP3 previews, global catalog."""
    logger = logger or (lambda *a: None)
    if requests is None:
        return None
    try:
        import urllib.parse as _up
        search_url = (
            f"https://api.deezer.com/search?q={_up.quote(query)}"
            "&limit=5&output=json"
        )

        def _resolve():
            try:
                resp = requests.get(search_url, timeout=10)
                resp.raise_for_status()
                data = resp.json().get("data", [])
                if not data:
                    return None
                best = max(
                    data,
                    key=lambda r: token_match_score(
                        query, f"{r.get('artist',{}).get('name','')} {r.get('title','')}"
                    ),
                    default=None,
                )
                if not best:
                    return None
                preview = best.get("preview")
                if not preview:
                    return None
                artist = best.get("artist", {}).get("name", "")
                title  = best.get("title", query)
                thumb  = best.get("album", {}).get("cover_medium")
                return {"title": f"{artist} — {title}".strip(" —"),
                        "preview_url": preview,
                        "duration": int(best.get("duration", 30)),
                        "thumbnail": thumb}
            except Exception as e:
                logger("MUSIC_DL_ERR", f"deezer_resolve: {e}")
                return None

        meta = await asyncio.wait_for(asyncio.to_thread(_resolve), timeout=12)
        if not meta:
            return None

        def _fetch():
            try:
                mp3_path = out_tmpl.replace("%(ext)s", "deezer.mp3")
                resp = requests.get(meta["preview_url"], timeout=20, stream=True)
                resp.raise_for_status()
                with open(mp3_path, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        if chunk:
                            f.write(chunk)
                if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 2048:
                    return mp3_path
                return None
            except Exception as e:
                logger("MUSIC_DL_ERR", f"deezer_fetch: {e}")
                return None

        file_path = await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=30)
        if not file_path:
            return None
        logger("MUSIC_DL", f"Deezer preview ✓ {meta['title']!r}")
        return {
            "title":     meta["title"],
            "file_path": file_path,
            "duration":  meta["duration"],
            "thumbnail": meta["thumbnail"],
            "source":    "deezer",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"deezer_outer: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# BACKUP SOURCE #3 — Audiomack
# ──────────────────────────────────────────────────────────────────────────────
# Audiomack is a free streaming platform (no subscription, no login for public
# tracks). Their public search API returns direct audio stream URLs. Best for
# hip-hop, Afrobeats, R&B, and Bhangra remixes — genres often missing from
# JioSaavn's library. Stream URLs are served from their own CDN.

# Audiomack's v1 API requires OAuth since 2024; use their oembed/search instead.
# We fall back to their public search page API (used by their own web app).
_AUDIOMACK_API_SEARCH = "https://api.audiomack.com/v1/music/search"   # keep for 401/403 detection
_AUDIOMACK_API_OEMBED = "https://www.audiomack.com/api/search"

async def audiomack_search_download(query: str, out_tmpl: str, logger=None) -> dict | None:
    """Audiomack — free, public API, no login. Full-length songs, strong catalog
    for hip-hop / Afrobeats / R&B / Bhangra."""
    logger = logger or (lambda *a: None)
    if requests is None:
        return None
    try:
        # Audiomack's v1 API now requires OAuth (returns 404/401).
        # Use their web app's internal API endpoint instead.
        headers = {
            "Accept": "application/json",
            "User-Agent": random_ua(),
            "Referer": "https://audiomack.com/",
            "Origin": "https://audiomack.com",
        }

        def _resolve():
            tried = [
                # ① Web app internal API (no auth needed)
                (f"https://audiomack.com/api/music/search"
                 f"?q={_urlparse.quote(query)}&limit=5&type=song", "results"),
                # ② Older v2 endpoint
                (f"https://api.audiomack.com/v2/music/search"
                 f"?q={_urlparse.quote(query)}&limit=5&type=song", "results"),
                # ③ Original v1 (may work for some regions)
                (f"https://api.audiomack.com/v1/music/search"
                 f"?q={_urlparse.quote(query)}&type=song&limit=5&content_type=1", "results"),
            ]
            for url, results_key in tried:
                try:
                    resp = requests.get(url, headers=headers, timeout=10)
                    if resp.status_code in (401, 403, 404):
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    results = (data.get(results_key) or
                               data.get("data") or
                               data.get("collection") or [])
                    if not results:
                        continue
                    best = max(
                        results,
                        key=lambda r: token_match_score(
                            query, f"{r.get('artist','')} {r.get('title','')}"
                        ),
                        default=None,
                    )
                    if not best:
                        best = results[0]
                    stream = (best.get("stream_url") or best.get("audio_url") or
                              best.get("hls_url") or best.get("url"))
                    if not stream:
                        continue
                    return {
                        "title": f"{best.get('artist','')} — {best.get('title',query)}".strip(" —"),
                        "stream_url": stream,
                        "duration": int(best.get("duration") or 0),
                        "thumbnail": best.get("image") or best.get("artwork"),
                    }
                except Exception as e:
                    logger("MUSIC_DL_ERR", f"audiomack_resolve [{url[:40]}]: {e}")
                    continue
            return None

        meta = await asyncio.wait_for(asyncio.to_thread(_resolve), timeout=12)
        if not meta:
            return None

        def _fetch():
            try:
                mp3_path = out_tmpl.replace("%(ext)s", "audiomack.mp3")
                resp = requests.get(
                    meta["stream_url"], headers=headers, timeout=30, stream=True
                )
                resp.raise_for_status()
                with open(mp3_path, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        if chunk:
                            f.write(chunk)
                if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 10240:
                    return mp3_path
                return None
            except Exception as e:
                logger("MUSIC_DL_ERR", f"audiomack_fetch: {e}")
                return None

        file_path = await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=40)
        if not file_path:
            return None
        logger("MUSIC_DL", f"Audiomack ✓ {meta['title']!r}")
        return {
            "title":     meta["title"],
            "file_path": file_path,
            "duration":  meta["duration"],
            "thumbnail": meta["thumbnail"],
            "source":    "audiomack",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"audiomack_outer: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# BACKUP SOURCE #4 — Dailymotion
# ──────────────────────────────────────────────────────────────────────────────
# Dailymotion has millions of music videos, including Bollywood, classic Hindi,
# and international tracks. yt-dlp has a robust Dailymotion extractor that
# works reliably from datacenter/cloud IPs — no bot-check like YouTube.
# Search uses `dmsearch1:` prefix (yt-dlp built-in, like ytsearch1: for YT).

async def dailymotion_search_download(query: str, out_tmpl: str, logger=None) -> dict | None:
    """Dailymotion — yt-dlp dmsearch, music videos, works from cloud IPs.
    Good for Bollywood video songs, classic Hindi, and international tracks."""
    logger = logger or (lambda *a: None)
    if yt_dlp is None:
        return None
    try:
        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best[height<=480]",
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "geo_bypass": True,
            "socket_timeout": 20,
            "retries": 2,
            "noplaylist": True,
            "max_downloads": 1,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
            **_ffmpeg_opts(),
        }

        def _get_dm_video_id():
            """Use Dailymotion public API to search — `dmsearch1:` prefix is NOT
            a valid yt-dlp scheme; DM has no built-in yt-dlp search extractor."""
            if requests is None:
                return None, None
            try:
                resp = requests.get(
                    "https://api.dailymotion.com/videos",
                    params={
                        "search": query,
                        "fields": "id,title,duration",
                        "limit": 5,
                        "family_filter": "false",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                items = resp.json().get("list") or []
                if not items:
                    return None, None
                best = max(
                    items,
                    key=lambda v: token_match_score(query, v.get("title", "")),
                    default=None,
                )
                if not best:
                    best = items[0]
                if looks_like_live_or_reaction(best.get("title", ""), query):
                    items = [v for v in items
                             if not looks_like_live_or_reaction(v.get("title",""), query)]
                    best = items[0] if items else best
                return best["id"], best.get("title") or query
            except Exception:
                return None, None

        dm_id, dm_title = await asyncio.to_thread(_get_dm_video_id)
        if not dm_id:
            return None
        search_query = f"https://www.dailymotion.com/video/{dm_id}"

        def _download():
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(search_query, download=True)
                    if not info:
                        return None
                    if "entries" in info:
                        info = info["entries"][0] if info["entries"] else None
                    if not info:
                        return None
                    if looks_like_live_or_reaction(info.get("title", ""), query):
                        return None
                    filepath = ydl.prepare_filename(info)
                    for ext in ("mp3", "m4a", "opus", "ogg", "webm"):
                        alt = os.path.splitext(filepath)[0] + f".{ext}"
                        if os.path.exists(alt):
                            filepath = alt
                            break
                    if not os.path.exists(filepath):
                        return None
                    return {
                        "title":     info.get("title") or query,
                        "file_path": filepath,
                        "duration":  int(info.get("duration") or 0),
                        "thumbnail": info.get("thumbnail"),
                        "source":    "dailymotion",
                    }
            except Exception as e:
                logger("MUSIC_DL_ERR", f"dailymotion_dl: {e}")
                return None

        result = await asyncio.wait_for(asyncio.to_thread(_download), timeout=40)
        if result:
            logger("MUSIC_DL", f"Dailymotion ✓ {result['title']!r}")
        return result
    except Exception as e:
        logger("MUSIC_DL_ERR", f"dailymotion_outer: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# BACKUP SOURCE #5 — Gaana (Indian music platform)
# ──────────────────────────────────────────────────────────────────────────────
# Gaana is India's second-largest music streaming platform with 80M+ songs,
# especially strong for Hindi film (Bollywood), regional Indian, and Punjabi.
# Uses Gaana's mobile search API to find song URLs, then yt-dlp to extract
# the audio stream. geo_bypass is set to handle non-India IP restrictions.

async def gaana_search_download(query: str, out_tmpl: str, logger=None) -> dict | None:
    """Gaana — Indian platform, 80M songs, best for Bollywood / Hindi / Punjabi.
    Falls back gracefully if geo-blocked or API changes."""
    logger = logger or (lambda *a: None)
    if requests is None or yt_dlp is None:
        return None
    try:
        import urllib.parse as _up
        # Gaana's mobile/app search API (public, no auth)
        search_url = (
            "https://api.gaana.com"
            f"?type=search&subtype=multisearch&search_key={_up.quote(query)}"
            "&count=5&token=b2e6d7d5d7f7c8e7&platform=web"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
                "Chrome/91.0.4472.120 Mobile Safari/537.36"
            ),
            "Accept": "application/json",
        }

        def _resolve():
            try:
                resp = requests.get(search_url, headers=headers, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                songs = (
                    (data.get("song") or {}).get("child_data") or
                    (data.get("track") or {}).get("child_data") or
                    (data.get("playlist") or {}).get("child_data") or
                    []
                )
                if not songs:
                    return None
                best = max(
                    songs,
                    key=lambda s: token_match_score(
                        query, f"{s.get('name','')} {s.get('artist_name','')}"
                    ),
                    default=None,
                )
                if not best:
                    return None
                seo = best.get("seo_url") or best.get("custom_artis_seo")
                if not seo:
                    return None
                track_url = f"https://gaana.com/song/{seo.lstrip('/')}"
                return {
                    "title": f"{best.get('artist_name','')} — {best.get('name', query)}".strip(" —"),
                    "url": track_url,
                    "duration": int(best.get("duration") or 0),
                    "thumbnail": best.get("artwork"),
                }
            except Exception as e:
                logger("MUSIC_DL_ERR", f"gaana_resolve: {e}")
                return None

        meta = await asyncio.wait_for(asyncio.to_thread(_resolve), timeout=12)
        if not meta:
            return None

        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio",
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "geo_bypass": True,
            "socket_timeout": 20,
            "retries": 2,
            "noplaylist": True,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
            **_ffmpeg_opts(),
        }

        def _download():
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(meta["url"], download=True)
                    if not info:
                        return None
                    filepath = ydl.prepare_filename(info)
                    for ext in ("mp3", "m4a", "opus", "ogg"):
                        alt = os.path.splitext(filepath)[0] + f".{ext}"
                        if os.path.exists(alt):
                            filepath = alt
                            break
                    if not os.path.exists(filepath):
                        return None
                    return filepath
            except Exception as e:
                logger("MUSIC_DL_ERR", f"gaana_dl: {e}")
                return None

        file_path = await asyncio.wait_for(asyncio.to_thread(_download), timeout=40)
        if not file_path:
            return None
        logger("MUSIC_DL", f"Gaana ✓ {meta['title']!r}")
        return {
            "title":     meta["title"],
            "file_path": file_path,
            "duration":  meta["duration"],
            "thumbnail": meta["thumbnail"],
            "source":    "gaana",
        }
    except Exception as e:
        logger("MUSIC_DL_ERR", f"gaana_outer: {e}")
        return None


# ── Zero-disk streaming (jugad #5/#6) ───────────────────────────────────
# Everything above downloads the track to a local file first (via yt-dlp or
# a raw HTTP fetch), then hands the FILE PATH to PyTgCalls. That's proven
# and kept as-is for the long-tail fallback sources below (Bandcamp,
# Mixcloud, Archive.org, Wikimedia, etc.) since it already works well and
# nothing here should regress a working source. But for the two most common
# real-world cases — a normal song search, and a pasted direct link — disk
# I/O is pure overhead: Heroku's filesystem is ephemeral/slow, every
# download adds seconds of latency, and leftover files are a slow memory/
# disk leak if cleanup ever misses one. `resolve_zero_disk_stream()` below
# tries to get a playable **remote URL** straight from the source's own API
# (no download, no local file at all) and hands that URL directly to
# PyTgCalls' MediaStream, which opens it with ffmpeg exactly like a local
# file — ffmpeg's own reconnect/probesize flags (added in main.py) make that
# just as resilient to network hiccups as a downloaded file, without ever
# touching disk. This is tried FIRST; if it can't find a clean remote URL,
# the existing disk-based race below still runs as a safety net.

_LIVE_OR_REACTION_KEYWORDS = (
    "live", "livestream", "live stream", "24/7", "24-7", "streaming now",
    "reaction", "react to", "reacts to", "loop", "1 hour", "one hour",
    "10 hours", "10 hour", "nonstop loop", "on repeat",
)


def looks_like_live_or_reaction(title: str, query: str, allow_live: bool = False) -> bool:
    """Jugad #3 (part 2): filter out live streams / infinite loops / reaction
    videos that a plain text search on a public API will happily surface
    ahead of the actual studio track, UNLESS the user's own query explicitly
    asked for "live" (e.g. ".play arijit singh live concert") — in which
    case we assume they want exactly that and don't filter it out."""
    if not title:
        return False
    t = title.lower()
    q = (query or "").lower()
    if "live" in q or "reaction" in q or "loop" in q:
        allow_live = True
    if allow_live:
        return False
    return any(kw in t for kw in _LIVE_OR_REACTION_KEYWORDS)


def token_match_score(query: str, title: str) -> float:
    """Jugad #3 (part 1): cheap token-overlap ratio (0.0-1.0) used to reject
    results that share almost no words with what the user actually typed —
    e.g. a search API returning an unrelated "top hits mix" video/track as
    its top result. No external dependency (no fuzzy-match library needed);
    plain set overlap over normalized word tokens is enough to catch the
    obviously-wrong results without being so strict it rejects legitimate
    covers/remixes that share most of the title's words."""
    qt = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
    tt = set(re.findall(r"[a-z0-9]+", (title or "").lower()))
    if not qt or not tt:
        return 1.0  # can't score — don't block on missing data
    overlap = qt & tt
    return len(overlap) / max(1, len(qt))


async def zero_disk_piped_lookup(query: str, logger=None, allow_live: bool = False) -> dict | None:
    """Same search as `piped_search_download()` but returns the remote audio
    CDN URL directly instead of downloading it — zero disk writes."""
    logger = logger or (lambda *a: None)
    if _aiohttp is None:
        return None
    _timeout = _aiohttp.ClientTimeout(total=12)
    _hdrs    = {"User-Agent": random_ua()}
    for api in _live_hosts(await _get_piped_apis()):
        try:
            video_id = title = None
            duration, thumbnail = 0, None
            for flt in ("music_songs", "videos"):
                try:
                    async with _make_aiohttp_session() as sess:
                        async with sess.get(f"{api}/search", params={"q": query, "filter": flt},
                                             headers=_hdrs, timeout=_timeout) as r:
                            if r.status == 429 or r.status == 403:
                                logger("MUSIC_DL_ERR", f"Piped [{api}] rate-limited ({r.status}) — rotating instance")
                                break
                            if r.status != 200:
                                continue
                            data = await r.json(content_type=None)
                    items = [i for i in (data.get("items") or []) if i.get("type") in ("stream", "video", None)]
                    if not items:
                        continue
                    item    = items[0]
                    raw_url = item.get("url", "")
                    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", raw_url) or re.search(r"/([A-Za-z0-9_-]{11})$", raw_url)
                    if not m:
                        continue
                    video_id  = m.group(1)
                    title     = item.get("title", query)
                    duration  = int(item.get("duration") or 0)
                    thumbnail = item.get("thumbnail") or item.get("thumbnailUrl")
                    break
                except Exception:
                    continue
            if not video_id:
                continue
            if looks_like_live_or_reaction(title, query, allow_live) or token_match_score(query, title) < 0.15:
                logger("MUSIC_FILTER", f"Piped result filtered out (live/reaction/mismatch): {title!r}")
                continue
            async with _make_aiohttp_session() as sess:
                async with sess.get(f"{api}/streams/{video_id}", headers=_hdrs, timeout=_timeout) as r:
                    if r.status in (429, 403):
                        logger("MUSIC_DL_ERR", f"Piped [{api}] rate-limited on /streams ({r.status})")
                        continue
                    if r.status != 200:
                        continue
                    sd = await r.json(content_type=None)
            a_streams = sorted([s for s in (sd.get("audioStreams") or []) if s.get("url")],
                                key=lambda s: s.get("bitrate", 0), reverse=True)
            if not a_streams:
                continue
            logger("MUSIC_DL", f"Piped (zero-disk) ✓ [{api}] '{title[:50]}'")
            return {"title": sd.get("title") or title, "stream_url": a_streams[0]["url"],
                    "duration": int(sd.get("duration") or duration),
                    "thumbnail": sd.get("thumbnailUrl") or thumbnail, "source": "piped"}
        except Exception as exc:
            _mark_host_dead(api)
            logger("MUSIC_DL_ERR", f"zero_disk_piped [{api}]: {exc}")
            continue
    return None


async def zero_disk_invidious_lookup(query: str, logger=None, allow_live: bool = False) -> dict | None:
    """Invidious instance failover (jugad #1) — a second independent
    frontend network from Piped, tried only if Piped fully fails. Uses
    Invidious's own `/api/v1/search` + `/api/v1/videos/<id>` JSON endpoints
    to get a direct CDN audio URL with no download."""
    logger = logger or (lambda *a: None)
    if _aiohttp is None:
        return None
    _timeout = _aiohttp.ClientTimeout(total=10)
    for instance in _live_hosts(_INVIDIOUS):
        try:
            _hdrs = {"User-Agent": random_ua()}
            async with _make_aiohttp_session() as sess:
                async with sess.get(f"{instance}/api/v1/search",
                                     params={"q": query, "type": "video"},
                                     headers=_hdrs, timeout=_timeout) as r:
                    if r.status in (429, 403):
                        logger("MUSIC_DL_ERR", f"Invidious [{instance}] blocked ({r.status}) — rotating")
                        continue
                    if r.status != 200:
                        continue
                    results = await r.json(content_type=None)
            if not results:
                continue
            top = results[0]
            title = top.get("title") or query
            video_id = top.get("videoId")
            if not video_id:
                continue
            if looks_like_live_or_reaction(title, query, allow_live) or token_match_score(query, title) < 0.15:
                logger("MUSIC_FILTER", f"Invidious result filtered out (live/reaction/mismatch): {title!r}")
                continue
            async with _make_aiohttp_session() as sess:
                async with sess.get(f"{instance}/api/v1/videos/{video_id}",
                                     headers=_hdrs, timeout=_timeout) as r:
                    if r.status in (429, 403):
                        logger("MUSIC_DL_ERR", f"Invidious [{instance}] blocked on video fetch ({r.status})")
                        continue
                    if r.status != 200:
                        continue
                    vdata = await r.json(content_type=None)
            fmts = [f for f in (vdata.get("adaptiveFormats") or []) if "audio" in (f.get("type") or "")]
            if not fmts:
                continue
            fmts.sort(key=lambda f: int(f.get("bitrate") or 0), reverse=True)
            logger("MUSIC_DL", f"Invidious (zero-disk) ✓ [{instance}] '{title[:50]}'")
            return {"title": vdata.get("title") or title, "stream_url": fmts[0]["url"],
                    "duration": int(vdata.get("lengthSeconds") or 0),
                    "thumbnail": None, "source": "youtube"}
        except Exception as exc:
            _mark_host_dead(instance)
            logger("MUSIC_DL_ERR", f"zero_disk_invidious [{instance}]: {exc}")
            continue
    return None


async def zero_disk_jiosaavn_lookup(query: str, logger=None, allow_live: bool = False) -> dict | None:
    """Same JioSaavn search as `jiosaavn_search_download()`, but JioSaavn's
    own CDN already hands back a direct, publicly-fetchable .m4a/.mp3-style
    URL — there's nothing to download-then-reupload here at all, so this
    just returns that URL straight for zero-disk playback."""
    logger = logger or (lambda tag, msg: None)
    if requests is None:
        return None

    def _resolve():
        for host in _JIOSAAVN_API_HOSTS:
            try:
                resp = requests.get(f"{host}/search/songs", params={"query": query, "limit": 3},
                                     headers={"User-Agent": random_ua()}, timeout=3)
                if resp.status_code in (429, 403):
                    logger("MUSIC_DL_ERR", f"JioSaavn host {host} blocked ({resp.status_code})")
                    continue
                if resp.status_code != 200:
                    continue
                body    = resp.json() or {}
                results = ((body.get("data") or {}).get("results") or (body.get("data") or {}).get("songs") or [])
                for song in results:
                    title = song.get("name") or song.get("song") or query
                    if looks_like_live_or_reaction(title, query, allow_live) or token_match_score(query, title) < 0.15:
                        continue
                    dl_list = song.get("downloadUrl") or song.get("download_url") or []
                    if not dl_list:
                        continue
                    stream_url = dl_list[-1].get("url") or dl_list[-1].get("link")
                    if not stream_url:
                        continue
                    return song, title, stream_url
            except Exception as e:
                logger("MUSIC_DL_ERR", f"JioSaavn host {host} failed: {e}")
                continue
        return None

    resolved = await asyncio.to_thread(_resolve)
    if not resolved:
        return None
    song, title, stream_url = resolved
    logger("MUSIC_DL", f"JioSaavn (zero-disk) ✓ '{title[:50]}'")
    return {
        "title": title, "stream_url": stream_url, "duration": int(song.get("duration") or 0),
        "thumbnail": (song.get("image") or [{}])[-1].get("url") if isinstance(song.get("image"), list) else song.get("image"),
        "source": "jiosaavn",
    }


async def zero_disk_soundcloud_lookup(query: str, logger=None, allow_live: bool = False) -> dict | None:
    """SoundCloud zero-disk via direct SC API v2 (NOT yt-dlp scsearch).
    yt-dlp's `scsearch1:` relies on auto-extracting a client_id from SC's web
    JS — this breaks several times a year when SC updates their page.
    We use our own client_id extraction + SC API v2 directly, which is far
    more reliable and returns a direct progressive-MP3 stream URL."""
    logger = logger or (lambda *a: None)
    if requests is None:
        return None

    client_id = _sc_get_client_id(logger)
    if not client_id:
        logger("MUSIC_DL_ERR", "zero_disk_soundcloud: could not get SC client_id")
        return None

    def _resolve():
        try:
            resp = requests.get(
                "https://api-v2.soundcloud.com/search/tracks",
                params={"q": query, "client_id": client_id,
                        "limit": 5, "offset": 0, "linked_partitioning": 1},
                headers={"User-Agent": random_ua()}, timeout=10,
            )
            resp.raise_for_status()
            tracks = resp.json().get("collection") or []
            if not tracks:
                return None
            best = max(
                tracks,
                key=lambda t: token_match_score(
                    query,
                    f"{t.get('title','')} {(t.get('user') or {}).get('username','')}",
                ),
                default=None,
            )
            if not best:
                best = tracks[0]
            title = best.get("title") or query
            if (looks_like_live_or_reaction(title, query, allow_live) or
                    token_match_score(query, title) < 0.15):
                # try second result
                others = [t for t in tracks if t is not best]
                if others:
                    best  = others[0]
                    title = best.get("title") or query
                else:
                    return None
            # Find a progressive transcoding (direct mp3/m4a — no HLS)
            transcodings = (best.get("media") or {}).get("transcodings") or []
            prog = next(
                (t for t in transcodings
                 if (t.get("format") or {}).get("protocol") == "progressive"),
                None,
            )
            if not prog:
                prog = transcodings[0] if transcodings else None
            if not prog:
                return None
            tc_resp = requests.get(
                prog["url"],
                params={"client_id": client_id},
                headers={"User-Agent": random_ua()}, timeout=10,
            )
            tc_resp.raise_for_status()
            stream_url = tc_resp.json().get("url")
            if not stream_url:
                return None
            return {
                "title":      title,
                "stream_url": stream_url,
                "duration":   int(best.get("duration") or 0) // 1000,
                "thumbnail":  best.get("artwork_url"),
            }
        except Exception as e:
            logger("MUSIC_DL_ERR", f"zero_disk_soundcloud: {e}")
            return None

    result = await asyncio.to_thread(_resolve)
    if not result:
        return None
    title = result["title"]
    logger("MUSIC_DL", f"SoundCloud (zero-disk) ✓ '{title[:50]}'")
    return {"title": title, "stream_url": result["stream_url"],
            "duration": result["duration"], "thumbnail": result["thumbnail"],
            "source": "soundcloud"}



# ── Fast direct-stream resolver ─────────────────────────────────────────────
# Resolve a playable CDN URL without downloading the whole track.  This is
# deliberately bounded: a bad YouTube client must never hold .play hostage.
_DIRECT_STREAM_CLIENTS = ["web_safari", "web", "android"]
_DIRECT_STREAM_RETRIES = 1
_DIRECT_STREAM_TIMEOUT = 4.2
_MUSIC_DOWNLOAD_LOCK = asyncio.Lock()


def _direct_stream_extract_sync(target: str, clients: list, cookiefile=None):
    if yt_dlp is None:
        return None
    clients = yt_clients(clients)
    ext_args = {"youtube": {"player_client": clients, "formats": ["missing_pot"]}}
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "ignore_no_formats_error": True, "no_check_certificate": True,
        "skip_download": True, "socket_timeout": _DIRECT_STREAM_TIMEOUT,
        "retries": 1, "extractor_retries": 1, "fragment_retries": 1,
        "check_formats": False, "geo_bypass": True,
        "format": "bestaudio[protocol^=http]/bestaudio/best[protocol^=http]/best",
        "extractor_args": ext_args,
        "plugin_dirs": [],
        "http_headers": {"User-Agent": random_ua(), "Referer": "https://www.youtube.com/"},
        **(_get_js_runtime_opts() if "_get_js_runtime_opts" in globals() else {}),
        **(_get_impersonate_opt() if "_get_impersonate_opt" in globals() else {}),
    }
    if cookiefile and "_clients_accept_cookies" in globals() and _clients_accept_cookies(clients):
        opts["cookiefile"] = cookiefile
    class _DirectQuietLogger:
        def debug(self, msg): pass
        def warning(self, msg): pass
        def error(self, msg): pass
    opts["logger"] = _DirectQuietLogger()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
        if info and info.get("entries"):
            info = next((e for e in info["entries"] if e), None)
        if not info:
            return None
        formats = [f for f in (info.get("formats") or []) if f.get("url")]
        formats.sort(key=lambda f: (bool(f.get("acodec") not in (None, "none")),
                                    bool(f.get("vcodec") in (None, "none")),
                                    f.get("abr") or 0), reverse=True)
        chosen = formats[0] if formats else info
        url = chosen.get("url")
        if not url:
            return None
        return {"title": info.get("title") or target, "stream_url": url,
                "duration": int(info.get("duration") or 0),
                "thumbnail": info.get("thumbnail"), "source": "youtube-direct"}
    except Exception:
        return None



async def _direct_stream_is_live(url: str) -> bool:
    """Cheap preflight so PyTgCalls never receives a dead/HTML CDN URL."""
    try:
        import aiohttp as _ah
        timeout = _ah.ClientTimeout(total=0.8, connect=0.6, sock_read=0.6)
        headers = {"Range": "bytes=0-1", "User-Agent": random_ua(),
                   "Accept": "*/*", "Icy-MetaData": "1"}
        async with _ah.ClientSession(timeout=timeout, headers=headers) as sess:
            async with sess.get(url, allow_redirects=True) as resp:
                if resp.status not in (200, 206):
                    return False
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype or "application/json" in ctype:
                    return False
                chunk = await resp.content.read(2)
                return bool(chunk) or resp.headers.get("Content-Length") not in (None, "0")
    except Exception:
        return False

async def _direct_stream_is_playable(url: str) -> bool:
    """Validate that FFmpeg can decode real audio before PyTgCalls sees it."""
    if not url or not await _direct_stream_is_live(url):
        return False
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-v", "error", "-t", "0.35",
            "-i", url, "-map", "0:a:0?", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=2.2)
        return proc.returncode == 0
    except Exception:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return False

async def youtube_direct_stream(query: str, logger=None) -> dict | None:
    """Resolve a fresh audio CDN URL with real bounded retries.

    Every round races all direct clients in parallel. A transient yt-dlp,
    YouTube client, or CDN preflight failure gets a second independent round
    instead of silently becoming a one-shot miss.
    """
    logger = logger or (lambda *a: None)
    target = query.strip()
    if not target.startswith(("http://", "https://")):
        target = "ytsearch1:" + target
    base_clients = list(dict.fromkeys(_DIRECT_STREAM_CLIENTS))
    for attempt in range(1, _DIRECT_STREAM_RETRIES + 1):
        # Rotate the first client on retry so a sticky client failure does not
        # repeat the exact same ordering. All clients still race concurrently.
        shift = (attempt - 1) % len(base_clients) if base_clients else 0
        clients = base_clients[shift:] + base_clients[:shift]
        async def _one(client_name):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(_direct_stream_extract_sync, target, [client_name],
                                      globals().get("_YTDLP_COOKIE_FILE")),
                    timeout=_DIRECT_STREAM_TIMEOUT)
            except Exception:
                return None
        tasks = [asyncio.create_task(_one(c)) for c in clients]
        try:
            for task in asyncio.as_completed(tasks):
                result = await task
                if (result and result.get("stream_url")
                        and await _direct_stream_is_playable(result["stream_url"])):
                    logger("MUSIC_DIRECT",
                           f"direct audio ready on retry {attempt}: {result.get('title','')[:60]}")
                    return result
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if attempt < _DIRECT_STREAM_RETRIES:
            await asyncio.sleep(0.15)
    logger("MUSIC_DIRECT_MISS",
           f"direct audio unavailable after {_DIRECT_STREAM_RETRIES} retries: {query[:80]}")
    return None

async def resolve_zero_disk_stream(query: str, logger=None, allow_live: bool = False) -> dict | None:
    """Jugad #1/#2/#4/#5/#6/#10 orchestrator: try to get a directly-playable
    remote URL (no local file at all) before ever falling back to disk-based
    downloads.

    PARALLEL RACE — all zero-disk sources run simultaneously so the FASTEST
    winner is returned immediately.  JioSaavn (reliable Indian/Bollywood CDN)
    and Piped/Invidious (YouTube frontends) race each other; whichever returns
    first wins.  This eliminates the old sequential wait where Invidious
    blocking would eat the entire 6 s timeout before JioSaavn even started."""
    logger = logger or (lambda *a: None)

    # Direct .mp3/.mp4/.m3u8/etc links skip search entirely (jugad #4).
    if is_direct_media_url(query):
        if await _direct_stream_is_live(query):
            logger("MUSIC_DL", f"Direct media URL (zero-disk) ✓ {query[:80]}")
            return {"title": query.rsplit("/", 1)[-1][:60] or query, "stream_url": query,
                    "duration": 0, "thumbnail": None, "source": "direct"}
        logger("MUSIC_DL_ERR", f"Direct media URL failed preflight: {query[:80]}")
        return None

    async def _safe(fn):
        try:
            result = await fn(query, logger=logger, allow_live=allow_live)
            url = (result or {}).get("stream_url") if result else None
            # Every provider must pass the same cheap HTTP preflight. This
            # prevents a dead CDN URL from reaching PyTgCalls and gives the
            # caller a real fallback within the ten-second budget.
            if result and url and await _direct_stream_is_live(url):
                return result
            return None
        except Exception as exc:
            logger("MUSIC_DL_ERR", f"resolve_zero_disk_stream/{fn.__name__}: {exc}")
            return None

    # Race independent zero-disk CDNs. The first valid URL wins; a slow or
    # dead mirror cannot block the other providers from starting immediately.
    direct_tasks = {
        asyncio.create_task(_safe(fn)): fn.__name__
        for fn in (
            zero_disk_jiosaavn_lookup,   # ① fast Indian music CDN
            zero_disk_piped_lookup,       # ② YouTube via Piped frontend
            zero_disk_invidious_lookup,   # ③ YouTube via Invidious frontend
        )
    }
    pending = set(direct_tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                result = t.result()
                if result and result.get("stream_url"):
                    for p in pending:
                        p.cancel()
                    return result
    finally:
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return None


# ── Orchestrator ─────────────────────────────────────────────────────────

async def try_extra_sources(query: str, out_tmpl: str, logger=None):
    """Runs YouTube (5-client jugad) -> SoundCloud -> Bandcamp -> Mixcloud ->
    Audius -> Internet Archive -> Wikimedia Commons -> Openverse -> Jamendo in
    order. YouTube is tried first since it has the best coverage for popular
    songs. Returns a dict (title/file_path/duration/thumbnail/source) or None.
    Callers pass a fresh out_tmpl per attempt so failed attempts never collide."""
    from_ = [
        ("youtube", lambda: youtube_search_download(query, out_tmpl, logger)),
        ("soundcloud", lambda: soundcloud_search_download(query, out_tmpl, {
            "quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "geo_bypass": True, "socket_timeout": 15, "retries": 5,
            "fragment_retries": 5, "noplaylist": True,
        }, logger)),
        ("bandcamp", lambda: bandcamp_search_download(query, out_tmpl, logger)),
        ("mixcloud", lambda: mixcloud_search_download(query, out_tmpl, logger)),
        ("audius", lambda: audius_search_download(query, out_tmpl, logger)),
        ("jiosaavn", lambda: jiosaavn_search_download(query, out_tmpl, logger)),
        ("hearthis.at", lambda: hearthis_search_download(query, out_tmpl, logger)),
        ("archive.org", lambda: archive_org_search_download(query, out_tmpl, logger)),
        ("wikimedia_commons", lambda: wikimedia_commons_search_download(query, out_tmpl, logger)),
        ("openverse", lambda: openverse_search_download(query, out_tmpl, logger)),
        ("ccmixter", lambda: ccmixter_search_download(query, out_tmpl, logger)),
        ("musopen", lambda: musopen_collection_search_download(query, out_tmpl, logger)),
        ("jamendo", lambda: jamendo_search_download(query, out_tmpl, logger)),
        ("pixabay", lambda: pixabay_music_search_download(query, out_tmpl, logger)),
    ]
    for name, fn in from_:
        try:
            result = await fn()
        except Exception as e:
            (logger or (lambda t, m: None))("MUSIC_DL_ERR", f"{name} source crashed: {e}")
            result = None
        if result:
            return result
    return None


# ── Parallel orchestrator (races every source simultaneously) ────────────

async def try_extra_sources_parallel(query: str, cache_dir: str, logger=None):
    """Race all extra sources concurrently and return the FIRST valid result.

    Unlike try_extra_sources() (sequential), this function launches every
    source as an independent asyncio.Task at the same time.  The moment any
    one of them returns a non-None dict the rest are cancelled — so the
    effective latency is that of the *fastest* working source rather than the
    sum of all attempted sources.

    Each source gets its own unique output path so concurrent downloads never
    clobber each other.  Partial files from cancelled tasks are left in
    cache_dir for the caller's existing cleanup logic to handle.

    Args:
        query:      song name or search string
        cache_dir:  directory to write downloaded audio files into
        logger:     callable(tag, msg) for debug output

    Returns:
        A dict {title, file_path, duration, thumbnail, source} or None.
    """
    import time as _time
    logger = logger or (lambda tag, msg: None)
    ts = int(_time.time() * 1000)

    _ydl_common_noauth = {
        "quiet": True, "no_warnings": True, "nocheckcertificate": True,
        "geo_bypass": True, "socket_timeout": 15, "retries": 5,
        "fragment_retries": 5, "noplaylist": True,
        "check_formats": False,
    }

    sources = [
        ("youtube",           youtube_search_download(
            query, os.path.join(cache_dir, f"par_{ts}_yt.%(ext)s"), logger)),
        ("soundcloud",        soundcloud_search_download(
            query, os.path.join(cache_dir, f"par_{ts}_sc.%(ext)s"),
            _ydl_common_noauth, logger)),
        ("bandcamp",          bandcamp_search_download(
            query, os.path.join(cache_dir, f"par_{ts}_bc.%(ext)s"), logger)),
        ("mixcloud",          mixcloud_search_download(
            query, os.path.join(cache_dir, f"par_{ts}_mx.%(ext)s"), logger)),
        ("audius",            audius_search_download(
            query, os.path.join(cache_dir, f"par_{ts}_au.%(ext)s"), logger)),
        ("jiosaavn",          jiosaavn_search_download(
            query, os.path.join(cache_dir, f"par_{ts}_js.%(ext)s"), logger)),
        ("archive.org",       archive_org_search_download(
            query, os.path.join(cache_dir, f"par_{ts}_ia.%(ext)s"), logger)),
        ("wikimedia_commons", wikimedia_commons_search_download(
            query, os.path.join(cache_dir, f"par_{ts}_wm.%(ext)s"), logger)),
        ("openverse",         openverse_search_download(
            query, os.path.join(cache_dir, f"par_{ts}_ov.%(ext)s"), logger)),
        ("jamendo",           jamendo_search_download(
            query, os.path.join(cache_dir, f"par_{ts}_jm.%(ext)s"), logger)),
    ]

    async def _safe(name, coro):
        try:
            return await coro
        except asyncio.CancelledError:
            return None
        except Exception as exc:
            logger("MUSIC_DL_ERR", f"[parallel] {name}: {exc}")
            return None

    # Sources that always return the correct song (by YouTube search rank).
    # Their results bypass the relevance filter — we trust YouTube to be right.
    _PRIORITY_SOURCES = {"youtube"}

    def _is_relevant(q: str, title: str) -> bool:
        """Return True if the track title shares at least one meaningful word
        with the query.  Stop-words and single-char tokens are ignored so a
        title like 'Live Session #3' doesn't trick us into thinking it matches
        'tum hi aana pawandeep'."""
        _STOP = {"a","an","the","of","in","on","at","to","for","and","or",
                 "by","is","it","me","my","i","ka","ki","ke","hai","ho","na"}
        q_words = {w.lower() for w in re.split(r'\W+', q)
                   if w and len(w) > 2 and w.lower() not in _STOP}
        t_lower = (title or "").lower()
        # If we can't extract any meaningful query word, accept anything.
        return (not q_words) or any(w in t_lower for w in q_words)

    tasks   = {asyncio.create_task(_safe(name, coro)): name
               for name, coro in sources}
    pending = set(tasks)
    winner  = None
    # Fallback: the first result that arrived but failed the relevance check.
    # Used only if every source either fails or returns an irrelevant title.
    _fallback = None

    while pending:
        done, pending = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                result = task.result()
            except Exception:
                result = None
            if result is None or winner is not None:
                continue
            src_name = tasks[task]
            title    = result.get("title", "")
            # Accept immediately: priority source OR title matches the query.
            if src_name in _PRIORITY_SOURCES or _is_relevant(query, title):
                winner = result
                logger("MUSIC_RACE_WIN",
                       f"[parallel] winner: {src_name} — '{title[:50]}'")
                for p in pending:
                    p.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                pending = set()
                break
            else:
                # Result arrived but title looks irrelevant; keep as last resort.
                logger("MUSIC_RACE_SKIP",
                       f"[parallel] skipping irrelevant '{src_name}' "
                       f"result: '{title[:50]}'")
                if _fallback is None:
                    _fallback = result

    # If no relevant winner was found, fall back to the first available result
    # so the user still hears something rather than getting a "not found" error.
    return winner or _fallback



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# YOUTUBE ENGINE — 40 jugad techniques
# No cookies. No login. No API key. Public songs only.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Technique list:
#  CLIENT LADDER (10)
#  1.  android              — Most reliable. Bypasses most bot-checks.
#  2.  android_embed        — Embedded player. Different token path.
#  3.  android_testsuite    — Test build client. Relaxed restrictions.
#  4.  android_music        — YouTube Music client. Better for Indian songs.
#  5.  tvhtml5              — TV client. Different CDN / bot-check stack.
#  6.  tvhtml5_simply_embedded_player — Simplified TV embed. Fewer checks.
#  7.  mweb                 — Mobile web. Lightweight extraction path.
#  8.  web_embedded_player  — Web embed. Different from standard web.
#  9.  web                  — Standard web. Fallback for all above.
#  10. ios                  — iOS client. Unique extraction fingerprint.
#
#  NETWORK / BYPASS (4)
#  11. force_ipv4           — Avoids IPv6 extraction failures.
#  12. geo_bypass           — Handles region-restricted tracks.
#  13. geo_bypass_country   — Tries IN → US → GB cycling.
#  14. nocheckcertificate   — Skips TLS cert issues on some networks.
#
#  SEARCH STRATEGY (5)
#  15. ytsearch1:           — Song name → first YouTube result.
#  16. ytmsearch1:          — YouTube Music search (better for Hindi/regional).
#  17. ytsearch5: + rank    — 5 results, pick closest title match.
#  18. query normalization  — Remove special chars, retry clean.
#  19. query simplification — Strip to "artist song" minimal form.
#
#  FORMAT / QUALITY (7)
#  20. bestaudio[ext=m4a]  — AAC. Most compatible, fastest decode.
#  21. bestaudio[ext=webm] — Opus. Fallback if m4a unavailable.
#  22. bestaudio           — Any best audio (codec-agnostic).
#  23. best                — Last resort: any stream.
#  24. prefer_free_formats — Prefer open-source codecs.
#  25. raw m4a (no postprocessor) — Direct download, no FFmpeg conversion.
#  26. worstaudio          — Absolute last resort: any audio stream.
#
#  FFMPEG STABILITY (5)
#  27. -reconnect 1        — Auto-reconnect dropped streams.
#  28. -reconnect_streamed — Reconnect even on already-started streams.
#  29. -reconnect_delay_max 5 — Wait up to 5s between reconnects.
#  30. fragment_retries: 5 — Retry per-fragment downloads.
#  31. http_chunk_size     — 10MB chunks for stability.
#
#  RETRY / RESILIENCE (5)
#  32. retries: 3          — Per-client retry on transient errors.
#  33. socket_timeout: 30  — Don't hang on slow responses.
#  34. concurrent=1        — Single fragment thread (avoids GIL starvation).
#  35. backoff between clients — 0.3s sleep prevents rate-limit cascade.
#  36. file collision guard — Fresh output template per attempt.
#
#  ALTERNATE FRONTENDS (2)
#  37. Piped.video          — Open-source YT frontend (public API).
#  38. Invidious instances  — Alternate extraction via privacy frontends.
#
#  MAINTENANCE (2)
#  39. auto yt-dlp update  — pip upgrade once per process start.
#  40. partial cleanup      — Delete incomplete files between attempts.
#
#  BOT-CHECK BYPASS (3 new)
#  41. tv_embedded / android_vr / web_creator — NO PO-token clients; skip sign-in gate.
#  42. HTTP browser headers — realistic UA/Accept headers reduce bot-check triggers.
#  43. Phase 4 cookie-jar retry — empty Netscape cookie file drops some bot gates.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# ── yt-dlp client compatibility layer ────────────────────────────────────
# yt-dlp removed several legacy InnerTube clients (tv_embedded,
# android_testsuite, android_embed, android_music, tvhtml5,
# tvhtml5_simply_embedded_player, web_embedded_player). Passing them now
# logs "Skipping unsupported client" and can leave ZERO usable clients,
# which is why downloads failed with cloud_dl [['tv_embedded']].
# Legacy names are remapped to their modern equivalents and anything the
# installed yt-dlp doesn't know is dropped before it reaches extractor_args.
_YT_CLIENT_ALIASES = {
    "tv_embedded": "tv_simply",
    "tvhtml5": "tv",
    "tvhtml5_simply_embedded_player": "tv_simply",
    "android_embed": "web_embedded",
    "android_music": "web_music",
    "web_embedded_player": "web_embedded",
    "android_testsuite": "android",
    "android_creator": "web_creator",
}

def _yt_supported_clients() -> set:
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
        return set(INNERTUBE_CLIENTS)
    except Exception:
        try:
            from yt_dlp.extractor.youtube import INNERTUBE_CLIENTS  # older layout
            return set(INNERTUBE_CLIENTS)
        except Exception:
            return set()

_YT_SUPPORTED_CLIENTS = _yt_supported_clients()
# tv_downgraded needs an account Data Sync ID; never send it ourselves.
_YT_BLOCKED_CLIENTS = {"tv_downgraded"}
_YT_FALLBACK_CLIENTS = ["default", "tv", "web_safari"]

def yt_clients(clients) -> list:
    """Normalise a player_client list for the installed yt-dlp version."""
    if isinstance(clients, str):
        clients = [clients]
    out = []
    for c in clients or []:
        c = _YT_CLIENT_ALIASES.get(c, c)
        if c in _YT_BLOCKED_CLIENTS:
            continue
        if c not in ("default", "all") and _YT_SUPPORTED_CLIENTS and c not in _YT_SUPPORTED_CLIENTS:
            continue
        if c not in out:
            out.append(c)
    if not out:
        out = [c for c in _YT_FALLBACK_CLIENTS
               if not _YT_SUPPORTED_CLIENTS or c in _YT_SUPPORTED_CLIENTS] or ["default"]
    return out

# Client ladder in priority order (technique #1-10)
# NOTE: names below are validated/remapped by yt_clients() before use.
_YT_CLIENTS = yt_clients([
    # ROOT-CAUSE FIX (Heroku log 2026-08-23: "ERROR: [youtube] <id>: No video
    # formats found!" for EVERY song, Piped/Invidious mirrors all 4xx/5xx):
    # verified live from a datacenter IP with no cookies and no PO token —
    #   android                      -> muxed https format 18 downloads fine
    #   mweb / web_music             -> formats listed, media request 403s
    #   default / tv / tv_simply /
    #   android_vr                   -> "Sign in to confirm you're not a bot"
    #   web_safari / ios             -> zero formats  => "No video formats found!"
    # So the ANDROID InnerTube client is the only rung that actually plays on
    # a cloud dyno right now; it must lead the ladder. It does NOT support a
    # cookie jar (yt-dlp skips it when `cookiefile` is attached — that is why
    # the cookie path also died with "No video formats found!"), so cookies
    # are withheld for it by _clients_accept_cookies() below.
    "android",         # 0 — ★ PO-token-free, cookie-free, proven working
    # SABR-only experiment (yt-dlp #12482) ne `tv`/`default` ke https URLs
    # strip kar diye, isliye SABR-resistant clients pehle.
    "web_safari",      # 1 — Safari fingerprint, https formats abhi bhi aate hain
    "web_embedded",    # 2 — embedded web player, PO-token gate halka
    "tv_simply",       # 3 — SABR-free TV variant
    "default",         # 4 — yt-dlp ka apna best-guess client set
    "tv",              # 5 — last resort (SABR-affected)
])

# Piped public API instances — open-source YouTube frontends (technique #37)
#
# NOTE: these are volunteer-run instances and rot fast — an instance that's
# alive today can 502 or redirect-loop next week. Hardcoding just 3 of them
# means the day all 3 happen to be down, this entire source (and the
# Piped/Invidious fallback inside youtube_search_download) silently returns
# nothing, which looks exactly like "Nothing found" for every song. Two
# mitigations: (1) a much longer fallback list so the odds of ALL of them
# being dead at once are low, and (2) `_get_piped_apis()` below fetches the
# live, uptime-checked instance list from Piped's own instances API at
# runtime and merges it in, so the bot self-heals as instances die/appear
# without needing a code change + redeploy.
_PIPED_APIS = [
    "https://pipedapi.wireway.ch",
    "https://api.piped.private.coffee",
    "https://pipedapi.orangenet.cc",
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.privacyredirect.com",
    "https://pipedapi.r4fo.com",
    "https://pipedapi.smnz.de",
    "https://piped-api.hostux.net",
    "https://pipedapi.leptons.xyz",
]

_PIPED_INSTANCES_API = "https://piped-instances.kavin.rocks"
_piped_apis_cache = {"ts": 0.0, "apis": None}
_PIPED_APIS_CACHE_TTL = 1800  # 30 min — instance health changes slowly enough


async def _get_piped_apis() -> list:
    """Return Piped API base URLs, freshest-known-live first.

    Merges the live instance directory (uptime-checked by Piped itself)
    with the static fallback list above, so a handful of dead hardcoded
    URLs never fully blocks this source — and newly-appearing healthy
    instances get picked up automatically. Falls back to the static list
    alone if the directory fetch fails or times out (offline-safe).
    """
    now = time.time()
    if _piped_apis_cache["apis"] is not None and (now - _piped_apis_cache["ts"]) < _PIPED_APIS_CACHE_TTL:
        return _piped_apis_cache["apis"]
    # Hard-play latency fix: never spend the first .play request waiting for
    # the optional instance-directory service. The static list is already
    # ordered with known-good APIs; refresh is deferred until the cache TTL.
    if _piped_apis_cache["apis"] is None:
        _piped_apis_cache["apis"] = list(_PIPED_APIS)
        _piped_apis_cache["ts"] = now
        return _piped_apis_cache["apis"]

    live = []
    if _aiohttp is not None:
        try:
            async with _make_aiohttp_session() as sess:
                async with sess.get(
                    _PIPED_INSTANCES_API,
                    timeout=_aiohttp.ClientTimeout(total=6),
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        for entry in data or []:
                            api_url = entry.get("api_url")
                            if api_url:
                                live.append(api_url.rstrip("/"))
        except Exception:
            pass

    merged, seen = [], set()
    for api in live + _PIPED_APIS:
        if api not in seen:
            seen.add(api)
            merged.append(api)

    _piped_apis_cache["apis"] = merged or _PIPED_APIS
    _piped_apis_cache["ts"] = now
    return _piped_apis_cache["apis"]


# Invidious instances — alternate extraction frontends (technique #38)
# Updated 2026-08: removed instances confirmed dead from Heroku logs
# (inv.us.projectsegfau.lt, slipfox.xyz, tux.pizza, lunar.icu,
#  iv.melmac.space — Network unreachable, protokolla.fi + materialio.us
#  returning empty JSON). Added fresh alternatives.
_INVIDIOUS = [
    "https://invidious.private.coffee",       # AT — long-running, reliable
    "https://invidious.privacyredirect.com",  # EU — active redirect node
    "https://invidious.nerdvpn.de",           # DE — reliable long-running
    "https://inv.nadeko.net",                 # CL — active, good uptime
    "https://invidious.incogniweb.net",       # EU — active
    "https://iv.ybins.top",                   # AS — active
    "https://invidious.drgns.space",          # US — stable
    "https://yt.artemislena.eu",              # EU — active
    "https://invidious.fdn.fr",               # FR — long-running
]

_YT_UPDATE_DONE = False  # technique #39: update once per process


def _ensure_ytdlp_updated_sync():
    """Actual blocking pip call — must only ever run off the event loop
    thread (see `_ensure_ytdlp_updated` below).

    BUG FIX: flag was set True BEFORE running pip, so if pip failed
    (network timeout, permission error, etc.) future calls would skip
    the update entirely — yt-dlp stayed stale forever after one bad
    attempt. Fix: only mark done AFTER pip returns successfully.
    """
    global _YT_UPDATE_DONE
    if _YT_UPDATE_DONE or yt_dlp is None:
        return
    try:
        result = subprocess.run(
            [_sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"],
            capture_output=True, timeout=45,
        )
        # Mark done only on success (returncode 0) so a failed pip attempt
        # can be retried on the next music request instead of silently skipping.
        if result.returncode == 0:
            _YT_UPDATE_DONE = True
    except Exception:
        pass


async def _ensure_ytdlp_updated():
    """Best-effort yt-dlp self-update, once per process.

    This is intentionally callable from the startup warm-up only. Updating a
    package during the first user request adds avoidable cold-start latency and
    cannot change the already-imported yt_dlp module anyway.
    """
    if _YT_UPDATE_DONE or yt_dlp is None:
        return
    await asyncio.to_thread(_ensure_ytdlp_updated_sync)


async def warm_up_music_runtime(logger=None) -> None:
    """Warm optional music dependencies without blocking the first `.play`.

    Heroku builds normally contain yt-dlp, Deno and bgutil already. When a
    build misses one of them, repair it while the worker is starting instead
    of making the first song wait for pip/download/install work. Playback has
    its own fallbacks and can proceed while this best-effort warm-up runs.
    """
    logger = logger or (lambda *a: None)
    tasks = [asyncio.create_task(_ensure_ytdlp_updated())]
    if _YTDLP_COOKIE_FILE and not _BGUTIL_ACTIVE:
        tasks.append(asyncio.create_task(_ensure_bgutil_runtime(logger)))
    elif _BGUTIL_ACTIVE and not _BGUTIL_HTTP_READY:
        tasks.append(asyncio.create_task(warm_up_bgutil_server()))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger("MUSIC_WARMUP_WARN", str(result))


def _yt_normalize_query(q: str) -> str:
    """Technique #18: Remove special chars, normalize spacing."""
    q = re.sub(r'[|<>"\[\]{}]', ' ', q)
    q = re.sub(r'\s+', ' ', q).strip()
    return q


def _yt_simplify_query(q: str) -> str:
    """Technique #19: Strip down to first 4 words (artist + song)."""
    words = q.split()
    return " ".join(words[:4]) if len(words) > 4 else q


def _yt_build_format_chain():
    """Techniques #20-26: ordered audio format preference chain.

    BUG FIX: m4a removed as first preference — YouTube's DASH manifest no
    longer reliably serves m4a audio tracks from datacenter/cloud IPs,
    causing repeated 'Requested format is not available' errors.
    webm/opus goes first: pytgcalls ffmpeg can passthrough opus natively
    (no re-encode), and the webm/opus DASH stream is always present when
    cookies are active and the manifest is fully resolved.
    """
    return [
        "bestaudio/best",
        "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
        "bestaudio",
        "best",
    ]


# ── Randomized User-Agent pool (jugad #10) ──────────────────────────────
# Rotating across real desktop/mobile browser fingerprints spreads our
# request pattern across every source (YouTube, SoundCloud, Bandcamp, etc.)
# instead of hammering them all with one static, easily-flagged UA string.
UA_POOL = [
    "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36",
]


def random_ua() -> str:
    """Pick a random realistic browser User-Agent (jugad #10)."""
    return random.choice(UA_POOL)


# Kept for any external caller expecting a fixed name — now just one pick.
_YT_BROWSER_UA = random_ua()
# Tier-1 clients that don't need PO tokens — skip innertube auth entirely
_YT_NO_POTOKEN_CLIENTS: set = set()  # mobile/no-cookie client tier removed

# ── Cookie-capability map (ROOT-CAUSE FIX, Heroku log 2026-08-16) ───────
# yt-dlp REFUSES to use InnerTube clients that don't support cookies when an
# authenticated cookie jar is attached:
#   WARN: Skipping client "tv_simply" since it does not support cookies
#   WARN: Skipping client "android_vr" since it does not support cookies
#   WARN: Skipping client "android"  since it does not support cookies
# Our ladder attached `cookiefile` to EVERY rung, so those three rungs ended
# up with ZERO usable clients — yt-dlp then extracted nothing and reported
# "Requested format is not available", which is exactly the failure in the
# log for every single song. The cookieless clients are precisely the ones
# that still work from a datacenter IP, so cookies were killing playback.
#
# Fix: attach cookies ONLY to rungs whose clients all support cookies; run
# the cookieless clients genuinely cookieless.
_YT_NO_COOKIE_CLIENTS = {"android", "android_vr", "android_music",
                         "android_creator", "ios", "ios_music",
                         "ios_creator", "tv_simply"}
try:  # prefer yt-dlp's own metadata when available
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS as _IC
    _YT_NO_COOKIE_CLIENTS |= {
        k for k, v in _IC.items() if not v.get("SUPPORTS_COOKIES")
    }
except Exception:
    pass


def _clients_accept_cookies(clients) -> bool:
    """True only if every client in the rung can use a cookie jar."""
    return bool(clients) and all(
        c not in _YT_NO_COOKIE_CLIENTS for c in clients
    )


# ── bgutil PO-token provider paths ──────────────────────────────────────
# ROOT-CAUSE FIX (Heroku log 2026-08-16: every cloud_dl rung dying with
# "unable to download video data: HTTP Error 403: Forbidden" and
# "Requested format is not available"):
#
# The bgutil script provider was being handed the provider ROOT directory
# as `server_home`. The plugin resolves the generator as
# `<server_home>/src/generate_once.ts`, so it looked for
# `vendor/bgutil-ytdlp-pot-provider/src/generate_once.ts` — a path that
# does NOT exist (the real file lives under `.../server/src/`). The plugin
# logged "Script path doesn't exist" and returned NO PO token, silently.
#
# Without a PO token, `formats: ["missing_pot"]` keeps the PO-token-gated
# googlevideo formats visible, yt-dlp happily picks one, and YouTube
# answers 403 on the actual media request — exactly what the log shows.
# For clients that expose nothing else, the selector matches nothing and we
# get "Requested format is not available" instead.
#
# `server_home` MUST be the `server` sub-directory.
_BGUTIL_HOME: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "vendor", "bgutil-ytdlp-pot-provider",
)
_BGUTIL_SERVER_HOME: str = os.path.join(_BGUTIL_HOME, "server")
_BGUTIL_SCRIPT: str = os.path.join(
    _BGUTIL_SERVER_HOME, "src", "generate_once.ts"
)
# Disabled: bundled bgutil versions conflict with yt-dlp provider registry.
_BGUTIL_ACTIVE: bool = False

# ── Warm bgutil HTTP provider (Melody_music proven fast path) ───────────
# Script mode spawns a brand-new Deno + BotGuard session for EVERY token,
# which costs seconds per track and times out under our short socket
# timeouts. The bundled `server/src/main.ts` is the same provider running
# as a long-lived local HTTP server with a warm session + token cache.
# Prefer it, fall back to script mode automatically.
_BGUTIL_HTTP_PORT: int = int(os.environ.get("BGUTIL_PORT", "4416"))
_BGUTIL_HTTP_PROC = None
_BGUTIL_HTTP_READY: bool = False
_BGUTIL_HTTP_LOCK = threading.Lock()


def ensure_bgutil_http_server() -> bool:
    """Start the persistent bgutil PO-token HTTP server (idempotent)."""
    global _BGUTIL_HTTP_PROC
    with _BGUTIL_HTTP_LOCK:
        if _BGUTIL_HTTP_READY:
            return True
        if _BGUTIL_HTTP_PROC is not None and _BGUTIL_HTTP_PROC.poll() is None:
            return False  # starting — poll _bgutil_http_alive() shortly
        main_ts = os.path.join(_BGUTIL_SERVER_HOME, "src", "main.ts")
        _ensure_deno_cache_env()
        deno = _find_deno() or ensure_deno_runtime()
        if not os.path.isfile(main_ts) or not deno:
            return False
        cache_dir = os.path.join(_BGUTIL_SERVER_HOME, "cache")
        node_mods = os.path.join(_BGUTIL_SERVER_HOME, "node_modules")
        try:
            os.makedirs(cache_dir, exist_ok=True)
            _BGUTIL_HTTP_PROC = subprocess.Popen(
                [
                    deno, "run",
                    "--allow-env", "--allow-net",
                    f"--allow-ffi={node_mods}",
                    f"--allow-write={cache_dir}",
                    f"--allow-read={cache_dir},{node_mods}",
                    main_ts, "--port", str(_BGUTIL_HTTP_PORT),
                ],
                cwd=_BGUTIL_SERVER_HOME,
                env={**os.environ, "XDG_CACHE_HOME": cache_dir},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return False
        except Exception:
            _BGUTIL_HTTP_PROC = None
            return False


def _bgutil_http_alive() -> bool:
    """Best-effort /ping against the local bgutil HTTP server."""
    global _BGUTIL_HTTP_READY
    if _BGUTIL_HTTP_READY:
        return True
    try:
        import urllib.request as _u
        with _u.urlopen(
            f"http://127.0.0.1:{_BGUTIL_HTTP_PORT}/ping", timeout=1.5
        ) as r:
            if r.status == 200:
                _BGUTIL_HTTP_READY = True
                return True
    except Exception:
        pass
    return False


async def warm_up_bgutil_server(timeout: float = 25.0) -> None:
    """Start + wait for the bgutil HTTP server (call once at startup)."""
    if not _BGUTIL_ACTIVE:
        return
    if not await asyncio.to_thread(ensure_bgutil_http_server) and _BGUTIL_HTTP_PROC is None:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await asyncio.to_thread(_bgutil_http_alive):
            return
        await asyncio.sleep(0.5)


def _bgutil_provider_args() -> dict:
    """PO-token provider extractor_args — warm HTTP first, script fallback."""
    if not _BGUTIL_ACTIVE:
        return {}
    if _bgutil_http_alive():
        return {
            "youtubepot-bgutilhttp": {
                "base_url": [f"http://127.0.0.1:{_BGUTIL_HTTP_PORT}"],
            }
        }
    # Kick the HTTP server off for the next track, use script mode now.
    with contextlib.suppress(Exception):
        ensure_bgutil_http_server()
    return {"youtubepot-bgutilscript": {"server_home": [_BGUTIL_SERVER_HOME]}}

if _BGUTIL_ACTIVE:
    import logging as _logging
    _logging.getLogger("music_sources").info(
        "✅ bgutil PO-token provider active: %s", _BGUTIL_SERVER_HOME
    )
else:
    # Provider intentionally disabled; strict audio path uses built-in yt-dlp.
    pass


# ── Deno runtime (build-time vendor + RUNTIME self-heal) ────────────────
# ROOT-CAUSE FIX (Heroku log 2026-08-23: EVERY cloud_dl rung — web_safari,
# web, web_embedded, tv, tv_simply, default — dies with
#   "Sign in to confirm you're not a bot"
# within ~1.5s, i.e. before any media request):
# bgutil could not mint a Proof-of-Origin token, so YouTube treated the
# datacenter IP as an unauthenticated bot on every client. bgutil cannot run
# without Deno, and Deno was ONLY installed at build time from a single
# GitHub URL with no mirrors — one 503 from GitHub during the build leaves
# the dyno permanently without a JS runtime and bricks playback until the
# next redeploy.
# Fix (same approach as the Melody_music repo, where it resolved this exact
# failure): keep the build-time vendoring, but also self-heal at runtime by
# downloading Deno into /tmp from multiple mirrors, and point Deno's module
# cache at a writable directory.
_DENO_RUNTIME_DIR = "/tmp/deno_runtime"
_DENO_LOCK = threading.Lock()
_DENO_MIRRORS = (
    "https://dl.deno.land/release/{ver}/deno-x86_64-unknown-linux-gnu.zip",
    "https://github.com/denoland/deno/releases/download/{ver}"
    "/deno-x86_64-unknown-linux-gnu.zip",
    "https://github.com/denoland/deno/releases/latest/download"
    "/deno-x86_64-unknown-linux-gnu.zip",
)


def _ensure_deno_cache_env() -> None:
    """Deno needs a writable module/npm cache; $HOME may be read-only."""
    cache = os.environ.get("DENO_DIR")
    if not cache or not os.access(os.path.dirname(cache) or "/", os.W_OK):
        cache = "/tmp/deno_cache"
        os.environ["DENO_DIR"] = cache
    os.environ.setdefault("DENO_NO_UPDATE_CHECK", "1")
    with contextlib.suppress(Exception):
        os.makedirs(cache, exist_ok=True)


_ensure_deno_cache_env()


def _find_deno() -> str | None:
    """Find Deno binary — vendor path, /tmp runtime install, then PATH."""
    _vendor_deno = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "vendor", "deno", "bin", "deno",
    )
    if os.path.isfile(_vendor_deno) and os.access(_vendor_deno, os.X_OK):
        return _vendor_deno
    _tmp_deno = os.path.join(_DENO_RUNTIME_DIR, "deno")
    if os.path.isfile(_tmp_deno) and os.access(_tmp_deno, os.X_OK):
        return _tmp_deno
    import shutil as _sh2
    return _sh2.which("deno")


def ensure_deno_runtime(logger=None) -> str | None:
    """Download Deno into /tmp when the build-time vendoring failed.

    Blocking — always call via asyncio.to_thread(). Returns the binary path
    or None when every mirror failed.
    """
    import logging as _log_d
    _log = _log_d.getLogger("music_sources")
    _info = logger or (lambda tag, msg: _log.info("[%s] %s", tag, msg))

    found = _find_deno()
    if found:
        return found

    with _DENO_LOCK:
        found = _find_deno()
        if found:
            return found

        _ensure_deno_cache_env()

        import urllib.request as _ureq
        import zipfile as _zf

        version = "v2.9.5"
        with contextlib.suppress(Exception):
            with _ureq.urlopen(
                "https://dl.deno.land/release-latest.txt", timeout=8
            ) as _r:
                _v = _r.read().decode().strip()
                if _v.startswith("v"):
                    version = _v

        os.makedirs(_DENO_RUNTIME_DIR, exist_ok=True)
        target = os.path.join(_DENO_RUNTIME_DIR, "deno")
        archive = os.path.join(_DENO_RUNTIME_DIR, "deno.zip")

        for mirror in _DENO_MIRRORS:
            url = mirror.format(ver=version)
            try:
                _info("DENO", f"⬇️ Installing Deno at runtime from {url}")
                with _ureq.urlopen(url, timeout=120) as resp, \
                        open(archive, "wb") as fh:
                    fh.write(resp.read())
                if os.path.getsize(archive) < 1_000_000:
                    raise RuntimeError("archive too small")
                with _zf.ZipFile(archive) as zf:
                    zf.extract("deno", _DENO_RUNTIME_DIR)
                os.chmod(target, 0o755)
                with contextlib.suppress(Exception):
                    os.unlink(archive)
                _info("DENO", f"✅ Deno {version} installed at {target}")
                return target
            except Exception as exc:
                _info("DENO_WARN", f"⚠️ Deno mirror failed ({url}): {exc}")
                with contextlib.suppress(Exception):
                    os.unlink(archive)

        _info(
            "DENO_ERR",
            "❌ Deno install failed on all mirrors — bgutil cannot mint a "
            "PO-token, so YouTube will keep bot-gating this IP.",
        )
        return None


def _get_js_runtime_opts() -> dict:
    """Return js_runtimes + remote_components — Musicbot working jugad.

    YouTube now requires a JavaScript runtime to solve its player challenge.
    Without Deno + ejs:github, yt-dlp cannot extract formats from cloud/Heroku
    IPs → 'No video formats found!'.

    bin/post_compile installs Deno to vendor/deno/bin/deno at Heroku build time.
    _find_deno() locates it.  remote_components=["ejs:github"] tells yt-dlp to
    fetch the EJS extractor from GitHub — the same approach that makes Musicbot
    work 100% on Heroku.
    """
    opts: dict = {"remote_components": ["ejs:github"]}
    deno = _find_deno()
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    return opts


def _cloud_download_sync(
    url_or_query: str,
    out_tmpl: str,
    audio_only: bool = True,
    logger=None,
    combos_override=None,
) -> dict | None:
    """Musicbot-style direct local download — works 100% on Heroku/cloud IPs.

    On cloud/datacenter IPs the YouTube CDN (googlevideo.com) is IP-blocked.
    Trying to stream directly or using complex client ladders just wastes time.
    yt-dlp ships with curl_cffi which impersonates Chrome's TLS fingerprint —
    this bypasses the CDN IP block entirely. Downloading to a local file and
    serving that file sidesteps ALL streaming CDN restrictions.

    Uses Musicbot's proven approach:
      - player_client=["web"] with cookies (authenticates fully, full DASH manifest)
      - js_runtimes + remote_components (solves YouTube JS challenge)
      - bgutil PO-token (bypasses cloud-IP gate)
      - 16 parallel fragment threads (fast download)
      - curl_cffi TLS impersonation

    Returns music_sources-style info dict or None on failure.
    """
    logger = logger or (lambda *a: None)
    if yt_dlp is None:
        return None

    # Per-run: is a cookie jar available at all? Actual file is minted fresh
    # per rung (see cookiefile_for_run() below) so parallel yt-dlp writers
    # can't corrupt each other's jar mid-flight.
    cookie = bool(_YTDLP_COOKIE_TEXT)
    is_search = not (url_or_query.startswith("http://") or url_or_query.startswith("https://"))
    target = f"ytsearch1:{url_or_query}" if is_search else url_or_query

    # Combo format: (format_string, clients_list, need_player_skip)
    # Ordering: no-PO-token clients FIRST so it works even without bgutil/cookies.
    #
    # tv_embedded + player_skip=["webpage"]: skips the sign-in gate entirely —
    # no cookies, no PO-token needed. Gives limited DASH manifest (no separate
    # audio DASH stream) but ffmpeg extracts audio fine from muxed stream.
    # This is the most reliable on cloud IPs.
    #
    # web + cookies + bgutil: full DASH manifest, best quality. Needs bgutil
    # PO-token because cloud IPs can't acquire PO-token from the webpage.
    # ROOT-CAUSE FIX (Heroku log: cloud_dl [['android_vr']] / [['tv_simply']] /
    # [['android']] → "Failed to extract any player response" on EVERY rung):
    # `player_skip=["webpage"]` webpage se player response fetch hi nahi karne
    # deta, aur ab YouTube in innertube clients ko datacenter IP par bina
    # webpage/PO-token ke koi player response deta hi nahi. Isliye har attempt
    # fail ho raha tha aur .play par kuch bhi nahi bajta tha.
    #
    # Melody_music (reference repo) wali proven config apnayi gayi hai:
    #   player_client = ["default", "tv", "web_safari"]  + formats=missing_pot
    #   (koi player_skip nahi) + native downloader.
    # missing_pot yt-dlp ko wo formats bhi rakhne deta hai jinke liye PO-token
    # nahi mila — cookies/bgutil ke saath ye normally download ho jaate hain.
    # ROOT-CAUSE FIX (Heroku log 2026-08-16 cloud_dl [['tv']] ydl-errors:
    #   "Some tv client https formats have been skipped as they are missing
    #    a URL. YouTube may have enabled the SABR-only streaming experiment
    #    for your account. See yt-dlp/yt-dlp#12482"):
    # `tv` client (aur `default` jo tv bhi include karta hai) SABR-only ban
    # gaya hai — HTTPS URLs strip ho jaate hain aur download 0-byte pe fail.
    # yt-dlp #12482 ke mutabik SABR-resistant clients pehle try karo:
    #   web_safari, mweb, ios, web_embedded, tv_embedded (with player_skip).
    # tv/default ko sirf last resort ke taur pe rakho.
    # LADDER ORDER — re-verified 2026-08-16 from a datacenter IP with the
    # (now actually working) bgutil PO-token provider attached:
    #   ✅ web_embedded, tv_simply, android_vr, android, default
    #   ❌ web_safari / mweb / ios  → 403 Forbidden on the media request
    #   ❌ tv / web                 → "Requested format is not available"
    # The old order led with the three 403 clients, so every song burned
    # ~40s of failures before reaching a client that actually works.
    # ROOT-CAUSE FIX (Heroku log 2026-08-16 12:16, cookies=✅ but EVERY rung:
    #   "Sign in to confirm you're not a bot"  on tv_simply / android_vr /
    #   android). Those three clients CANNOT use a cookie jar.
    #
    # ROOT-CAUSE FIX (Heroku log 2026-08-16 12:35, cookies=✅ bgutil=✅ but
    #   cloud_dl [['mweb']]: "unable to download video data: HTTP Error 403:
    #   Forbidden" and then NOTHING played at all):
    # Two separate defects combined:
    #   1. Cookie-first ordering put the whole `web` family (web_embedded,
    #      web_safari, mweb, web, web_music) at the TOP of the ladder. On a
    #      datacenter IP every one of those needs a valid GVS PO-token for the
    #      media request; with `formats=["missing_pot"]` we deliberately keep
    #      the un-tokened formats visible, yt-dlp picks one and googlevideo
    #      answers 403 Forbidden. Guaranteed failure, ~8s burned per rung.
    #   2. The `_botgate_hits >= 2` optimisation skipped EVERY cookieless rung
    #      once two rungs hit the bot-gate — i.e. exactly the tv_simply /
    #      android_vr / tv_embedded rungs that actually work on Heroku were
    #      thrown away, so the ladder ran out of options and `.play` produced
    #      nothing at all.
    # Fix: one single ladder that leads with the PO-token-free clients
    # (verified working from a datacenter IP), attaches cookies only where the
    # client supports them, and keeps the web family as a later fallback.
    # A 403 on the media request now marks the whole web family as gated and
    # skips its remaining rungs instead of aborting the useful ones.
    _WEB_FAMILY = {"web", "web_safari", "web_embedded", "web_music", "mweb"}

    # ROOT-CAUSE FIX (Heroku log 2026-08-16 13:02, bgutil=✅ deno=✅ cookies=✅
    #   but EVERY rung: cloud_dl [['tv_simply']] / [['android_vr']] →
    #   "Sign in to confirm you're not a bot"):
    # tv_simply / android_vr / tv_embedded CANNOT use a cookie jar, and from a
    # datacenter IP YouTube now bot-gates them even with a PO-token provider
    # attached. Leading the ladder with them means every single rung that can
    # actually authenticate is reached only after ~8 wasted attempts — and the
    # cookieless rungs can never succeed on Heroku at all.
    # Fix: when cookies are available, lead with the Melody_music-proven
    # cookie + bgutil configuration (player_client default/tv/web_safari with
    # formats=missing_pot), which is the config verified working on this exact
    # Heroku USA dyno. The cookieless clients stay on as a fallback for the
    # no-cookie deployment.
    # ROOT-CAUSE FIX (Heroku log 2026-08-16 18:03, cloud_dl [['tv']] ydl-errors:
    #   "Some tv client https formats have been skipped as they are missing a
    #    URL. YouTube may have enabled the SABR-only streaming experiment"):
    # YouTube ne is IP/account ke liye `tv` (aur `default` jo tv include karta
    # hai) ko SABR-only kar diya — https URLs hi nahi aate, isliye rung 0-byte
    # pe marta hai. SABR-resistant clients pehle chalte hain aur SABR warning
    # dikhte hi us client family ke baaki rungs skip ho jaate hain.
    _SABR_FAMILY = {"tv", "default"}

    # ★ Rung 0 — the only combination verified to actually deliver bytes from a
    # datacenter IP today (no cookies, no PO token). `best` is required in the
    # selector because the android client only advertises the muxed format 18;
    # _audio_pp() extracts the audio track afterwards.
    _ANDROID_RUNGS = [
        ("bestaudio/best[acodec!=none]/best", ["android"], False),
    ]

    _COOKIE_RUNGS = [
        # SABR-resistant, cookie-capable clients pehle.
        ("bestaudio[ext=m4a]/bestaudio/best",  ["web_safari"],      False),
        ("bestaudio[abr<=128]/bestaudio/best", ["web_safari", "web"], False),
        ("bestaudio[ext=m4a]/bestaudio/best",  ["web"],             False),
        ("bestaudio/best",                     ["web_embedded"],    False),
        # tv/default (SABR-affected) sirf last resort.
        ("bestaudio[abr<=128]/bestaudio/best",
         ["web_safari", "tv", "default"],                          False),
    ]

    _NOCOOKIE_RUNGS = [
        ("bestaudio/best",                     ["web_safari"],    False),
        ("bestaudio/best",                     ["web_embedded"],  False),
        ("bestaudio/best",                     ["tv_simply"],     False),
        ("bestaudio/best",                     ["default"],       False),
        ("bestaudio/best",
         ["web_safari", "default", "tv"],                         False),
    ]
    if cookie:
        # Production proof: android hit bot-gate, web_safari returned bytes.
        # Cookie-capable Safari/Web must run before anonymous Android.
        combos = _COOKIE_RUNGS + _ANDROID_RUNGS + _NOCOOKIE_RUNGS
    else:
        combos = _ANDROID_RUNGS + _NOCOOKIE_RUNGS + _COOKIE_RUNGS
    # Last resort: SABR-affected clients (tv/default).
    combos += [
        ("bestaudio/best",                    ["tv"],                False),
        ("bestaudio/best",                    ["tv_simply"],         False),
    ]
    # FINAL SAFETY NET — never fail with "Requested format is not available"
    # just because the selector was too strict. These rungs accept literally
    # any downloadable stream (audio-only preferred, then muxed, then worst)
    # from the clients that are proven to still serve https URLs.
    _ANY_FMT = ("bestaudio*[protocol^=http]/bestaudio*/bestaudio/"
                "best[protocol^=http]/best/worst")
    combos += [
        (_ANY_FMT, ["android"],                                    False),
        (_ANY_FMT, ["web_safari", "web", "web_embedded"],          False),
        (_ANY_FMT, ["default", "tv", "web_safari"],                False),
        (_ANY_FMT, ["default"],                                    False),
    ]


    # HEROKU FIX: when ffmpeg not found, override format to pre-packaged audio.
    # "bestaudio/best" can pick DASH separate streams that need FFmpegMergerPP —
    # yt-dlp adds that automatically, causing crash even with postprocessors=[].
    # m4a/opus/webm/mp4 are single-file containers — no merge, no ffmpeg needed.
    _NO_FFMPEG_FMT = (
        "bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio[ext=webm]"
        "/best[ext=mp4]/best[ext=webm]/best"
    )

    _botgate_hits = 0          # how many rungs died on "Sign in to confirm…"
    _pot403_hits = 0           # web-family rungs killed by a 403 media request
    _sabr_hits = 0             # rungs killed by the SABR-only experiment (tv/default)
    if combos_override is not None:
        combos = list(combos_override)
    for fmt, clients, use_player_skip in combos:
        actual_fmt = fmt if _MS_FFMPEG_DIR else _NO_FFMPEG_FMT
        clients = yt_clients(clients)
        # A 403 on the media request means this IP has no valid GVS PO-token,
        # so every remaining PO-token-gated web rung will fail identically —
        # skip them (but NEVER skip the cookieless clients that do work).
        if _pot403_hits >= 3 and set(clients) <= _WEB_FAMILY:
            continue
        # SABR-only experiment: tv/default se https URLs aate hi nahi, so once
        # it has bitten, don't burn more time on pure tv/default rungs.
        if _sabr_hits >= 1 and set(clients) <= _SABR_FAMILY:
            continue
        # Same idea for the anonymous bot-gate: once it has bitten twice,
        # cookieless rungs are hopeless *only if* we still have cookie-capable
        # rungs left, so just skip pure-cookieless single-client rungs.
        if (cookie and _botgate_hits >= 3
                and "android" not in clients
                and not _clients_accept_cookies(clients)):
            continue


        # formats=missing_pot: PO-token ke bina bhi formats hide na ho —
        # warna selector kuch match nahi karta ("Requested format is not
        # available") aur har client fail dikhta hai.
        yt_ext: dict = {"player_client": clients, "formats": ["missing_pot"]}
        if use_player_skip:
            yt_ext["player_skip"] = ["webpage"]
        ext_args: dict = {"youtube": yt_ext}
        ext_args.update(_bgutil_provider_args())


        opts: dict = {
            "format":           actual_fmt,
            "outtmpl":          out_tmpl,
            "quiet":            False,   # ← keep stderr for debug logging below
            "no_warnings":      False,
            "noplaylist":       not is_search,
            "geo_bypass":       True,
            "geo_bypass_country": "US",
            "check_formats":    False,
            "socket_timeout":   6,
            "retries":          1,       # fast fail — don't retry same broken client
            "extractor_retries": 1,
            "fragment_retries": 3,
            "concurrent_fragment_downloads": 16,
            # Melody fix: kabhi external downloader (ffmpeg/aria2c) mat use
            # karo — Heroku par wo non-zero exit code se marta hai aur
            # yt-dlp use hard DownloadError bana deta hai.
            "external_downloader": {"default": "native"},
            "hls_prefer_native": True,

            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.youtube.com/",
            },
            "extractor_args": ext_args,
            "plugin_dirs": [],
            **_get_js_runtime_opts(),
            **_get_impersonate_opt(),
            # _audio_pp() emits FFmpegExtractAudio + ffmpeg_location when a real
            # ffmpeg/ffprobe PAIR exists, otherwise postprocessors=[] plus
            # fixup="never" so yt-dlp's implicit fixup pass can't invoke a
            # missing binary. This is the actual "ffprobe and ffmpeg not found" fix.
            **_youtube_audio_opts()
        }
        # BUG FIX: always inject cookies when available — even for tv_embedded/android_vr
        # with player_skip. YouTube now bot-checks these clients from cloud IPs regardless
        # of player_skip, so valid cookies are required to pass the gate.
        # Only attach cookies to rungs whose clients actually support them —
        # otherwise yt-dlp skips every client in the rung and the download
        # dies with "Requested format is not available" (see note above).
        if cookie and _clients_accept_cookies(clients):
            _fresh = cookiefile_for_run()
            if _fresh:
                opts["cookiefile"] = _fresh

        # Capture yt-dlp stderr for debug logging
        _ydl_error: list[str] = []

        class _ErrLogger:
            def debug(self, msg):   pass
            def warning(self, msg): _ydl_error.append(f"WARN: {msg}")
            def error(self, msg):   _ydl_error.append(f"ERR: {msg}")

        opts["logger"] = _ErrLogger()

        info = None
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                raw = ydl.extract_info(target, download=True)
                if isinstance(raw, dict) and "entries" in raw:
                    entries = [e for e in (raw.get("entries") or []) if e]
                    info = entries[0] if entries else None
                else:
                    info = raw
        except Exception as exc:
            err_str = str(exc)
            if _ydl_error:
                err_str = _ydl_error[-1] + " | " + err_str
            if "not a bot" in err_str or "Sign in to confirm" in err_str:
                _botgate_hits += 1
            if ("403" in err_str and "Forbidden" in err_str
                    and set(clients) <= _WEB_FAMILY):
                _pot403_hits += 1
            if ("SABR" in err_str or "missing a URL" in err_str):
                _sabr_hits += 1

            logger("MUSIC_DL_ERR", f"cloud_dl [{clients}]: {err_str[:200]}")

            # Cleanup partial files
            for ext in ("opus", "mp3", "m4a", "webm", "part", "ytdl"):
                p = out_tmpl.replace("%(ext)s", ext)
                if os.path.exists(p):
                    with contextlib.suppress(Exception):
                        os.remove(p)
            continue

        if _ydl_error:
            _joined = " ".join(_ydl_error)
            if "SABR" in _joined or "missing a URL" in _joined:
                _sabr_hits += 1
            logger("MUSIC_DL_ERR", f"cloud_dl [{clients}] ydl-errors: {'; '.join(_ydl_error[-3:])[:300]}")

        if info:
            p = _resolve_downloaded_file(info, out_tmpl)
            if p:
                logger("MUSIC_DL", f"cloud_dl ✓ [{clients}] '{(info.get('title',''))[:50]}'")
                return {
                    "title":     info.get("title", url_or_query),
                    "file_path": p,
                    "duration":  int(info.get("duration") or 0),
                    "thumbnail": info.get("thumbnail"),
                    "source":    "youtube",
                }
            logger("MUSIC_DL_ERR",
                   f"cloud_dl [{clients}]: downloaded file not found for {out_tmpl}")
        else:
            logger("MUSIC_DL_ERR", f"cloud_dl [{clients}]: ydl returned no info")

        # Cleanup partial files on no-result
        for ext in ("opus", "mp3", "m4a", "webm", "part", "ytdl"):
            p = out_tmpl.replace("%(ext)s", ext)
            if os.path.exists(p):
                with contextlib.suppress(Exception):
                    os.remove(p)

    return None


# ── curl-cffi TLS impersonation (jugad #42) ──────────────────────────────────
# yt-dlp[default,curl-cffi] is in requirements.txt.
# ImpersonateTarget makes requests look like real Chrome at the TLS layer,
# bypassing Heroku/cloud-IP fingerprinting that YouTube/CDN uses to detect bots.
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget as _ImpersonateTarget
except Exception:
    _ImpersonateTarget = None

# BUG FIX: importing ImpersonateTarget only proves yt-dlp is installed — the
# class lives in core yt-dlp, NOT in curl-cffi. When the curl_cffi wheel is
# missing/failed to build (very common on Heroku), passing `impersonate`
# raised "Impersonate target chrome:windows is not available" and EVERY
# single download attempt died instantly — that is why the whole client
# ladder failed in seconds with no usable error. Now the real, runtime list
# of supported targets is probed once and impersonation is simply skipped
# when unsupported.
_IMPERSONATE_TARGET = None
_IMPERSONATE_PROBED = False


def _probe_impersonate_target():
    """Pick a genuinely available impersonate target, or None."""
    global _IMPERSONATE_TARGET, _IMPERSONATE_PROBED
    if _IMPERSONATE_PROBED:
        return _IMPERSONATE_TARGET
    _IMPERSONATE_PROBED = True
    if yt_dlp is None or _ImpersonateTarget is None:
        return None
    try:
        import curl_cffi  # noqa: F401  — the actual backend
    except Exception:
        return None
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as _y:
            available = list(_y._get_available_impersonate_targets() or [])
    except Exception:
        return None
    if not available:
        return None
    preferred = (
        _ImpersonateTarget("chrome", None, "windows", None),
        _ImpersonateTarget("chrome", None, None, None),
    )
    targets = [t[0] if isinstance(t, (tuple, list)) else t for t in available]
    for want in preferred:
        for got in targets:
            try:
                if got in want or want in got:
                    _IMPERSONATE_TARGET = want
                    return _IMPERSONATE_TARGET
            except Exception:
                continue
    _IMPERSONATE_TARGET = targets[0]
    return _IMPERSONATE_TARGET


def _get_impersonate_opt() -> dict:
    """Return {"impersonate": <target>} only when it actually works here."""
    target = _probe_impersonate_target()
    return {"impersonate": target} if target else {}


_CURL_CFFI_AVAILABLE = None  # legacy flag kept for external callers


# ── Runtime bgutil installer (jugad #43) ─────────────────────────────────────
# bgutil provides Proof-of-Origin tokens that YouTube requires from cloud IPs.
# bin/post_compile installs it at Heroku build time; this function installs it
# at runtime on the first music request so any running dyno gets it immediately
# without a full redeploy.
_BGUTIL_INSTALL_LOCK = asyncio.Lock()
_BGUTIL_INSTALL_DONE = False


async def _ensure_bgutil_runtime(logger=None) -> None:
    # External bgutil builds are incompatible with the deployed yt-dlp
    # provider registry; strict audio playback intentionally does not install
    # or register them at runtime.
    return
    # External bgutil builds are incompatible with the deployed yt-dlp
    # provider registry; strict audio playback intentionally does not install
    # or register them at runtime.
    return
    """One-shot runtime bgutil installer — idempotent and async-safe."""
    global _BGUTIL_ACTIVE, _BGUTIL_INSTALL_DONE
    if _BGUTIL_ACTIVE or _BGUTIL_INSTALL_DONE:
        return
    async with _BGUTIL_INSTALL_LOCK:
        if _BGUTIL_ACTIVE or _BGUTIL_INSTALL_DONE:
            return
        _BGUTIL_INSTALL_DONE = True  # prevent retries on failure

        import logging as _log2
        _log = _log2.getLogger("music_sources")
        _info = logger or (lambda tag, msg: _log.info("[%s] %s", tag, msg))

        BGUTIL_VERSION = "1.3.1"
        BGUTIL_URL = (
            f"https://github.com/Brainicism/bgutil-ytdlp-pot-provider"
            f"/archive/refs/tags/{BGUTIL_VERSION}.zip"
        )

        try:
            import zipfile
            import tempfile
            import urllib.request as _urlreq

            _info("BGUTIL", f"⬇️ Installing bgutil v{BGUTIL_VERSION} (IP bypass for YouTube)...")

            tmp_zip = os.path.join(tempfile.gettempdir(), "bgutil_dl.zip")
            tmp_ext = os.path.join(tempfile.gettempdir(), "bgutil_ext")

            # Download
            def _dl():
                _urlreq.urlretrieve(BGUTIL_URL, tmp_zip)
            await asyncio.to_thread(_dl)

            # Extract
            def _unzip():
                import shutil as _sh3
                if os.path.exists(tmp_ext):
                    _sh3.rmtree(tmp_ext)
                os.makedirs(tmp_ext, exist_ok=True)
                with zipfile.ZipFile(tmp_zip) as zf:
                    zf.extractall(tmp_ext)
            await asyncio.to_thread(_unzip)

            # Find extracted root dir
            extracted_root = None
            for _item in os.listdir(tmp_ext):
                _full = os.path.join(tmp_ext, _item)
                if os.path.isdir(_full):
                    extracted_root = _full
                    break
            if not extracted_root:
                raise RuntimeError("bgutil: no directory in extracted archive")

            plugin_src = os.path.join(extracted_root, "plugin")
            server_src = os.path.join(extracted_root, "server")
            if not os.path.isdir(plugin_src) or not os.path.isdir(server_src):
                raise RuntimeError("bgutil: unexpected archive layout")

            # Copy to vendor/
            import shutil as _sh4
            bgutil_home = _BGUTIL_HOME
            plugin_dst = os.path.join(bgutil_home, "plugin")
            server_dst = os.path.join(bgutil_home, "server")
            os.makedirs(bgutil_home, exist_ok=True)
            if os.path.exists(plugin_dst):
                _sh4.rmtree(plugin_dst)
            if os.path.exists(server_dst):
                _sh4.rmtree(server_dst)
            await asyncio.to_thread(_sh4.copytree, plugin_src, plugin_dst)
            await asyncio.to_thread(_sh4.copytree, server_src, server_dst)

            # Register plugin with yt-dlp
            if plugin_dst not in _sys_top.path:
                _sys_top.path.insert(0, plugin_dst)

            # Find Deno (self-heal at runtime when the build hook failed)
            deno = await asyncio.to_thread(ensure_deno_runtime, logger)
            if not deno:
                _info("BGUTIL_WARN", "⚠️ Deno not found — bgutil downloaded but provider inactive. Songs will still play via direct cookie path.")
                return

            # deno install
            def _deno_install():
                _ensure_deno_cache_env()
                return subprocess.run(
                    [deno, "install", "--allow-scripts=npm:canvas", "--frozen"],
                    cwd=server_dst, capture_output=True, timeout=180,
                    env={**os.environ},
                )
            res = await asyncio.to_thread(_deno_install)
            if res.returncode != 0:
                _info("BGUTIL_WARN", f"⚠️ bgutil deno install error: {res.stderr.decode()[:300]}")
                return

            # Verify
            gen_once = os.path.join(server_dst, "src", "generate_once.ts")
            if os.path.isfile(gen_once):
                _BGUTIL_ACTIVE = True
                # Bring the warm HTTP provider up right away.
                with contextlib.suppress(Exception):
                    asyncio.create_task(warm_up_bgutil_server())
                _info("BGUTIL", "✅ bgutil runtime install complete — PO-token provider active!")
            else:
                _info("BGUTIL_WARN", "⚠️ bgutil: generate_once.ts missing after install")

            # Cleanup
            def _cleanup():
                import shutil as _sh5
                _sh5.rmtree(tmp_ext, ignore_errors=True)
                try:
                    os.unlink(tmp_zip)
                except Exception:
                    pass
            await asyncio.to_thread(_cleanup)

        except Exception as _be:
            _info("BGUTIL_ERR", f"⚠️ bgutil runtime install failed: {_be}. Continuing with cookie-only path.")


def _build_extractor_args(yt_args: dict) -> dict:
    """Wrap youtube extractor args and add bgutil PO-token provider if installed.

    bgutil (https://github.com/Brainicism/bgutil-ytdlp-pot-provider) provides
    real Proof-of-Origin tokens to yt-dlp, which YouTube now requires for
    cloud/datacenter IPs. Without it, most innertube clients return
    'No video formats found!' on Heroku/Railway/Render.

    The provider is installed by bin/post_compile at Heroku build time.
    On a fresh deploy it will be missing (shows ⚠️ above); redeploy to get it.
    """
    providers: dict = {"youtube": yt_args}
    providers.update(_bgutil_provider_args())
    return providers


def _yt_base_opts(out_tmpl: str, client: str, fmt: str) -> dict:
    """Build yt-dlp options with all stability/bypass techniques applied.

    No-cookie path: Tier-1 clients (tv_embedded, android_vr, web_creator) get
    player_skip=["webpage"] to bypass the "Sign in to confirm" bot-check gate.
    Side-effect: these clients only receive a limited innertube format manifest
    (combined A/V streams, no separate DASH audio), so 'bestaudio' can fail.

    Cookie path: player_skip is NOT applied. With real cookies, 'web'/'android'
    clients authenticate fully and get the complete DASH manifest including
    separate audio-only streams — 'bestaudio' works correctly.
    """
    # BUG FIX (Heroku: "Failed to extract any player response"):
    # player_skip=["webpage"] ab har client ko tod deta hai — YouTube bina
    # webpage ke player response hi nahi bhejta. Isliye player_skip hata diya
    # aur Melody wala formats=missing_pot laga diya, jisse PO-token na milne
    # par bhi formats visible rehte hain.
    ext_args_yt: dict = {
        "player_client": yt_clients([client]),
        "formats": ["missing_pot"],
    }


    class _QuietYtdlpLogger:
        def debug(self, msg): pass
        def warning(self, msg): pass
        def error(self, msg): pass

    return {
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietYtdlpLogger(),
        "nocheckcertificate": True,        # technique #14
        "geo_bypass": True,                # technique #12
        "geo_bypass_country": "US",        # spoof US location for regional blocks
        "force_ipv4": True,                # technique #11
        # prefer_free_formats removed: it filters out m4a/mp4 which are
        # YouTube's primary audio formats on server IPs, causing empty format
        # lists ("No video formats found!"). Codec preference is handled by
        # the postprocessors (opus output) instead.

        # BUG FIX: 30→15s — faster fail-over across the 13-client ladder.
        # socket_timeout governs TCP idle, not total extraction, so 15s
        # is still generous while preventing one stuck client from blocking
        # the event loop for up to 13×30=390 s per song.
        "socket_timeout": 15,              # technique #33
        "retries": 3,                      # technique #32
        # BUG FIX: extractor_retries 5→1 on cloud/Heroku IPs.
        # "No video formats found!" is a SYSTEMATIC YouTube block, not a
        # transient glitch — retrying the same client 5 times just wastes
        # 10-15s per client before moving on. With 1 retry we fail fast
        # and the outer client loop reaches tv_embedded/Piped much sooner.
        "extractor_retries": 1,            # fail fast on datacenter IP blocks
        "fragment_retries": 5,             # technique #30
        "noplaylist": True,
        "concurrent_fragment_downloads": 1, # technique #34
        "http_chunk_size": 10485760,        # technique #31 — 10MB chunks
        # BUG FIX: skip CDN HEAD probes — YouTube's googlevideo CDN blocks
        # HEAD requests from datacenter IPs with 403, causing yt-dlp to
        # report 'Requested format is not available' even when the URL is
        # fine. check_formats=False skips the probe; ffmpeg validates on open.
        "check_formats": False,
        "ignore_no_formats_error": True,
        "format": fmt,
        "outtmpl": out_tmpl,
        # HTTP headers — realistic, ROTATING browser fingerprint (jugad #10)
        # reduces bot-check triggers far better than one static UA reused
        # for every single request.
        "http_headers": {
            "User-Agent": random_ua(),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            "Sec-Fetch-Mode":  "navigate",
            "Sec-Fetch-Site":  "none",
        },
        # Native Opus output (jugad #7): PyTgCalls streams voice-chat audio
        # as Opus over WebRTC regardless of what we feed it — handing it an
        # MP3 means it has to decode MP3 → PCM → re-encode Opus on every
        # play(). Producing Opus directly here skips that extra transcode
        # generation, which cuts CPU/RAM per track and shaves startup
        # latency versus the old "always MP3" pipeline.
        # BUG FIX: only ask ffmpeg to do this if it's actually available —
        # otherwise yt-dlp's automatic fixup postprocessors still try to
        # invoke a nonexistent ffmpeg and crash with "ffprobe and ffmpeg
        # not found", even though our own postprocessor list is empty.
        **_youtube_audio_opts(),  # preserve source audio for faster startup
        # FFmpeg reconnect + low-latency flags (techniques #27-29, jugad #6):
        # nobuffer/low_delay/tiny probesize shrink startup latency so a
        # track begins streaming to the voice chat almost immediately
        # instead of waiting on ffmpeg's default input analysis window.
        "external_downloader_args": {
            "ffmpeg_i": [
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-probesize", "32k",
                "-analyzeduration", "0",
            ]
        },
        # curl-cffi TLS impersonation (jugad #42) — makes all HTTP(S) requests
        # from yt-dlp look like real Chrome at the TLS/JA3 fingerprint level.
        # YouTube and googlevideo CDN use TLS fingerprinting to detect cloud IPs;
        # impersonating Chrome bypasses this check without needing a proxy.
        **_get_impersonate_opt(),
        # ── MUSICBOT JUGAD ── js_runtimes + remote_components ──────────────
        # YouTube now requires a JavaScript runtime to solve its player challenge.
        # Without Deno + ejs:github this entire client ladder returns
        # "No video formats found!" on Heroku/cloud IPs regardless of cookies.
        # bin/post_compile installs Deno to vendor/deno/bin/deno at build time.
        **_get_js_runtime_opts(),
        # Innertube client + bot-check bypass + bgutil PO-token provider
        # bgutil is auto-installed at runtime on first use (_ensure_bgutil_runtime).
        # It generates Proof-of-Origin tokens that YouTube now requires for
        # cloud-IP requests — without it most clients return "No video formats".
        "extractor_args": _build_extractor_args(ext_args_yt),
        # BUG FIX: DO NOT inject cookies for Tier-1 no-PO-token clients.
        # tv_embedded / android_vr / web_creator / android_testsuite use
        # player_skip=["webpage"] to bypass the sign-in gate entirely —
        # they work COOKIE-FREE by design. Injecting a cookiefile for these
        # clients can confuse yt-dlp's auth flow and produce "No video
        # formats found!" even when the cookie-free path would succeed.
        # Cookies are only useful for authenticated DASH clients (web,
        # android, ios) that can leverage the full session manifest.
        # ROOT-CAUSE FIX: _YT_NO_POTOKEN_CLIENTS is an empty set, so a cookie
        # jar was attached to EVERY client — including android/ios/tv_simply,
        # which yt-dlp then skips entirely ("does not support cookies"),
        # leaving zero clients and the exact "No video formats found!" error
        # from the log. Ask the real cookie-capability map instead.
        **({} if not _clients_accept_cookies([client]) else _cookie_opts()),
    }


def _yt_title_score(title: str, query: str) -> float:
    """Simple word-overlap relevance score (0.0–1.0) between a result title and query.
    Higher = better match. Used to rank ytsearch5 results."""
    import re as _re
    _tok = lambda s: set(_re.findall(r'\w+', s.lower()))
    t_words = _tok(title)
    q_words = _tok(query)
    if not q_words:
        return 0.0
    overlap = t_words & q_words
    return len(overlap) / len(q_words)


async def _yt_try_download(search_target: str, out_tmpl: str, client_name: str,
                            fmt: str, logger=None) -> dict | None:
    """Single download attempt for one client+format combo.
    Returns info dict or None. Cleans up partial files on failure (technique #40).

    When search_target is ytsearch5:..., the top-5 results are scored by title
    similarity to the query and the best-matching video is downloaded (not just
    the first result Telegram/YouTube would return).
    """
    logger = logger or (lambda *a: None)

    # ── Title-relevance pre-selection for ytsearch5 ───────────────────────
    # Phase: extract metadata for all 5 candidates without downloading, pick
    # the closest title match, then download only that one by video URL.
    best_url: str | None = None
    if search_target.startswith("ytsearch5:"):
        raw_query = search_target[len("ytsearch5:"):]
        info_opts = {
            **_yt_base_opts(out_tmpl, client_name, fmt),
            "extract_flat": True,   # metadata only, no download
            "skip_download": True,
            "quiet": True,
            # CRITICAL FIX: ytsearch1:/ytsearch5: returns a "SearchResultsPlaylist"
            # internally. noplaylist=True (default in _yt_base_opts) makes yt-dlp
            # 2025.x+ refuse to process it → silent empty result.
            "noplaylist": False,
        }
        info_opts.pop("postprocessors", None)
        info_opts.pop("outtmpl", None)

        def _info_run():
            try:
                with yt_dlp.YoutubeDL(info_opts) as ydl:
                    info = ydl.extract_info(search_target, download=False)
                    entries = (info or {}).get("entries") or []
                    return [e for e in entries if e]
            except Exception:
                return []

        try:
            candidates = await asyncio.to_thread(_info_run)
            if candidates:
                best = max(
                    candidates,
                    key=lambda e: _yt_title_score(e.get("title", ""), raw_query),
                )
                best_url = best.get("url") or best.get("webpage_url")
                logger("MUSIC_YT", f"ytsearch5 → best: {best.get('title', '?')!r}")
        except Exception:
            pass

    # Actual download: use best_url if we found one, otherwise original target
    download_target = best_url if best_url else search_target
    opts = _yt_base_opts(out_tmpl, client_name, fmt)
    # CRITICAL FIX: ytsearch* targets return a "SearchResultsPlaylist" internally.
    # noplaylist=True (set in _yt_base_opts) causes yt-dlp 2025.x+ to silently
    # refuse to process it → "No video formats found" / empty result with no error.
    # Override to False for any search target; True stays for direct video URLs.
    if download_target.startswith(("ytsearch", "ytmsearch", "scsearch")):
        opts["noplaylist"] = False

    def _run():
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(download_target, download=True)
                if isinstance(info, dict) and "entries" in info:
                    entries = [e for e in (info.get("entries") or []) if e]
                    info = entries[0] if entries else None
                return info
        except Exception:
            return None

    try:
        info = await asyncio.to_thread(_run)
        if info:
            # Primary: FFmpeg-converted native Opus (jugad #7 — see _yt_base_opts),
            # warna jo bhi raw audio (m4a/webm/ogg) disk par utra hai — pytgcalls
            # unhe natively decode kar leta hai.  Path yt-dlp se hi lete hain,
            # isliye custom outtmpl fields ya postprocessor ki badli hui extension
            # se ab koi successful download discard nahi hota.
            found = _resolve_downloaded_file(info, out_tmpl)
            if found:
                return {
                    "title":     info.get("title", search_target),
                    "file_path": found,
                    "duration":  int(info.get("duration") or 0),
                    "thumbnail": info.get("thumbnail"),
                    "source":    "youtube",
                }
            logger("MUSIC_DL_ERR",
                   f"yt_dl [{client_name}]: file missing after download ({out_tmpl})")
    except Exception:
        pass

    # Technique #40: cleanup partial files
    for ext in ("mp3", "m4a", "webm", "opus", "part", "ytdl"):
        p = out_tmpl.replace("%(ext)s", ext)
        if os.path.exists(p):
            try: os.remove(p)
            except Exception: pass
    return None


async def _yt_try_piped(video_id: str, out_tmpl: str, logger=None) -> dict | None:
    """Technique #37: Piped.video open-source API fallback for known video IDs."""
    logger = logger or (lambda *a: None)
    # BUG FIX: this used the bare name `aiohttp`, which is never imported in
    # this module (the import is aliased to `_aiohttp`). Every Piped fallback
    # attempt therefore died with NameError instead of returning a stream.
    if not video_id or yt_dlp is None or _aiohttp is None:
        return None
    for api in _live_hosts(await _get_piped_apis()):
        url = f"{api}/streams/{video_id}"
        try:
            async with _make_aiohttp_session() as sess:
                async with sess.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()
            audio_streams = [s for s in data.get("audioStreams", [])
                             if s.get("mimeType", "").startswith("audio")]
            if not audio_streams:
                continue
            audio_streams.sort(key=lambda s: s.get("bitrate", 0), reverse=True)
            stream_url = audio_streams[0]["url"]
            title      = data.get("title", video_id)
            duration   = int(data.get("duration") or 0)

            # BUG FIX: requests+ffmpeg se download karo (yt-dlp nahi)
            # Piped stream URL googlevideo.com proxy hai — direct requests
            # se download Heroku pe kaam karta hai.
            raw_path = out_tmpl.replace("%(ext)s", "piped_raw")
            mp3_path = out_tmpl.replace("%(ext)s", "mp3")
            def _run_raw(u=stream_url):
                try:
                    with requests.get(u, stream=True, timeout=90,
                                      headers={"User-Agent": random_ua()},
                                      allow_redirects=True) as r:
                        r.raise_for_status()
                        with open(raw_path, "wb") as fh:
                            for chunk in r.iter_content(chunk_size=1 << 16):
                                if chunk: fh.write(chunk)
                except Exception:
                    pass
            await asyncio.to_thread(_run_raw)
            if not os.path.exists(raw_path) or os.path.getsize(raw_path) < 4096:
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", raw_path,
                    "-vn", "-ar", "44100", "-ac", "2", "-b:a", "320k", mp3_path,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            except Exception:
                pass
            with contextlib.suppress(Exception):
                os.remove(raw_path)
            if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 4096:
                logger("MUSIC_DL", f"Piped [{api}] ✓ {title!r}")
                return {"title": title, "file_path": mp3_path,
                        "duration": duration, "thumbnail": None, "source": "youtube"}
        except Exception:
            continue
    return None


async def _yt_try_invidious(video_id: str, out_tmpl: str, logger=None) -> dict | None:
    """Technique #38: Invidious instance fallback for known video IDs."""
    logger = logger or (lambda *a: None)
    if not video_id or yt_dlp is None:
        return None
    for instance in _live_hosts(_INVIDIOUS):
        try:
            # Use yt-dlp with invidious URL — it uses the generic extractor
            inv_url = f"{instance}/watch?v={video_id}"
            mp3_path = out_tmpl.replace("%(ext)s", "mp3")
            opts = {
                "quiet": True, "no_warnings": True,
                "nocheckcertificate": True,
                "geo_bypass": True, "force_ipv4": True,
                "format": "bestaudio/best",
                "outtmpl": out_tmpl,
                "retries": 2,
                **_audio_pp("mp3", "256"),
                "external_downloader_args": {"ffmpeg_i": ["-reconnect", "1",
                    "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]},
            }
            def _run(u=inv_url):
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.extract_info(u, download=True)
                except Exception:
                    pass
            await asyncio.to_thread(_run)
            if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 4096:
                logger("MUSIC_DL", f"Invidious [{instance}] ✓ video_id={video_id!r}")
                return {"title": video_id, "file_path": mp3_path,
                        "duration": 0, "thumbnail": None, "source": "youtube"}
        except Exception:
            continue
    return None


def _yt_extract_video_id(url_or_query: str) -> str | None:
    """Extract YouTube video ID from a URL (used before Piped/Invidious fallback)."""
    patterns = [
        r'(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/embed/)([A-Za-z0-9_-]{11})',
        r'(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})',
    ]
    for p in patterns:
        m = re.search(p, url_or_query)
        if m:
            return m.group(1)
    return None


async def piped_search_download(query: str, out_tmpl: str, logger=None) -> dict | None:
    """Search YouTube via Piped.video public API and download audio.

    Piped is an open-source YouTube frontend with its own CDN and no
    rate-limiting on public instances.  Server/datacenter IPs (Heroku,
    Railway, Render, VPS) that are blocked by YouTube directly can still
    fetch songs through Piped because all extraction happens on Piped's
    own servers — not on ours.

    Steps:
      1. GET /search?q=<query>&filter=music_songs  → get video_id
      2. GET /streams/<video_id>                   → get direct audio stream URL
      3. yt-dlp downloads the raw stream URL       → saved as audio file
    """
    logger = logger or (lambda *a: None)
    if _aiohttp is None or yt_dlp is None:
        return None

    # BUG FIX: 12s→7s per instance — Piped instances that are slow/down
    # were silently blocking the event loop for 12s each with no log output,
    # causing a visible 15-20s pause before Phase 1 kicked in.
    _timeout = _aiohttp.ClientTimeout(total=7)
    _hdrs    = {"User-Agent": random_ua()}

    for api in _live_hosts(await _get_piped_apis()):
        try:
            # ── Step 1: Search ──────────────────────────────────────────
            video_id = title = None
            duration = 0
            thumbnail = None

            for flt in ("music_songs", "videos"):
                try:
                    async with _make_aiohttp_session() as sess:
                        async with sess.get(
                            f"{api}/search",
                            params={"q": query, "filter": flt},
                            headers=_hdrs,
                            timeout=_timeout,
                        ) as r:
                            if r.status != 200:
                                # BUG FIX: log non-200 so dead Piped instances
                                # are visible in Heroku logs (was silent before)
                                _mark_host_dead(api)
                                logger("MUSIC_DL_ERR", f"piped_search [{api}]: HTTP {r.status}")
                                continue
                            data = await r.json(content_type=None)
                    items = [i for i in (data.get("items") or [])
                             if i.get("type") in ("stream", "video", None)]
                    if not items:
                        continue
                    item      = items[0]
                    raw_url   = item.get("url", "")
                    m         = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", raw_url)
                    if not m:
                        m = re.search(r"/([A-Za-z0-9_-]{11})$", raw_url)
                    if not m:
                        continue
                    video_id  = m.group(1)
                    title     = item.get("title", query)
                    duration  = int(item.get("duration") or 0)
                    thumbnail = item.get("thumbnail") or item.get("thumbnailUrl")
                    break
                except Exception:
                    continue

            if not video_id:
                continue

            # ── Step 2: Get stream URL ──────────────────────────────────
            try:
                async with _make_aiohttp_session() as sess:
                    async with sess.get(
                        f"{api}/streams/{video_id}",
                        headers=_hdrs,
                        timeout=_timeout,
                    ) as r:
                        if r.status != 200:
                            continue
                        sd = await r.json(content_type=None)
            except Exception:
                continue

            title     = sd.get("title") or title
            duration  = int(sd.get("duration") or duration)
            thumbnail = sd.get("thumbnailUrl") or thumbnail
            a_streams = [s for s in (sd.get("audioStreams") or []) if s.get("url")]
            if not a_streams:
                continue
            a_streams.sort(key=lambda s: s.get("bitrate", 0), reverse=True)
            stream_url = a_streams[0]["url"]

            # ── Step 3: Download via requests+ffmpeg (yt-dlp nahi) ──────
            # BUG FIX: yt-dlp Piped stream URL ko YouTube extractor se handle
            # karta tha aur googlevideo.com hit karta tha → Heroku pe block.
            # Ab: requests se seedha Piped proxy URL download karo (Piped apne
            # servers se serve karta hai), ffmpeg se mp3 banao. JioSaavn wali
            # same approach — Heroku pe 100% kaam karti hai.
            raw_path = out_tmpl.replace("%(ext)s", "piped_raw")
            mp3_path = out_tmpl.replace("%(ext)s", "mp3")

            def _dl_raw(url=stream_url):
                try:
                    with requests.get(url, stream=True, timeout=90,
                                      headers={"User-Agent": random_ua()},
                                      allow_redirects=True) as r:
                        r.raise_for_status()
                        with open(raw_path, "wb") as fh:
                            for chunk in r.iter_content(chunk_size=1 << 16):
                                if chunk:
                                    fh.write(chunk)
                except Exception:
                    pass

            await asyncio.to_thread(_dl_raw)
            if not os.path.exists(raw_path) or os.path.getsize(raw_path) < 4096:
                continue  # is instance ne kaam nahi kiya, agli try karo

            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", raw_path,
                    "-vn", "-ar", "44100", "-ac", "2", "-b:a", "320k", mp3_path,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            except Exception:
                pass
            with contextlib.suppress(Exception):
                os.remove(raw_path)

            if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 4096:
                logger("MUSIC_DL", f"Piped ✓ [{api}] '{title[:50]}'")
                return {"title": title, "file_path": mp3_path,
                        "duration": duration, "thumbnail": thumbnail,
                        "source": "piped"}

        except Exception as exc:
            _mark_host_dead(api)
            logger("MUSIC_DL_ERR", f"piped_search [{api}]: {exc}")
            continue

    return None


async def _yt_search_via_invidious(query: str, out_tmpl: str, logger=None) -> dict | None:
    """Search + download in one shot via Invidious's own API (no youtube.com
    contact at all — the search and the stream URL both come from the
    Invidious instance, which does its own extraction server-side)."""
    logger = logger or (lambda *a: None)
    if _aiohttp is None or yt_dlp is None:
        return None
    # BUG FIX: timeout 10s→5s per instance — dead instances were blocking
    # the event loop for 10s each; with 9 instances that's up to 90s of
    # waiting. 5s is still generous for a live instance over HTTPS.
    _timeout = _aiohttp.ClientTimeout(total=5)
    for instance in _live_hosts(_INVIDIOUS):
        try:
            _hdrs = {"User-Agent": random_ua()}
            async with _make_aiohttp_session() as sess:
                async with sess.get(f"{instance}/api/v1/search",
                                     params={"q": query, "type": "video"},
                                     headers=_hdrs, timeout=_timeout) as r:
                    if r.status != 200:
                        _mark_host_dead(instance)
                        logger("MUSIC_DL_ERR", f"_yt_search_via_invidious [{instance}]: HTTP {r.status}")
                        continue
                    results = await r.json(content_type=None)
            if not results:
                continue
            top      = results[0]
            title    = top.get("title") or query
            video_id = top.get("videoId")
            if not video_id:
                continue
            result = await _yt_try_invidious(video_id, out_tmpl, logger)
            if result:
                result["title"] = result.get("title") or title
                return result
        except Exception as exc:
            _mark_host_dead(instance)
            logger("MUSIC_DL_ERR", f"_yt_search_via_invidious [{instance}]: {exc}")
            continue
    return None


async def youtube_search_download(query: str, out_tmpl: str, logger=None) -> dict | None:
    """Strict bounded audio race: direct CDN and parallel download together."""
    logger = logger or (lambda *a: None)
    if yt_dlp is None:
        return None

    async def _parallel_round(round_no: int):
        # One native yt-dlp worker at a time. Parallel fallback workers were
        # the memory multiplier, and cancelling to_thread() does not stop
        # native work already running in the executor.
        workers = [
            ("web", "bestaudio[ext=m4a]/bestaudio/best"),
        ]
        tasks=[]
        for client, fmt in workers:
            target=out_tmpl.replace("%(ext)s", f"_parallel{round_no}_{client}.%(ext)s")
            tasks.append(asyncio.create_task(asyncio.to_thread(
                _cloud_download_sync, query, target, True, logger,
                [(fmt, [client], False)])))
        try:
            for task in asyncio.as_completed(tasks):
                try: result=await task
                except Exception: result=None
                if result and result.get("file_path") and os.path.exists(result["file_path"]):
                    return result
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return None

    async def _parallel_download():
        for round_no in (1, 2):
            try:
                # Direct CDN winners remain immediate; let a valid local
                # web_safari/web worker finish instead of discarding it early.
                result=await asyncio.wait_for(_parallel_round(round_no), timeout=30.0)
            except Exception as exc:
                logger("MUSIC_PARALLEL_ERR", f"round={round_no}: {type(exc).__name__}: {exc!r}"); result=None
            if result: return result
            if round_no == 1: await asyncio.sleep(0.1)
        return None

    async def _direct_provider(fn):
        try:
            result = await fn(query, logger=logger, allow_live=False)
            url = (result or {}).get("stream_url") if result else None
            if result and url and await _direct_stream_is_live(url):
                return result
        except Exception as exc:
            logger("MUSIC_DIRECT_PROVIDER_ERR", f"{getattr(fn, '__name__', 'provider')}: {type(exc).__name__}: {exc!r}")
        return None

    async def _piped_to_local_fast():
        """Convert a fast Piped audio URL to a local file before playback."""
        try:
            hit = await asyncio.wait_for(
                zero_disk_piped_lookup(query, logger=logger), timeout=7.0)
            url = (hit or {}).get("stream_url") if hit else None
            if not url:
                return None
            fast_path = out_tmpl.replace("%(ext)s", "piped_fast.mp3")
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                "-i", url, "-vn", "-ac", "2", "-ar", "44100",
                "-c:a", "libmp3lame", "-b:a", "128k", fast_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=8.0)
            if proc.returncode == 0 and os.path.exists(fast_path) and os.path.getsize(fast_path) > 4096:
                logger("MUSIC_FAST_PATH", f"Piped-to-local ✓ {hit.get('title', query)!r}")
                return {"title": hit.get("title", query), "file_path": fast_path,
                        "duration": hit.get("duration", 0),
                        "thumbnail": hit.get("thumbnail"), "source": "piped-local"}
        except Exception as exc:
            logger("MUSIC_FAST_PATH_MISS", f"{type(exc).__name__}: {exc}")
        return None
    async def _race():
        # Both candidates produce local files. The Piped conversion is bounded;
        # yt-dlp remains the deterministic fallback.
        fast_task = asyncio.create_task(_piped_to_local_fast())
        download_task = asyncio.create_task(_parallel_download())
        all_tasks = (fast_task, download_task)
        try:
            for task in asyncio.as_completed(all_tasks):
                try:
                    result = await task
                except Exception as exc:
                    logger("MUSIC_RACE_ERR", f"{type(exc).__name__}: {exc!r}"); result = None
                if result:
                    return result
            return None
        finally:
            for task in all_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*all_tasks, return_exceptions=True)
    # Only one disk/yt-dlp race may run at a time on a low-memory dyno.
    # Cancelling asyncio.to_thread tasks does not stop their native work, so
    # overlapping .play requests otherwise leave several ffmpeg/yt-dlp jobs alive.
    async with _MUSIC_DOWNLOAD_LOCK:
        try:
            result=await asyncio.wait_for(_race(), timeout=40.0)
        except asyncio.TimeoutError:
            logger("MUSIC_RACE_TIMEOUT", f"bounded 40s deadline: {query!r}")
            result=None
        except Exception as exc:
            logger("MUSIC_RACE_ERR", repr(exc)); result=None
    if result:
        logger("MUSIC_YT", f"✓ [direct+parallel-race] {result.get('title', query)!r}")
        return result
    logger("MUSIC_YT_FAIL", f"Direct+parallel race failed within strict deadline: {query!r}")
    return None
async def youtube_video_download(query: str, out_tmpl: str, logger=None) -> dict | None:
    """YouTube VIDEO download — same 40-jugad client chain, returns mp4.
    Uses 720p cap to keep file size manageable for voice chat streaming."""
    logger = logger or (lambda *a: None)
    if yt_dlp is None:
        return None

    # yt-dlp updates run in the startup warm-up, never in a user request.
    is_direct = bool(_URL_RE.match(query.strip()))

    # FAST PATH: Data API v3 se query → watch URL (see yt_api_search).
    api_title = None
    if not is_direct and yt_api_enabled():
        _api = await yt_api_search(query, logger, video=True)
        if _api:
            query     = _api["url"]
            api_title = _api["title"]
            is_direct = True

    norm_q    = _yt_normalize_query(query)
    simp_q    = _yt_simplify_query(norm_q)
    ts        = int(time.time() * 1000)

    if is_direct:
        search_targets = [query.strip()]
    else:
        search_targets = [
            f"ytsearch1:{norm_q}",
            f"ytmsearch1:{norm_q}",
            f"ytsearch1:{simp_q}",
        ]

    # BUG FIX: Video format chain.
    # Old code always tried video_fmts[0] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]'.
    # YouTube no longer reliably serves m4a DASH audio from datacenter IPs →
    # 'Requested format is not available' on every attempt, looping through
    # ALL clients without ever succeeding.
    # Fix: (1) prefer single-container formats (no mux needed → faster),
    #      (2) drop m4a from the chain, (3) cycle ALL formats per client,
    #      (4) check_formats=False to skip CDN HEAD probes that return 403,
    #      (5) inject cookies, (6) accept any output extension.
    # BUG FIX (logs: repeated "Requested format is not available"):
    # har format alag attempt tha, isliye ek client ke liye 4 baar extraction
    # chalti thi aur kisi bhi format ke na milne par poora ladder loop karta
    # tha. Ab har entry khud slash-fallback chain hai — yt-dlp ek hi
    # extraction me neeche gir jaata hai — aur aakhri entry "best" hai jo
    # kabhi fail nahi hoti. Do hi attempts per client.
    video_fmts = [
        ("best[height<=720][vcodec!=none][acodec!=none]/"
         "bestvideo[height<=720]+bestaudio/"
         "best[height<=720]/"
         "bestvideo+bestaudio/best"),
        "best",
    ]

    for search_target in search_targets:
        for client_idx, client in enumerate(_YT_CLIENTS):
            for fmt_idx, vid_fmt in enumerate(video_fmts):
                vid_tmpl = out_tmpl.replace("%(ext)s", f"vid_{client_idx}_{fmt_idx}_{ts}.%(ext)s")
                opts = {
                    "quiet": True, "no_warnings": True,
                    "nocheckcertificate": True,
                    "geo_bypass": True, "force_ipv4": True,
                    "socket_timeout": 15,           # BUG FIX: 30→15s
                    "retries": 2, "fragment_retries": 3,
                    "noplaylist": True,
                    "concurrent_fragment_downloads": 1,
                    "format": vid_fmt,
                    "check_formats": False,          # BUG FIX: skip CDN 403 probes
                    "merge_output_format": "mp4",
                    "outtmpl": vid_tmpl,
                    "external_downloader_args": {"ffmpeg_i": [
                        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                    ]},
                    # missing_pot: PO-token na mile to bhi formats hide na ho.
                    "extractor_args": {"youtube": {
                        "player_client": yt_clients([client]),
                        "formats": ["missing_pot"],
                    }},
                    # Cookies only for clients that support them — attaching a
                    # cookie jar to android/ios/tv_simply makes yt-dlp skip the
                    # client and report "No video formats found!".
                    **(_cookie_opts() if _clients_accept_cookies([client]) else {}),
                }

                def _run(o=opts, st=search_target):
                    try:
                        with yt_dlp.YoutubeDL(o) as ydl:
                            info = ydl.extract_info(st, download=True)
                            if isinstance(info, dict) and "entries" in info:
                                entries = [e for e in (info.get("entries") or []) if e]
                                info = entries[0] if entries else None
                            return info
                    except Exception:
                        return None

                try:
                    info = await asyncio.to_thread(_run)
                    if info:
                        # BUG FIX: find actual output file — yt-dlp picks the
                        # extension based on the container (mp4/webm/mkv/avi).
                        base = vid_tmpl.replace(".%(ext)s", "")
                        found_file = None
                        for ext in ("mp4", "webm", "mkv", "avi"):
                            c = f"{base}.{ext}"
                            if os.path.exists(c) and os.path.getsize(c) > 8192:
                                found_file = c
                                break
                        if not found_file:
                            import glob as _g
                            ms = [f for f in _g.glob(f"{base}.*")
                                  if os.path.getsize(f) > 8192]
                            found_file = ms[0] if ms else None
                        if found_file:
                            logger("MUSIC_YT_VID", f"✓ [{client}] {info.get('title', query)!r}")
                            return {
                                "title":     info.get("title") or api_title or query,
                                "file_path": found_file,
                                "duration":  int(info.get("duration") or 0),
                                "thumbnail": info.get("thumbnail"),
                                "source":    "youtube",
                            }
                except Exception:
                    pass

        if client_idx < len(_YT_CLIENTS) - 1:
            await asyncio.sleep(0.2)

    return None
