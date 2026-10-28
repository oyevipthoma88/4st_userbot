# plugins/nc_suite.py — name-change loops
#   .raidnc <n>  .frostnc <n>  .venomnc <n>  .voidnc <n>
#   .snc <n>     .shutupnc <n> .doggync <n>
#   stop: .stopall / .stop / .stopnc / .stop<theme>nc
# Logic lives in nc_engine.py.
import asyncio

from telethon import events, errors, types
from telethon.tl.functions.channels import EditTitleRequest
from telethon.tl.functions.messages import EditChatTitleRequest

import nc_engine as nc
from plugins._common import is_authorized

PLUGIN_NAME = "NC Suite"
PLUGIN_CMDS = [".raidnc <n>", ".frostnc <n>", ".venomnc <n>", ".voidnc <n>",
               ".snc <n>", ".shutupnc <n>", ".doggync <n>", ".stopnc"]


def setup(client, cfg=None, bot_logger=None, **kwargs):
    log = bot_logger or (lambda t, m: print(f"[{t}] {m}"))
    cfg = cfg if cfg is not None else {}
    st = {"me": None, "tasks": {}}   # chat_id -> asyncio.Task (one NC per chat)

    async def me_id():
        if st["me"] is None:
            st["me"] = (await client.get_me()).id
        return st["me"]

    async def say(chat_id, text, ttl=10):
        try:
            m = await client.send_message(chat_id, text, parse_mode="html")
        except Exception:
            return
        if ttl:
            async def _later():
                await asyncio.sleep(ttl)
                try:
                    await m.delete()
                except Exception:
                    pass
            asyncio.create_task(_later())

    async def rename(entity, title):
        if isinstance(entity, types.Channel):
            await client(EditTitleRequest(channel=entity, title=title))
        else:
            await client(EditChatTitleRequest(chat_id=entity.id, title=title))

    async def cleanup_service(chat_id, my_id, limit):
        """Delete the 'X changed group name' service messages we caused."""
        ids = []
        try:
            async for m in client.iter_messages(chat_id, limit=limit):
                if isinstance(getattr(m, "action", None), types.MessageActionChatEditTitle) \
                        and m.sender_id == my_id:
                    ids.append(m.id)
            if ids:
                await client.delete_messages(chat_id, ids)
        except Exception:
            pass

    async def run(entity, chat_id, titles, delay, original, my_id, theme):
        done = 0
        try:
            for i, title in enumerate(titles):
                try:
                    await rename(entity, title)
                    done += 1
                except errors.FloodWaitError as fw:
                    if fw.seconds > 90:
                        await say(chat_id, f"<blockquote>⚠️ FloodWait <b>{fw.seconds}s</b> — NC rok diya "
                                           f"({done}/{len(titles)}).</blockquote>")
                        break
                    await asyncio.sleep(fw.seconds + 1)
                    continue
                except errors.RPCError as e:
                    name = type(e).__name__
                    if "NotModified" in name:
                        continue
                    await say(chat_id, f"<blockquote>❌ Naam nahi badal paya: <code>{name}</code></blockquote>")
                    break
                if (i + 1) % 10 == 0:
                    asyncio.create_task(cleanup_service(chat_id, my_id, 30))
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass
        finally:
            # Always restore the original title, even when stopped mid-way.
            if original:
                try:
                    await asyncio.shield(rename(entity, original))
                except Exception:
                    pass
            await cleanup_service(chat_id, my_id, min(100, done * 2 + 10))
            st["tasks"].pop(chat_id, None)
            log("NC", f"{theme} finished {done}/{len(titles)} in {chat_id}")

    @client.on(events.NewMessage(pattern=nc.CMD_RE.pattern))
    async def _nc_cmd(event):
        mid = await me_id()
        if not is_authorized(event, mid, cfg):
            return
        parsed = nc.parse(event.raw_text or "")
        if not parsed:
            return
        theme, count, custom = parsed
        try:
            await event.delete()
        except Exception:
            pass
        chat_id = event.chat_id
        if event.is_private:
            return await say(chat_id, "<blockquote>❌ NC sirf group me chalta hai.</blockquote>")
        entity = await event.get_chat()
        if isinstance(entity, types.Channel) and entity.broadcast:
            return await say(chat_id, "<blockquote>❌ Channel me nahi, group me chalao.</blockquote>")
        try:
            perms = await client.get_permissions(entity, mid)
            if not (perms.is_creator or (perms.is_admin and getattr(perms.participant, "admin_rights", None)
                                         and perms.participant.admin_rights.change_info)):
                return await say(chat_id, "<blockquote>❌ Group info change karne ka admin right nahi hai.</blockquote>")
        except Exception:
            pass   # unknown -> let Telegram decide on the first rename
        name = None
        if event.is_reply:
            try:
                r = await event.get_reply_message()
                snd = await r.get_sender() if r else None
                name = (getattr(snd, "first_name", None) or getattr(snd, "title", None) or "").strip() or None
            except Exception:
                name = None
        # One NC per chat: a new run replaces the old one (restores its title first).
        old = st["tasks"].get(chat_id)
        original = getattr(entity, "title", None)
        if old and not old.done():
            old.cancel()
            try:
                await old
            except BaseException:
                pass
            try:
                entity = await client.get_entity(chat_id)
                original = getattr(entity, "title", original)
            except Exception:
                pass
        delay = max(nc.MIN_DELAY, float(cfg.get("NC_DELAY", nc.DEFAULT_DELAY) or nc.DEFAULT_DELAY))
        titles = nc.build_titles(theme, count, name=name, custom=custom)
        task = asyncio.create_task(run(entity, chat_id, titles, delay, original, mid, theme))
        st["tasks"][chat_id] = task
        await say(chat_id, f"<blockquote>🌀 <b>{nc.THEMES[theme]} NC</b> start — <code>{count}</code> baar "
                           f"(delay {delay}s). Stop: <code>.stopall</code> / <code>.stopnc</code></blockquote>", ttl=6)

    @client.on(events.NewMessage(pattern=nc.STOP_RE.pattern))
    async def _nc_stop(event):
        mid = await me_id()
        if not is_authorized(event, mid, cfg):
            return
        txt = (event.raw_text or "").strip().lower()
        # `.stop` / `.stopall` / `.stopm` stop every NC of this account;
        # `.stopnc` / `.stop<theme>nc` stop the NC of this chat.
        everything = txt in (".stop", ".stopall", ".stopm")
        targets = list(st["tasks"].items()) if everything else \
            [(c, t) for c, t in st["tasks"].items() if c == event.chat_id]
        for _chat, t in targets:
            if not t.done():
                t.cancel()
        if targets and not everything:
            try:
                await event.delete()
            except Exception:
                pass
            await say(event.chat_id, "<blockquote>🛑 <b>NC stopped</b> — purana naam restore.</blockquote>", ttl=5)

    log("PLUGIN", f"{PLUGIN_NAME} loaded — commands: {', '.join(PLUGIN_CMDS)}")
