"""
MongoDB-backed durable persistence for data/config.json.

Heroku dynos have an EPHEMERAL filesystem — everything written to disk
(BOT_USERS, SAVED_STRINGS, warnings, per-account PYRO_SESSIONS, custom
commands, ...) disappears when the dyno restarts or redeploys. This module
mirrors data/config.json to MongoDB and pulls the last-synced copy back on a
fresh boot when no local copy exists (see load_config() in main.py).

Previously this mirrored the file to a GitHub repo through the Contents API.
That turned GitHub into a live database — one automated commit per config
save — which is what got the old GitHub account suspended. MongoDB does the
same job with no commits, no API rate limits and no account risk.

The module name and every public function are unchanged, so all call sites
keep working.

Enable it with:
  MONGO_URI  - MongoDB connection string (e.g. mongodb+srv://...)
Optional:
  MONGO_DB          - database name (default: "4st_userbot")
  MONGO_CONFIG_KEY  - document key for the synced config (default: "config")

If MONGO_URI is not set, every public function here is a safe, silent no-op —
the bot behaves exactly as before; it just won't survive a filesystem wipe.
"""

import os
import threading
import time

try:
    from pymongo import MongoClient
except ImportError:
    MongoClient = None

_MONGO_URI = os.environ.get("MONGO_URI", "")
_MONGO_DB  = os.environ.get("MONGO_DB", "4st_userbot")
_CONFIG_KEY = os.environ.get("MONGO_CONFIG_KEY", "config")

_client = None
_client_lock = threading.Lock()


def is_enabled() -> bool:
    """True only if pymongo is available AND MONGO_URI is set. Every other
    function checks this first and no-ops if it's False, so callers never
    need to branch on it themselves."""
    return bool(MongoClient and _MONGO_URI)


def _collection():
    """Lazily create one shared client (PyMongo pools connections itself)."""
    global _client
    if not is_enabled():
        return None
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=8000)
    return _client[_MONGO_DB]["kv"]


def fetch_remote_config(logger=None):
    """Return the last-synced config dict from MongoDB, or None."""
    if not is_enabled():
        return None
    log = logger or (lambda tag, msg: None)
    try:
        col = _collection()
        doc = col.find_one({"_id": _CONFIG_KEY})
        if not doc or not isinstance(doc.get("data"), dict):
            log("MONGO", "No stored config found — starting fresh.")
            return None
        log("MONGO", "Restored config from MongoDB.")
        return doc["data"]
    except Exception as e:
        log("MONGO", f"Could not restore config: {e}")
        return None


def _push_config_sync(data: dict, logger):
    log = logger or (lambda tag, msg: None)
    for attempt in range(3):
        try:
            col = _collection()
            col.update_one(
                {"_id": _CONFIG_KEY},
                {"$set": {"data": data, "updated_at": time.time()}},
                upsert=True,
            )
            log("MONGO", "Config synced to MongoDB.")
            return True
        except Exception as e:
            log("MONGO", f"Sync attempt {attempt + 1} failed: {e}")
            time.sleep(1 + attempt)
    return False


def push_config_async(data: dict, logger=None):
    """Fire-and-forget config sync — never blocks the bot's event loop."""
    if not is_enabled():
        return
    threading.Thread(
        target=_push_config_sync,
        args=(data, logger),
        daemon=True,
        name="mongo-config-sync",
    ).start()
