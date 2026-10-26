"""ShrutiBots YouTube API — fast path for .play / .vplay.

Docs: https://api.shrutibots.site/docs

Flow (target: song bajna < 5s):
  1. Query -> YouTube video_id  (link se seedha ID, warna YouTube Data API v3
     aur key-free InnerTube search ek saath race; ~0.3-1s)
  2. .play  -> GET /download?type=audio  -> chhoti m4a/webm file disk par
              (keep-alive session, chunked write, video_id ke naam se cache)
     .vplay -> /download?type=video URL seedha FFmpeg/PyTgCalls ko diya jata
              hai (bada video poora download hone ka wait nahi).

Env:
  SHRUTI_API_KEY        required — key @SHRUTIAPIBOT se milti hai
  SHRUTI_API_URL        optional — default https://api.shrutibots.site
  SHRUTI_AUDIO_TIMEOUT  optional — audio download cap (sec), default 12
  SHRUTI_SEARCH_TIMEOUT optional — search cap (sec), default 2.5
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
import uuid
from urllib.parse import urlencode

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

import music_sources

API_URL = os.environ.get("SHRUTI_API_URL", "https://api.shrutibots.site").rstrip("/")

_YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/|music\.youtube\.com/watch\?(?:.*&)?v=)"
    r"([A-Za-z0-9_-]{11})"
)
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_EXT_BY_CT = {
    "audio/mp4": "m4a", "audio/m4a": "m4a", "audio/x-m4a": "m4a",
    "audio/webm": "webm", "audio/mpeg": "mp3", "audio/ogg": "ogg",
    "video/mp4": "mp4", "video/webm": "webm", "video/x-matroska": "mkv",
}

_session: "aiohttp.ClientSession | None" = None
_dead_until = 0.0          # key quota/expiry hit -> skip API for a while
_last_error = ""


def _log(logger, tag, msg):
    if logger:
        with contextlib.suppress(Exception):
            logger(tag, msg)


def api_key() -> str:
    return (os.environ.get("SHRUTI_API_KEY") or "").strip()


def enabled() -> bool:
    return bool(api_key()) and aiohttp is not None and time.time() >= _dead_until


def last_error() -> str:
    return _last_error


async def _get_session() -> "aiohttp.ClientSession":
    """One shared keep-alive session — saves the TLS handshake on every play."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=600),
            headers={"User-Agent": "4st-userbot/1.0"},
        )
    return _session


def extract_video_id(text: str) -> str | None:
    t = (text or "").strip()
    m = _YT_ID_RE.search(t)
    if m:
        return m.group(1)
    if _BARE_ID_RE.match(t) and not t.isalpha():
        return t
    return None


async def resolve(query: str, logger=None, video: bool = False) -> dict | None:
    """Query/link -> {"video_id","title","duration","thumbnail"}."""
    vid = extract_video_id(query)
    if vid:
        return {"video_id": vid, "title": query, "duration": 0,
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}
    if query.startswith(("http://", "https://")):
        return None  # non-YouTube link — let the old resolver handle it

    jobs = [asyncio.create_task(music_sources.youtube_innertube_search(query, logger=logger))]
    if music_sources.yt_api_enabled():
        jobs.append(asyncio.create_task(
            music_sources.yt_api_search(query, logger=logger, video=video)))
    deadline = time.monotonic() + float(os.environ.get("SHRUTI_SEARCH_TIMEOUT", "2.5"))
    hit = None
    pending = set(jobs)
    try:
        while pending and hit is None:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            done, pending = await asyncio.wait(
                pending, timeout=left, return_when=asyncio.FIRST_COMPLETED)
            for j in done:
                with contextlib.suppress(Exception):
                    r = j.result()
                    if r and r.get("video_id"):
                        hit = r
                        break
    finally:
        for j in jobs:
            if not j.done():
                j.cancel()
    if not hit:
        _log(logger, "SHRUTI_SEARCH_MISS", repr(query))
        return None
    vid = hit["video_id"]
    return {"video_id": vid, "title": hit.get("title") or query,
            "duration": int(hit.get("duration") or 0),
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}


def download_url(video_id: str, kind: str = "audio") -> str:
    return f"{API_URL}/download?" + urlencode(
        {"url": video_id, "type": kind, "api_key": api_key()})


def _handle_error(status: int, detail: str, logger):
    global _dead_until, _last_error
    _last_error = f"{status} {detail}"
    _log(logger, "SHRUTI_API_ERR", _last_error)
    # Key-level problems: stop hammering the API for a while (avoids IP block).
    if status in (401, 403) or (status == 429 and "quota" in detail.lower()):
        _dead_until = time.time() + 300
    elif status == 429:
        _dead_until = time.time() + 15


