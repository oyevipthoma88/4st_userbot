"""Small, testable Telegram login, promotion and forwarding workflows."""
import asyncio
import time
import weakref
from html import escape
from telethon import errors, types, utils
from telethon.tl.functions.channels import InviteToChannelRequest, EditAdminRequest
from telethon.tl.functions.messages import AddChatUserRequest, EditChatAdminRequest


class AccountGate:
    """Serialize writes and remember Telegram's cooldown across all bulk jobs."""
    def __init__(self):
        self.lock = asyncio.Lock()
        self.until = 0.0

    async def call(self, action):
        async with self.lock:
            remaining = self.until - time.monotonic()
            if remaining > 0:
                raise RuntimeError(f"Telegram cooldown: retry after {int(remaining) + 1}s")
            try:
                return await action()
            except errors.FloodWaitError as exc:
                self.until = time.monotonic() + exc.seconds
                raise


_gates = weakref.WeakKeyDictionary()

def account_gate(client):
    gate = _gates.get(client)
    if gate is None:
        gate = AccountGate()
        _gates[client] = gate
    return gate


def normalize_phone(text):
    phone = re_phone(text)
    if not phone or not 7 <= len(phone) <= 15:
        raise ValueError("Send a valid phone number with country code.")
    return '+' + phone


def re_phone(text):
    import re
    raw = re.sub(r'[\s()+-]', '', text or '')
    return raw if raw.isascii() and raw.isdigit() else ''


def record_code(state, sent):
    code_hash = getattr(sent, 'phone_code_hash', None)
    if not code_hash:
        raise RuntimeError("Telegram did not issue a login code. Start login again in the official Telegram app.")
    state['phone_code_hash'] = code_hash
    timeout = max(0, int(getattr(sent, 'timeout', None) or 60))
    state['resend_at'] = time.monotonic() + timeout
    state['expires_at'] = time.monotonic() + 600
    delivery = getattr(sent, 'type', None)
    name = type(delivery).__name__
    if name == 'SentCodeTypeApp':
        hint = "Check the Telegram service chat in your already logged-in Telegram app (not SMS)."
    elif name in {'SentCodeTypeSms', 'SentCodeTypeFragmentSms'}:
        hint = "Check SMS on this phone number."
    elif name in {'SentCodeTypeCall', 'SentCodeTypeFlashCall', 'SentCodeTypeMissedCall'}:
        hint = "Telegram selected phone-call delivery; check incoming calls."
    elif name == 'SentCodeTypeEmailCode':
        hint = f"Check the email Telegram selected: {escape(getattr(delivery, 'email_pattern', '') or '')}."
    elif name == 'SentCodeTypeSetUpEmailRequired':
        raise RuntimeError("Telegram requires email setup. Complete it in the official Telegram app, then retry.")
    else:
        hint = "Check your official Telegram app. Telegram controls which delivery methods are available."
    state['delivery_hint'] = hint
    return f"<blockquote>📩 <b>Telegram accepted the code request.</b>\n{hint}\nEnter code with spaces: <code>1 2 3 4 5</code>\nResend available in {timeout}s; SMS cannot be forced.</blockquote>"


async def promote_in_chat(client, entity, target, rights, rank='Admin'):
    """Never confuse invite errors or an unverified command with promotion."""
    try:
        if isinstance(entity, types.Chat):
            return False, 'basic group cannot grant limited rights; upgrade to a supergroup'
        perms = await client.get_permissions(entity, 'me')
        creator = bool(getattr(perms, 'is_creator', False))
        if not creator and not getattr(perms, 'add_admins', False):
            return False, 'missing add-admin permission'
        try:
            target_perms = await client.get_permissions(entity, target)
            if getattr(target_perms, 'is_admin', False) or getattr(target_perms, 'is_creator', False):
                return True, 'already admin'
            member = True
        except errors.UserNotParticipantError:
            member = False
        if not member:
            if not creator and not getattr(perms, 'invite_users', False):
                return False, 'target is absent; missing invite permission'
            try:
                if isinstance(entity, types.Channel):
                    await account_gate(client).call(lambda: client(InviteToChannelRequest(entity, [target])))
                else:
                    await account_gate(client).call(lambda: client(AddChatUserRequest(entity.id, target, fwd_limit=0)))
            except errors.UserAlreadyParticipantError:
                pass
        # Delegate only rights this account holds, and only those valid here.
        values = rights.to_dict()
        values.pop('_', None)
        if isinstance(entity, types.Channel):
            group = bool(getattr(entity, 'megagroup', False))
            for key in list(values):
                if not creator:
                    values[key] = bool(values[key] and getattr(perms, key, False))
                if group and key in {'post_messages', 'edit_messages', 'post_stories', 'edit_stories', 'delete_stories'}:
                    values[key] = False
                if not group and key in {'ban_users', 'pin_messages', 'manage_topics'}:
                    values[key] = False
            delegated = types.ChatAdminRights(**values)
            try:
                await account_gate(client).call(lambda: client(EditAdminRequest(entity, target, delegated, rank=rank)))
            except errors.AdminRankInvalidError:
                await account_gate(client).call(lambda: client(EditAdminRequest(entity, target, delegated, rank='')))
        else:
            await account_gate(client).call(lambda: client(EditChatAdminRequest(entity.id, target, is_admin=True)))
        return True, 'promoted' if member else 'added + promoted'
    except errors.FloodWaitError as exc:
        return False, f'Telegram cooldown {exc.seconds}s; retry later'
    except Exception as exc:
        return False, f'{type(exc).__name__}: {str(exc)[:100]}'


async def bulk_promote(client, target_arg, rights, progress=None):
    target = await client.get_entity(target_arg)
    if not isinstance(target, types.User):
        raise ValueError('Choose a user or bot, not a group/channel.')
    results = []
    seen = set()
    async for dialog in client.iter_dialogs():
        if not dialog.is_group:
            continue
        cid = utils.get_peer_id(dialog.entity)
        if cid in seen:
            continue
        seen.add(cid)
        ok, note = await promote_in_chat(client, dialog.entity, target, rights)
        results.append((getattr(dialog.entity, 'title', str(cid)), ok, note))
        if progress and (len(results) == 1 or len(results) % 10 == 0):
            await progress(results)
    return results


async def broadcast_message(client, destination, message, *, forward=False):
    """Forward the message received by this client; do not dereference hidden origins."""
    if forward or getattr(message, 'fwd_from', None):
        return await account_gate(client).call(lambda: client.forward_messages(destination, message))
    return await account_gate(client).call(lambda: client.send_message(destination, message))


async def stage_message(sender, receiver, source, *, forward=False):
    """Wait for the receiver's own message ID; IDs differ between accounts."""
    sender_identity = await sender.get_me()
    receiver_identity = await receiver.get_me()
    sender_peer = await receiver.get_input_entity(sender_identity)
    receiver_peer = await sender.get_input_entity(receiver_identity)
    async with receiver.conversation(sender_peer, timeout=20) as conversation:
        await broadcast_message(sender, receiver_peer, source, forward=forward)
        return await conversation.get_response()


async def broadcast_many(destinations, message, *, forward=False):
    ok = fail = 0
    notes = []
    seen = set()
    for client, destination in destinations:
        key = (id(client), str(destination))
        if key in seen:
            continue
        seen.add(key)
        try:
            await broadcast_message(client, destination, message, forward=forward)
            ok += 1
        except Exception as exc:
            fail += 1
            if len(notes) < 5:
                notes.append(f'{type(exc).__name__}: {str(exc)[:80]}')
    return ok, fail, notes
