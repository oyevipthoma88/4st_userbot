"""Single source of truth for locating (or installing) ffmpeg + ffprobe.

ROOT CAUSE of "Postprocessing: ffprobe and ffmpeg not found":
  The old code searched a list of directories for `ffmpeg` and for `ffprobe`
  *independently*, then handed yt-dlp `ffmpeg_location=dirname(ffmpeg)`.
  On Heroku the apt buildpack frequently lands only one of the two binaries
  (or lands ffmpeg in /app/.apt/usr/bin while ffprobe never gets unpacked),
  so `ffmpeg_location` pointed at a directory that had no ffprobe in it.
  yt-dlp's postprocessor requires BOTH in the location dir, so every
  download died with "ffprobe and ffmpeg not found" — which is exactly the
  cloud_dl [['tv_embedded']] / [['mweb']] / [['web']] failure loop.

FIX:
  1. Only accept a directory that contains BOTH executables.
  2. If no such directory exists, actively *install* a static build at
     runtime (static-ffmpeg wheel first, then a direct tarball download into
     a writable dir) instead of silently degrading.
  3. Symlink/copy whatever partial binaries were found into one unified dir
     so `ffmpeg_location` is always valid.
  4. Expose FFMPEG_BIN / FFPROBE_BIN / FFMPEG_DIR + ffmpeg_opts()/audio_pp()
     helpers so main.py and music_sources.py can never drift apart again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

__all__ = [
    "FFMPEG_BIN", "FFPROBE_BIN", "FFMPEG_DIR",
    "ensure_ffmpeg", "ffmpeg_opts", "audio_pp", "have_ffmpeg",
]

_HERE = os.path.dirname(os.path.abspath(__file__))

_SEARCH_DIRS = [
    os.path.join(_HERE, "vendor", "ffmpeg", "bin"),
    "/app/vendor/ffmpeg/bin",
    "/app/.apt/usr/bin",
    "/app/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
]

_STATIC_URLS = [
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-linux64-gpl.tar.xz",
]

FFMPEG_BIN: str | None = None
FFPROBE_BIN: str | None = None
FFMPEG_DIR: str | None = None

_DONE = False


def _is_exe(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _works(path: str) -> bool:
    """A binary that exists but cannot execute (wrong arch / missing shared
    libs after an apt unpack) is worse than no binary at all, because it makes
    detection succeed and postprocessing fail. Actually run it once."""
    if not _is_exe(path):
        return False
    try:
        return subprocess.run(
            [path, "-version"], capture_output=True, timeout=15
        ).returncode == 0
    except Exception:
        return False


def _pair_in(directory: str) -> tuple[str, str] | None:
    """Return (ffmpeg, ffprobe) only if BOTH live in `directory` and run."""
    f = os.path.join(directory, "ffmpeg")
    p = os.path.join(directory, "ffprobe")
    if _works(f) and _works(p):
        return f, p
    return None


def _scan_for(name: str) -> str | None:
    found = shutil.which(name)
    if _works(found or ""):
        return found
    for d in _SEARCH_DIRS:
        cand = os.path.join(d, name)
        if _works(cand):
            return cand
    return None


def _writable_bin_dir() -> str:
    for base in (os.path.join(_HERE, "vendor", "ffmpeg", "bin"),
                 "/tmp/ffmpeg-bin",
                 os.path.join(tempfile.gettempdir(), "ffmpeg-bin")):
        try:
            os.makedirs(base, exist_ok=True)
            probe = os.path.join(base, ".w")
            with open(probe, "w") as fh:
                fh.write("1")
            os.remove(probe)
            return base
        except Exception:
            continue
    return tempfile.mkdtemp(prefix="ffmpeg-")


def _unify(ffmpeg: str | None, ffprobe: str | None) -> tuple[str, str] | None:
    """Copy whatever we have into one directory so ffmpeg_location is valid."""
    if not (ffmpeg and ffprobe):
        return None
    if os.path.dirname(ffmpeg) == os.path.dirname(ffprobe):
        return ffmpeg, ffprobe
    dest = _writable_bin_dir()
    out = []
    for src, name in ((ffmpeg, "ffmpeg"), (ffprobe, "ffprobe")):
        tgt = os.path.join(dest, name)
        try:
            if os.path.abspath(src) != os.path.abspath(tgt):
                shutil.copy2(src, tgt)
                os.chmod(tgt, 0o755)
            out.append(tgt)
        except Exception:
            return ffmpeg, ffprobe  # best effort; caller still gets a pair
    return out[0], out[1]


def _try_static_ffmpeg_wheel() -> tuple[str, str] | None:
    """`static-ffmpeg` PyPI wheel — downloads + caches real binaries."""
    try:
        import static_ffmpeg  # type: ignore
        try:
            from static_ffmpeg import run as _sf_run  # type: ignore
            # This is the call that actually *fetches* the binaries; add_paths()
            # alone is a no-op when the cache is cold in some releases.
            pair = _sf_run.get_or_fetch_platform_executables_else_raise()
            if pair and len(pair) == 2 and _works(pair[0]) and _works(pair[1]):
                return pair[0], pair[1]
        except Exception:
            pass
        static_ffmpeg.add_paths()
        f, p = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if _works(f or "") and _works(p or ""):
            return f, p  # type: ignore[return-value]
    except Exception:
        pass
    return None


def _try_download_static() -> tuple[str, str] | None:
    """Last resort: pull a self-contained static tarball at runtime."""
    dest = _writable_bin_dir()
    tmp = tempfile.mkdtemp(prefix="ffdl-")
    archive = os.path.join(tmp, "ffmpeg.tar.xz")
    try:
        for url in _STATIC_URLS:
            try:
                ok = subprocess.run(
                    ["curl", "-fsSL", "--retry", "2", "--retry-delay", "2",
                     "--max-time", "180", url, "-o", archive],
                    capture_output=True, timeout=200,
                ).returncode == 0
            except Exception:
                ok = False
            if not ok or not os.path.exists(archive) or os.path.getsize(archive) < 1_000_000:
                continue
            try:
                subprocess.run(["tar", "-xf", archive, "-C", tmp],
                               capture_output=True, timeout=180)
            except Exception:
                continue
            got = {}
            for root, _dirs, files in os.walk(tmp):
                for name in ("ffmpeg", "ffprobe"):
                    if name in files and name not in got:
                        src = os.path.join(root, name)
                        tgt = os.path.join(dest, name)
                        try:
                            shutil.copy2(src, tgt)
                            os.chmod(tgt, 0o755)
                            got[name] = tgt
                        except Exception:
                            pass
            if len(got) == 2 and _works(got["ffmpeg"]) and _works(got["ffprobe"]):
                return got["ffmpeg"], got["ffprobe"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return None


def ensure_ffmpeg(logger=None) -> tuple[str | None, str | None]:
    """Resolve ffmpeg+ffprobe once, installing them if necessary."""
    global FFMPEG_BIN, FFPROBE_BIN, FFMPEG_DIR, _DONE
    if _DONE:
        return FFMPEG_BIN, FFPROBE_BIN
    _DONE = True

    log = logger or (lambda *_a, **_k: None)
    pair = None

    # 1) A directory that already holds a working PAIR (the only thing
    #    yt-dlp's ffmpeg_location can safely point at).
    for d in _SEARCH_DIRS:
        pair = _pair_in(d)
        if pair:
            break

    # 2) Split install (ffmpeg here, ffprobe there) -> unify into one dir.
    if not pair:
        pair = _unify(_scan_for("ffmpeg"), _scan_for("ffprobe"))

    # 3) Nothing usable -> install a static build for real.
    if not pair:
        log("FFMPEG", "no ffmpeg/ffprobe pair found — installing static build")
        pair = _try_static_ffmpeg_wheel() or _try_download_static()
        if pair:
            pair = _unify(pair[0], pair[1])

    if pair:
        FFMPEG_BIN, FFPROBE_BIN = pair
        FFMPEG_DIR = os.path.dirname(FFMPEG_BIN)
        cur = os.environ.get("PATH", "")
        if FFMPEG_DIR not in cur.split(":"):
            os.environ["PATH"] = FFMPEG_DIR + ":" + cur
        os.environ["FFMPEG_BINARY"] = FFMPEG_BIN
        os.environ["FFPROBE_BINARY"] = FFPROBE_BIN
        log("FFMPEG", f"ready: {FFMPEG_BIN} + {FFPROBE_BIN}")
    else:
        log("FFMPEG", "UNAVAILABLE — downloads will run in no-postprocess mode")

    return FFMPEG_BIN, FFPROBE_BIN


def have_ffmpeg() -> bool:
    return bool(FFMPEG_DIR)


def ffmpeg_opts() -> dict:
    """yt-dlp opts guaranteeing it never invokes a missing ffmpeg."""
    if FFMPEG_DIR:
        return {"ffmpeg_location": FFMPEG_DIR}
    # `fixup` postprocessors run even when postprocessors=[] — disable them,
    # otherwise yt-dlp raises the exact "ffprobe and ffmpeg not found" error.
    return {"fixup": "never", "prefer_ffmpeg": False}


def audio_pp(codec: str = "opus", quality: str = "192") -> dict:
    if not FFMPEG_DIR:
        return {"postprocessors": [], "fixup": "never", "prefer_ffmpeg": False}
    return {
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
            "preferredquality": quality,
        }],
        "ffmpeg_location": FFMPEG_DIR,
    }


# Resolve at import time so every downstream import sees the final values.
ensure_ffmpeg()
