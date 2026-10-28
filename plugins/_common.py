"""Shared helpers for plugins (not loaded as a plugin: name starts with '_')."""
from __future__ import annotations


def sudo_ids(cfg: dict, me_id) -> set:
    """Sudo ids of THIS core only (per-account bucket, levels 1 and 2)."""
    out = set()
    try:
        bucket = (cfg.get("SUDO_MAP") or {}).get(str(me_id)) or {}
        for lvl in ("1", "2"):
            for x in bucket.get(lvl) or []:
                try:
                    out.add(int(x))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return out


def is_authorized(event, me_id, cfg: dict) -> bool:
    """Mirror of main.verify_privileges for plugin handlers.

    Own outgoing command -> yes. Owner -> yes on the owner core, or on any
    core while MASTER_SYNC is ON. Sudo of this core -> yes. Everyone else no.
    """
    try:
        msg = getattr(event, "message", None)
        if getattr(event, "out", False) or getattr(msg, "out", False):
            return True
        sender = event.sender_id
        if sender is None:
            return False
        sender = int(sender)
        if sender == int(me_id):
            return True
        owner = int((cfg or {}).get("OWNER_ID", 0) or 0)
        if owner and sender == owner:
            if int(me_id) == owner or (cfg or {}).get("MASTER_SYNC", False):
                return True
        return sender in sudo_ids(cfg or {}, me_id)
    except Exception:
        return False
