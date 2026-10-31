"""Testable safety primitives shared by Telegram polling and callbacks."""
import time


class PollBackoff:
    def __init__(self):
        self.failures = {}
        self.until = {}

    def ready(self, key):
        return time.monotonic() >= self.until.get(key, 0)

    def fail(self, key, error):
        count = min(self.failures.get(key, 0) + 1, 8)
        self.failures[key] = count
        delay = max(min(5 * 2 ** (count - 1), 300),
                    float(getattr(error, 'seconds', 0) or 0))
        if type(error).__name__ in {'ChatAdminRequiredError', 'ChannelPrivateError', 'ChannelInvalidError'}:
            delay = max(delay, 300)
        self.until[key] = time.monotonic() + delay
        return delay

    def clear(self, key):
        self.failures.pop(key, None)
        self.until.pop(key, None)

    def drop_client(self, client_id):
        for key in list(self.until):
            if key[0] == client_id:
                self.clear(key)


def recent_burst(timestamps, timestamp, now, window):
    """Old/future admin events must not trigger a present-day punishment."""
    if not 0 <= now - timestamp <= window:
        return []
    return [t for t in timestamps if 0 <= now - t <= window] + [timestamp]


def service_callback_allowed(data, is_owner):
    return is_owner or not (data in {b'rm_str', b'reboot_mine', b'reboot'}
                            or data.startswith(b'rm_str_'))


def grid_core_visible(core_id, viewer_id, is_owner, controllers):
    """Active Grid: owner sees all cores; others see their own account and
    cores they personally logged in (CORE_CONTROLLERS maps core -> viewer)."""
    if is_owner:
        return True
    try:
        core_id, viewer_id = int(core_id), int(viewer_id)
    except (TypeError, ValueError):
        return False
    if core_id == viewer_id:
        return True
    try:
        return int((controllers or {}).get(str(core_id), 0) or 0) == viewer_id
    except (TypeError, ValueError):
        return False
