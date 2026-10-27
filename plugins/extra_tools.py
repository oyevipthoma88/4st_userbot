import time
from datetime import datetime
from telethon import functions, types

START_TIME = time.time()
AFK_STATE = {"afk": False, "reason": "", "time": None}

# .ping command
async def ping_handler(event, client):
    start = datetime.now()
    await event.edit("🏓 Pong!")
    end = datetime.now()
    ms = (end - start).microseconds / 1000
    await event.edit(f"🏓 **Pong!**\n⚡ **Speed:** `{ms:.2f} ms`")

# .alive command
async def alive_handler(event, client):
    uptime_sec = int(time.time() - START_TIME)
    h, rem = divmod(uptime_sec, 3600)
    m, s = divmod(rem, 60)
    await event.edit(
        f"🔥 **4st Userbot is Active & Running!**\n\n"
        f"⏱ **Uptime:** `{h}h {m}m {s}s`\n"
        f"👑 **Owner:** `{event.sender_id}`\n"
        f"⚙️ **Framework:** Telethon\n"
        f"✨ **Version:** 2.0"
    )

# .purge command (Reply to message to delete till latest)
async def purge_handler(event, client):
    if not event.reply_to_msg_id:
        return await event.edit("❌ Reply to a message to purge from.")
    reply_msg = await event.get_reply_message()
    from_id = reply_msg.id
    to_id = event.id
    msgs = list(range(from_id, to_id + 1))
    await client.delete_messages(event.chat_id, msgs)
    res = await event.respond(f"🧹 Purged `{len(msgs)}` messages!")
    await asyncio.sleep(3)
    await res.delete()

# .whois / .info command
async def whois_handler(event, client):
    target = None
    if event.reply_to_msg_id:
        reply = await event.get_reply_message()
        target = await client.get_entity(reply.sender_id)
    else:
        args = event.text.split(maxsplit=1)
        if len(args) > 1:
            target = await client.get_entity(args[1])
        else:
            target = await client.get_me()

    full = await client(functions.users.GetFullUserRequest(target.id))
    bio = full.full_user.about or "None"
    uname = f"@{target.username}" if target.username else "None"
    dc = target.photo.dc_id if target.photo else "Unknown"

    text = (
        f"👤 **User Information:**\n\n"
        f"• **First Name:** {target.first_name}\n"
        f"• **Last Name:** {target.last_name or ''}\n"
        f"• **Username:** {uname}\n"
        f"• **ID:** `{target.id}`\n"
        f"• **DC ID:** `{dc}`\n"
        f"• **Bio:** {bio}\n"
        f"• **Bot:** {target.bot}\n"
        f"• **Scam:** {target.scam}"
    )
    await event.edit(text)

