import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from telethon import types, errors
from telethon.tl.functions.channels import InviteToChannelRequest, EditAdminRequest
from bot_workflows import add_to_chat


def _channel():
    return types.Channel(id=100, title='G', photo=types.ChatPhotoEmpty(), date=None,
                         access_hash=1, megagroup=True)


class Client:
    def __init__(self):
        self.calls = []
    async def get_permissions(self, entity, user):
        raise errors.UserNotParticipantError(None)
    async def __call__(self, req):
        self.calls.append(req)


def test_invite_only_admin_just_adds_user_and_never_promotes():
    c = Client()
    target = types.InputUser(42, 0)
    ok, note = asyncio.run(add_to_chat(c, _channel(), target))
    assert ok and 'added' in note
    assert [type(x) for x in c.calls] == [InviteToChannelRequest]
