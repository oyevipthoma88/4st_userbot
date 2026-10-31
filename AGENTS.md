# Telegram workflow architecture
- Keep reusable OTP metadata, strict invite/promotion, and forwarding operations in bot_workflows.py so they can be tested without starting Telegram clients.
- Use complete peer entities for supergroups; reject limited-rights promotion in basic groups because Telegram can only grant full admin there.
- Share a per-account write gate across bulk operations and preserve Telegram FloodWait cooldowns rather than bypassing limits through management commands.
- Serialize assistant login messages and callbacks per sender and close pending clients before changing login method; do not expose sessions or passwords in notifications.
- Run new-account onboarding after successful dynamic core deployment; promote only the configured assistant bot in that account's eligible groups, never arbitrary visitors in the primary account's groups.
- Forward the received message with the owning client's API; stage messages before another account sends them and do not circumvent protected content.

- Use runtime_safety.py for polling cooldowns, fresh-event burst checks and shared-service callback permissions so these rules have executable tests.
- Bound security scans across accounts, restrict them to administrable supergroups and cancel monitors when their owning client disconnects to avoid retry storms.
- Main owns moderation commands; do not register overlapping plugin handlers, and inject the synchronous local/durable config saver into standalone plugins.
- Serialize durable config snapshots in submission order so older retries cannot overwrite newer account settings.
- Close shared HTTP sessions on the original event loop after cancelling producers; never send login credentials in notification messages.
- Keep OW payload rules, auto-reaction decisions and NC title generation in ow_engine.py, autoreact_engine.py and nc_engine.py (no Telegram imports) so they stay unit-tested; handlers only wire Telegram I/O.
- Key FloodWait queues by (account, chat) and bound them; one module worker per (module, chat) — a new start cancels the old one.
- Port third-party Pyrogram animations via plugins/aaa_stranger_animations.py shim (vendored sources in plugins/_stranger_anims/); replace author branding with the running account's name at send time, and let other animation plugins skip commands it owns.
- Active Grid visibility goes through runtime_safety.grid_core_visible using CORE_CONTROLLERS (core -> assistant user who logged it in); owner sees all.
