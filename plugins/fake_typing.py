"""Long-running typing indicator commands for 4ST Prime Core."""
import asyncio
import re

from telethon import events
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import SendMessageTypingAction

PLUGIN_NAME = "Fake Typing"
PLUGIN_CMDS = [".faketyping <chat_id|user_id|@username>", ".ftyping <chat_id|user_id|@username>", ".stoptyping [chat_id]"]

# Telegram's typing action expires after ~5s, so renew it before then.
_RENEW_SECONDS = 4
_tasks: dict = {}


def _parse_target(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    return int(raw) if re.fullmatch(r"-?\d+", raw) else raw


def setup(client, bot_logger=None, **_kwargs):
    async def _typing_loop(chat_id, peer):
        try:
            while True:
                try:
                    await client(SetTypingRequest(peer=peer, action=SendMessageTypingAction()))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Flood waits / temporary errors: back off and keep going until owner stops.
                    wait = getattr(exc, "seconds", None) or 10
                    if bot_logger:
                        bot_logger("FAKE_TYPING", f"{chat_id}: {exc} — retry in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                await asyncio.sleep(_RENEW_SECONDS)
        finally:
            if _tasks.get(chat_id) is asyncio.current_task():
                _tasks.pop(chat_id, None)

    @client.on(events.NewMessage(outgoing=True, pattern=r"(?is)^\.(?:faketyping|ftyping)(?:\s+(.+))?$"))
    async def fake_typing_handler(event):
        target = _parse_target(event.pattern_match.group(1))
        if target is None:
            await event.edit(
                "<blockquote>⌨️ <b>Fake Typing</b>\n"
                "Start: <code>.ftyping &lt;chat_id|user_id|@username&gt;</code>\n"
                "Stop: <code>.stoptyping [chat_id]</code></blockquote>")
            return
        try:
            peer = await client.get_input_entity(target)
            entity = await client.get_entity(peer)
            chat_id = int(entity.id)
        except Exception as exc:
            await event.edit(f"<blockquote>❌ Chat/user not found: <code>{exc}</code></blockquote>")
            return

        previous = _tasks.pop(chat_id, None)
        if previous:
            previous.cancel()
        _tasks[chat_id] = asyncio.create_task(_typing_loop(chat_id, peer))
        await event.edit(
            f"<blockquote>⌨️ <b>Fake typing ON</b> → <code>{chat_id}</code>\n"
            f"Jab tak aap stop nahi karte, typing chalti rahegi.\n"
            f"Stop: <code>.stoptyping {chat_id}</code></blockquote>")

    @client.on(events.NewMessage(outgoing=True, pattern=r"(?is)^\.stoptyping(?:\s+(.+))?$"))
    async def stop_typing_handler(event):
        raw = (event.pattern_match.group(1) or "").strip()
        if raw:
            try:
                entity = await client.get_entity(_parse_target(raw))
                ids = [int(entity.id)]
            except Exception as exc:
                await event.edit(f"<blockquote>❌ Chat/user not found: <code>{exc}</code></blockquote>")
                return
        else:
            ids = list(_tasks)

        stopped = 0
        for chat_id in ids:
            task = _tasks.pop(chat_id, None)
            if task:
                task.cancel()
                stopped += 1
        await event.edit(
            f"<blockquote>{'🛑' if stopped else 'ℹ️'} Fake typing stopped in <b>{stopped}</b> chat(s).</blockquote>")

    if bot_logger:
        bot_logger("PLUGIN", f"{PLUGIN_NAME} loaded — commands: {', '.join(PLUGIN_CMDS)}")
