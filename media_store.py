"""
Permanent storage for the /start visual media (start picture / video).

WHY THIS EXISTS
---------------
Heroku dynos have an EPHEMERAL filesystem. The old flow downloaded the photo
the owner sent into data/ and stored only the local path in
CONFIG["START_MEDIA_PATH"]. On the next restart/redeploy that file was gone,
so /start silently fell back to a text-only menu — the "pic reset ho jata hai"
bug.

WHAT IT DOES NOW ("Both" storage, as requested)
-----------------------------------------------
1. The file bytes are committed to the GitHub repo (Contents API) under
   `media/start_media.<ext>` — a REAL permanent copy that survives dyno wipes,
   redeploys and even a full app rebuild.
2. The reference (path, sha, raw download url, ext) is written into
   CONFIG, which is itself mirrored to MongoDB by github_store.py.
   So Mongo holds the pointer, GitHub holds the bytes.

On boot, `ensure_local_media()` re-downloads the committed file to disk if it
isn't there, and returns a usable local path for Telethon.

IMPORTANT: unlike the old config-backup-to-GitHub scheme (one commit per
config save — that got an account suspended), this module commits ONLY when
the owner actually changes the start media, i.e. a handful of commits ever.

Env used (already present in app.json):
  GITHUB_TOKEN   - PAT with `contents:write` on the repo
  GITHUB_REPO    - "owner/repo"
  GITHUB_BRANCH  - default "main"
  GITHUB_MEDIA_DIR - optional, default "media"
"""

import base64
import os
import shutil
import time

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

_TOKEN     = os.environ.get("GITHUB_TOKEN", "").strip()
_REPO      = os.environ.get("GITHUB_REPO", "").strip()
_BRANCH    = os.environ.get("GITHUB_BRANCH", "main").strip() or "main"
_MEDIA_DIR = os.environ.get("GITHUB_MEDIA_DIR", "media").strip("/") or "media"

_API = "https://api.github.com"


def is_enabled() -> bool:
    """True only when we can actually talk to GitHub. Every public function
    checks this and degrades to a silent no-op otherwise, so the bot keeps
    working exactly as before when the vars aren't set."""
    return bool(requests and _TOKEN and "/" in _REPO)


def _headers():
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_sha(path: str):
    """Existing blob sha for `path`, or None. Required to overwrite a file."""
    try:
        r = requests.get(
            f"{_API}/repos/{_REPO}/contents/{path}",
            headers=_headers(), params={"ref": _BRANCH}, timeout=20,
        )
        if r.status_code == 200:
            return r.json().get("sha")
    except Exception:
        pass
    return None


def upload_media(local_path: str, logger=None):
    """Commit `local_path` into the repo. Returns a reference dict on success:

        {"path": "media/start_media.jpg",
         "raw":  "https://raw.githubusercontent.com/...",
         "sha":  "...", "ts": 1700000000.0}

    or None when GitHub storage is disabled / the push failed. Callers must
    keep working with just the local path in that case.
    """
    log = logger or (lambda tag, msg: None)
    if not is_enabled():
        log("MEDIA_STORE", "GitHub storage disabled (GITHUB_TOKEN/REPO unset).")
        return None
    if not local_path or not os.path.exists(local_path):
        return None

    ext = os.path.splitext(local_path)[1].lower() or ".jpg"
    repo_path = f"{_MEDIA_DIR}/start_media{ext}"

    try:
        with open(local_path, "rb") as fh:
            content_b64 = base64.b64encode(fh.read()).decode()
    except Exception as e:
        log("MEDIA_STORE", f"read failed: {e}")
        return None

    payload = {
        "message": "chore(media): update start media",
        "content": content_b64,
        "branch": _BRANCH,
    }
    sha = _get_sha(repo_path)
    if sha:
        payload["sha"] = sha

    for attempt in range(3):
        try:
            r = requests.put(
                f"{_API}/repos/{_REPO}/contents/{repo_path}",
                headers=_headers(), json=payload, timeout=45,
            )
            if r.status_code in (200, 201):
                data = r.json()
                ref = {
                    "path": repo_path,
                    "raw": (f"https://raw.githubusercontent.com/"
                            f"{_REPO}/{_BRANCH}/{repo_path}"),
                    "sha": (data.get("content") or {}).get("sha", ""),
                    "ts": time.time(),
                }
                log("MEDIA_STORE", f"Start media committed to {repo_path}")
                return ref
            log("MEDIA_STORE", f"push {r.status_code}: {r.text[:180]}")
        except Exception as e:
            log("MEDIA_STORE", f"push attempt {attempt + 1} failed: {e}")
        time.sleep(1 + attempt)
    return None


def download_media(ref: dict, dest_dir: str, logger=None):
    """Re-materialise the committed media on local disk. Returns the local
    path, or None. Safe to call on every boot — it's a no-op when the file is
    already present."""
    log = logger or (lambda tag, msg: None)
    if not ref or not isinstance(ref, dict) or not requests:
        return None

    repo_path = ref.get("path") or ""
    ext = os.path.splitext(repo_path)[1] or ".jpg"
    local_path = os.path.join(dest_dir, f"start_media{ext}")
    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return local_path

    urls = []
    if ref.get("raw"):
        urls.append(ref["raw"])
    if repo_path:
        urls.append(f"https://raw.githubusercontent.com/{_REPO}/{_BRANCH}/{repo_path}")

    for url in urls:
        try:
            headers = _headers() if _TOKEN else {}
            r = requests.get(url, headers=headers, timeout=45)
            if r.status_code == 200 and r.content:
                os.makedirs(dest_dir, exist_ok=True)
                with open(local_path, "wb") as fh:
                    fh.write(r.content)
                log("MEDIA_STORE", f"Start media restored from GitHub → {local_path}")
                return local_path
            log("MEDIA_STORE", f"download {r.status_code} for {url}")
        except Exception as e:
            log("MEDIA_STORE", f"download failed: {e}")
    return None


def ensure_local_media(cfg: dict, dest_dir: str, logger=None):
    """Boot-time helper. Uses, in order:
      1. the existing local file (fast path),
      2. the GitHub copy referenced by CONFIG["START_MEDIA_REF"].
    Updates cfg["START_MEDIA_PATH"] in place and returns it (or None)."""
    path = cfg.get("START_MEDIA_PATH")
    if path and os.path.exists(str(path)):
        return path
    # Private GitHub repos return 404 from raw.githubusercontent.com even
    # though the asset is committed. Prefer the bundled release asset first.
    bundled_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
    for name in ("start_media.jpg", "start_media.jpeg", "start_media.png", "start_media.webp"):
        bundled = os.path.join(bundled_dir, name)
        if os.path.isfile(bundled) and os.path.getsize(bundled) > 0:
            target = os.path.join(dest_dir, name)
            try:
                os.makedirs(dest_dir, exist_ok=True)
                if os.path.abspath(bundled) != os.path.abspath(target):
                    shutil.copy2(bundled, target)
                cfg["START_MEDIA_PATH"] = target
                return target
            except Exception:
                return bundled
    restored = download_media(cfg.get("START_MEDIA_REF"), dest_dir, logger)
    if restored:
        cfg["START_MEDIA_PATH"] = restored
    return restored
