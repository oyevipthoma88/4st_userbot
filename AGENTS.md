# Telegram workflow architecture
- Keep reusable OTP metadata, strict invite/promotion, and forwarding operations in bot_workflows.py so they can be tested without starting Telegram clients.
- Use complete peer entities for supergroups; reject limited-rights promotion in basic groups because Telegram can only grant full admin there.
- Share a per-account write gate across bulk operations and preserve Telegram FloodWait cooldowns rather than bypassing limits through management commands.
- Serialize assistant login messages and callbacks per sender and close pending clients before changing login method; do not expose sessions or passwords in notifications.
- Run new-account onboarding after successful dynamic core deployment; promote only the configured assistant bot in that account's eligible groups, never arbitrary visitors in the primary account's groups.
- Forward the received message with the owning client's API; stage messages before another account sends them and do not circumvent protected content.
