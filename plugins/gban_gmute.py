from telethon import events
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import ChatBannedRights
from telethon.errors import FloodWaitError
import asyncio


async def _is_admin(client, chat_id):
    try:
        me = await client.get_me()
        p = await client.get_permissions(chat_id, me.id)
        return p.is_admin or p.is_creator
    except Exception:
        return False


async def _save(cfg, log):
    try:
        from main import save_config
        save_config(cfg)
    except Exception as e:
        log("GBAN", f"save fail: {e}")


def setup(client, cfg=None, bot_logger=None, **kwargs):
    log = bot_logger or (lambda t, m: print(f"[{t}] {m}"))
    if cfg is None:
        cfg = {}

    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.gban(?:\s+(.+))?$"))
    async def cmd_gban(event):
        if not event.reply_to_msg_id:
            return await event.edit("Reply karke .gban likho.")
        r = await event.get_reply_message()
        uid = r.sender_id
        reason = event.pattern_match.group(1) or "No reason"
        await event.edit(f"Gbanning {uid}...")
        banned = 0
        failed = 0
        total = 0
        async for d in client.iter_dialogs():
            if not (d.is_group or d.is_channel):
                continue
            if not await _is_admin(client, d.id):
                continue
            total += 1
            try:
                await client(EditBannedRequest(
                    channel=d.id, participant=uid,
                    banned_rights=ChatBannedRights(
                        until_date=None, view_messages=True, send_messages=True,
                        send_media=True, send_stickers=True, send_gifs=True,
                        send_games=True, send_inline=True, embed_links=True)))
                banned += 1
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 2)
                continue
            except Exception:
                failed += 1
                continue
            await asyncio.sleep(1.2)
        gb = cfg.get("GBANNED_USERS", {})
        gb[str(uid)] = {"reason": reason}
        cfg["GBANNED_USERS"] = gb
        await _save(cfg, log)
        await event.edit(f"Gban done\nUser: {uid}\nReason: {reason}\nBanned: {banned}\nFailed: {failed}\nTotal: {total}")

    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.ungban$"))
    async def cmd_ungban(event):
        if not event.reply_to_msg_id:
            return await event.edit("Reply karke .ungban likho.")
        r = await event.get_reply_message()
        uid = str(r.sender_id)
        gb = cfg.get("GBANNED_USERS", {})
        if uid in gb:
            del gb[uid]
            cfg["GBANNED_USERS"] = gb
            await _save(cfg, log)
            await event.edit(f"{uid} ungban.")
        else:
            await event.edit(f"{uid} list mein nahi.")

    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.gmute(?:\s+(.+))?$"))
    async def cmd_gmute(event):
        if not event.reply_to_msg_id:
            return await event.edit("Reply karke .gmute likho.")
        r = await event.get_reply_message()
        uid = str(r.sender_id)
        reason = event.pattern_match.group(1) or "No reason"
        gm = cfg.get("GMUTED_USERS", {})
        gm[uid] = {"reason": reason}
        cfg["GMUTED_USERS"] = gm
        await _save(cfg, log)
        await event.edit(f"Gmuted {uid}\nReason: {reason}\nAuto-delete ON")

    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.ungmute$"))
    async def cmd_ungmute(event):
        if not event.reply_to_msg_id:
            return await event.edit("Reply karke .ungmute likho.")
        r = await event.get_reply_message()
        uid = str(r.sender_id)
        gm = cfg.get("GMUTED_USERS", {})
        if uid in gm:
            del gm[uid]
            cfg["GMUTED_USERS"] = gm
            await _save(cfg, log)
            await event.edit(f"{uid} ungmute.")
        else:
            await event.edit(f"{uid} list mein nahi.")

    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.gbanlist$"))
    async def cmd_gbanlist(event):
        gb = cfg.get("GBANNED_USERS", {})
        if not gb:
            return await event.edit("Gban list khaali.")
        t = "Gbanned Users:\n"
        for u, d in gb.items():
            t += f"- {u} : {d.get('reason','N/A')}\n"
        await event.edit(t)

    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.gmutelist$"))
    async def cmd_gmutelist(event):
        gm = cfg.get("GMUTED_USERS", {})
        if not gm:
            return await event.edit("Gmute list khaali.")
        t = "Gmuted Users:\n"
        for u, d in gm.items():
            t += f"- {u} : {d.get('reason','N/A')}\n"
        await event.edit(t)

    @client.on(events.NewMessage(incoming=True))
    async def auto_del(event):
        try:
            gm = cfg.get("GMUTED_USERS", {})
            if gm and str(event.sender_id) in gm:
                await event.delete()
        except Exception:
            pass

    log("PLUGIN", "Gban & Gmute loaded")
