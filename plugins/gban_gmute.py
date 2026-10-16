# plugins/gban_gmute.py — v2.0.0
from telethon import events
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import ChatBannedRights
from telethon.errors import FloodWaitError
import asyncio

PLUGIN_NAME = "Gban & Gmute"
PLUGIN_CMDS = [
    ".gban [id/@user/reply] [reason] — Global ban",
    ".ungban [id/@user/reply] — Unban",
    ".gmute [id/@user/reply] [reason] — Global mute",
    ".ungmute [id/@user/reply] — Unmute",
    ".gbanlist — Show gbanned users",
    ".gmutelist — Show gmuted users",
]

async def _is_admin(client, chat_id):
    try:
        me = await client.get_me()
        p = await client.get_permissions(chat_id, me.id)
        return p.is_admin or p.is_creator
    except Exception:
        return False

async def _save(cfg, log):
    try:
        import github_store
        if hasattr(github_store, "push_config_async"):
            await github_store.push_config_async(cfg, logger=log)
        elif hasattr(github_store, "push_config"):
            github_store.push_config(cfg)
        log("GBAN", "Config saved")
    except Exception as e:
        log("GBAN", f"save fail: {e}")

async def _resolve_target(event, client, args):
    """(user_id, reason) — reply, ID, @username sab handle karta hai."""
    target_id = None
    reason = "No reason"

    # Reply check
    if event.reply_to_msg_id:
        r = await event.get_reply_message()
        target_id = r.sender_id
        if args:
            reason = args
        return target_id, reason

    if not args:
        return None, "❌ Reply karo ya `.gban <id|@user> [reason]` likho."

    parts = args.split(None, 1)
    first = parts[0]

    # Numeric ID try
    try:
        target_id = int(first)
    except ValueError:
        # Username try
        try:
            entity = await client.get_entity(first)
            target_id = entity.id
        except Exception as e:
            return None, f"❌ User nahi mila: `{first}`\n{type(e).__name__}"

    if len(parts) > 1:
        reason = parts[1]

    return target_id, reason

def setup(client, cfg=None, bot_logger=None, **kwargs):
    log = bot_logger or (lambda t, m: print(f"[{t}] {m}"))
    if cfg is None:
        cfg = {}
    log("PLUGIN", f"Loading {PLUGIN_NAME}")

    # ─────── GBAN ───────
    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.gban(?:\s+(.*))?$"))
    async def cmd_gban(event):
        args = (event.pattern_match.group(1) or "").strip()
        target_id, reason = await _resolve_target(event, client, args)
        if target_id is None:
            return await event.edit(reason)

        await event.edit(f"⏳ Gbanning `{target_id}`...\n📝 Reason: {reason}")

        banned = failed = total = 0
        async for d in client.iter_dialogs():
            if not (d.is_group or d.is_channel):
                continue
            if not await _is_admin(client, d.id):
                continue
            total += 1
            try:
                await client(EditBannedRequest(
                    channel=d.id,
                    participant=target_id,
                    banned_rights=ChatBannedRights(
                        until_date=None,
                        view_messages=True,
                        send_messages=True,
                        send_media=True,
                        send_stickers=True,
                        send_gifs=True,
                        send_games=True,
                        send_inline=True,
                        embed_links=True,
                    ),
                ))
                banned += 1
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 2)
                continue
            except Exception:
                failed += 1
                continue
            await asyncio.sleep(1.2)

        gb = cfg.get("GBANNED_USERS", {})
        gb[str(target_id)] = {"reason": reason}
        cfg["GBANNED_USERS"] = gb
        await _save(cfg, log)

        await event.edit(
            f"✅ **Gban Complete**\n"
            f"👤 User: `{target_id}`\n"
            f"📝 Reason: {reason}\n"
            f"🔨 Banned: **{banned}**\n"
            f"❌ Failed: **{failed}**\n"
            f"📊 Total checked: **{total}**"
        )

    # ─────── UNGBAN ───────
    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.ungban(?:\s+(.*))?$"))
    async def cmd_ungban(event):
        args = (event.pattern_match.group(1) or "").strip()
        target_id, _ = await _resolve_target(event, client, args)
        if target_id is None:
            return await event.edit("❌ Reply karo ya `.ungban <id|@user>` likho.")
        uid = str(target_id)
        gb = cfg.get("GBANNED_USERS", {})
        if uid in gb:
            del gb[uid]
            cfg["GBANNED_USERS"] = gb
            await _save(cfg, log)
            await event.edit(f"✅ `{uid}` ungban (list se hata).")
        else:
            await event.edit(f"⚠️ `{uid}` list mein nahi tha.")

    # ─────── GMUTE ───────
    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.gmute(?:\s+(.*))?$"))
    async def cmd_gmute(event):
        args = (event.pattern_match.group(1) or "").strip()
        target_id, reason = await _resolve_target(event, client, args)
        if target_id is None:
            return await event.edit(reason)
        uid = str(target_id)
        gm = cfg.get("GMUTED_USERS", {})
        gm[uid] = {"reason": reason}
        cfg["GMUTED_USERS"] = gm
        await _save(cfg, log)
        await event.edit(
            f"✅ **Gmuted** `{uid}`\n"
            f"📝 Reason: {reason}\n"
            f"🗑️ Auto-delete ON (0 sec)"
        )

    # ─────── UNGMUTE ───────
    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.ungmute(?:\s+(.*))?$"))
    async def cmd_ungmute(event):
        args = (event.pattern_match.group(1) or "").strip()
        target_id, _ = await _resolve_target(event, client, args)
        if target_id is None:
            return await event.edit("❌ Reply karo ya `.ungmute <id|@user>` likho.")
        uid = str(target_id)
        gm = cfg.get("GMUTED_USERS", {})
        if uid in gm:
            del gm[uid]
            cfg["GMUTED_USERS"] = gm
            await _save(cfg, log)
            await event.edit(f"✅ `{uid}` ungmute.")
        else:
            await event.edit(f"⚠️ `{uid}` list mein nahi tha.")

    # ─────── LISTS ───────
    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.gbanlist$"))
    async def cmd_gbanlist(event):
        gb = cfg.get("GBANNED_USERS", {})
        if not gb:
            return await event.edit("📭 Gban list khaali.")
        t = "**🚫 Gbanned Users**\n\n"
        for u, d in gb.items():
            t += f"• `{u}` — {d.get('reason', 'N/A')}\n"
        await event.edit(t)

    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.gmutelist$"))
    async def cmd_gmutelist(event):
        gm = cfg.get("GMUTED_USERS", {})
        if not gm:
            return await event.edit("📭 Gmute list khaali.")
        t = "**🔇 Gmuted Users**\n\n"
        for u, d in gm.items():
            t += f"• `{u}` — {d.get('reason', 'N/A')}\n"
        await event.edit(t)

    # ─────── AUTO-DELETE gmuted messages ───────
    @client.on(events.NewMessage(incoming=True))
    async def auto_del(event):
        try:
            gm = cfg.get("GMUTED_USERS", {})
            if gm and str(event.sender_id) in gm:
                await event.delete()
        except Exception:
            pass

    log("PLUGIN", f"{PLUGIN_NAME} loaded")
