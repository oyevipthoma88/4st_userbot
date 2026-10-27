import os
from telethon import events, functions, types
from telethon.tl.functions.photos import UploadProfilePhotoRequest, DeletePhotosRequest
from telethon.tl.functions.account import UpdateProfileRequest

ORIGINAL_PROFILE = {}

async def get_user_data(event, client):
    if event.reply_to_msg_id:
        reply_msg = await event.get_reply_message()
        return await client.get_entity(reply_msg.sender_id)
    args = event.text.split(maxsplit=1)
    if len(args) > 1:
        return await client.get_entity(args[1])
    return None

def setup(client, **kwargs):
    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.clone(?:\s+(.+))?$"))
    async def clone_handler(event):
        target = await get_user_data(event, client)
        if not target:
            return await event.edit("❌ Reply to a user or provide username/ID to clone.")

        await event.edit("🔄 Cloning profile...")
        me = await client.get_me()

        if "first_name" not in ORIGINAL_PROFILE:
            full_me = await client(functions.users.GetFullUserRequest(me.id))
            ORIGINAL_PROFILE["first_name"] = me.first_name or ""
            ORIGINAL_PROFILE["last_name"] = me.last_name or ""
            ORIGINAL_PROFILE["about"] = full_me.full_user.about or ""

        full_target = await client(functions.users.GetFullUserRequest(target.id))
        t_first = target.first_name or ""
        t_last = target.last_name or ""
        t_bio = full_target.full_user.about or ""

        await client(UpdateProfileRequest(first_name=t_first, last_name=t_last, about=t_bio))

        photos = await client.get_profile_photos(target, limit=1)
        if photos:
            dl_photo = await client.download_media(photos[0])
            uploaded = await client.upload_file(dl_photo)
            await client(UploadProfilePhotoRequest(file=uploaded))
            if os.path.exists(dl_photo):
                os.remove(dl_photo)

        await event.edit(f"✅ Successfully cloned **{target.first_name}**!")

    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.(revert|unclone)$"))
    async def revert_handler(event):
        if not ORIGINAL_PROFILE:
            return await event.edit("❌ No backup found to revert.")

        await event.edit("🔄 Reverting to original profile...")
        await client(UpdateProfileRequest(
            first_name=ORIGINAL_PROFILE.get("first_name", ""),
            last_name=ORIGINAL_PROFILE.get("last_name", ""),
            about=ORIGINAL_PROFILE.get("about", "")
        ))

        my_photos = await client.get_profile_photos("me", limit=1)
        if my_photos:
            await client(DeletePhotosRequest(id=[types.InputPhoto(
                id=my_photos[0].id,
                access_hash=my_photos[0].access_hash,
                file_reference=my_photos[0].file_reference
            )]))

        ORIGINAL_PROFILE.clear()
        await event.edit("✅ Profile reverted to original successfully!")