async def fetch_audio(query: str, cache_dir: str, logger=None) -> dict | None:
    """Resolve + download audio. Returns track dict with local file_path."""
    if not enabled():
        return None
    t0 = time.perf_counter()
    meta = await resolve(query, logger=logger)
    if not meta:
        return None
    vid = meta["video_id"]
    os.makedirs(cache_dir, exist_ok=True)

    for ext in ("m4a", "webm", "mp3", "ogg"):
        cached = os.path.join(cache_dir, f"shruti_{vid}.{ext}")
        if os.path.exists(cached) and os.path.getsize(cached) > 10_000:
            _log(logger, "SHRUTI_CACHE_HIT", vid)
            return {**meta, "file_path": cached, "source": "shruti"}

    timeout = aiohttp.ClientTimeout(
        total=float(os.environ.get("SHRUTI_AUDIO_TIMEOUT", "12")), sock_connect=4)
    tmp = os.path.join(cache_dir, f"shruti_{vid}_{uuid.uuid4().hex}.part")
    try:
        sess = await _get_session()
        async with sess.get(download_url(vid, "audio"), timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if resp.status != 200 or "json" in ctype:
                detail = ""
                with contextlib.suppress(Exception):
                    detail = (await resp.json(content_type=None)).get("detail", "")
                _handle_error(resp.status, detail or ctype, logger)
                return None
            ext = _EXT_BY_CT.get(ctype, "m4a")
            with open(tmp, "wb") as fh:
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    fh.write(chunk)
        final = os.path.join(cache_dir, f"shruti_{vid}.{ext}")
        os.replace(tmp, final)
    except Exception as exc:
        _log(logger, "SHRUTI_DL_ERR", f"{vid}: {exc!r}")
        with contextlib.suppress(Exception):
            os.remove(tmp)
        return None
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)
    _log(logger, "SHRUTI_AUDIO_OK",
         f"{vid} ready in {time.perf_counter() - t0:.2f}s ({os.path.getsize(final)//1024} KB)")
    return {**meta, "file_path": final, "source": "shruti"}


async def video_stream(query: str, logger=None) -> dict | None:
    """Resolve video and return a direct stream URL (no full download)."""
    if not enabled():
        return None
    meta = await resolve(query, logger=logger, video=True)
    if not meta:
        return None
    return {**meta, "stream_url": download_url(meta["video_id"], "video"),
            "source": "shruti"}


async def fetch_video(query: str, cache_dir: str, logger=None) -> dict | None:
    """Full video download — used only as fallback when streaming fails."""
    if not enabled():
        return None
    meta = await resolve(query, logger=logger, video=True)
    if not meta:
        return None
    vid = meta["video_id"]
    os.makedirs(cache_dir, exist_ok=True)
    tmp = os.path.join(cache_dir, f"shruti_v_{vid}_{uuid.uuid4().hex}.part")
    try:
        sess = await _get_session()
        async with sess.get(download_url(vid, "video"),
                            timeout=aiohttp.ClientTimeout(total=90, sock_connect=4)) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if resp.status != 200 or "json" in ctype:
                detail = ""
                with contextlib.suppress(Exception):
                    detail = (await resp.json(content_type=None)).get("detail", "")
                _handle_error(resp.status, detail or ctype, logger)
                return None
            ext = _EXT_BY_CT.get(ctype, "mp4")
            with open(tmp, "wb") as fh:
                async for chunk in resp.content.iter_chunked(512 * 1024):
                    fh.write(chunk)
        final = os.path.join(cache_dir, f"shruti_v_{vid}.{ext}")
        os.replace(tmp, final)
    except Exception as exc:
        _log(logger, "SHRUTI_DL_ERR", f"video {vid}: {exc!r}")
        with contextlib.suppress(Exception):
            os.remove(tmp)
        return None
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)
    return {**meta, "file_path": final, "source": "shruti"}


async def close() -> None:
    global _session
    session, _session = _session, None
    if session is not None and not session.closed:
        await session.close()


async def warm_up(logger=None) -> None:
    """Open the keep-alive connection at startup so the first .play is fast."""
    if not enabled():
        return
    with contextlib.suppress(Exception):
        sess = await _get_session()
        async with sess.get(f"{API_URL}/health",
                            timeout=aiohttp.ClientTimeout(total=5)) as r:
            _log(logger, "SHRUTI_HEALTH", f"HTTP {r.status}")
