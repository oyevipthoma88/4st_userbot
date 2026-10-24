import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from telethon import errors, types
from telethon.tl.functions.channels import InviteToChannelRequest, EditAdminRequest
from telethon.tl.functions.messages import AddChatUserRequest, EditChatAdminRequest
from bot_workflows import (normalize_phone, record_code, promote_in_chat,
                           broadcast_many, bulk_promote, AccountGate, stage_message)


def channel():
    return types.Channel(id=100, title='Group', photo=types.ChatPhotoEmpty(),
                         date=None, megagroup=True, access_hash=123)


def target():
    return types.User(id=42, access_hash=456, first_name='Target')


class Client:
    def __init__(self, *, member=False, invite_error=None, creator=True):
        self.member = member
        self.invite_error = invite_error
        self.creator = creator
        self.calls = []

    async def get_permissions(self, entity, user):
        if user == 'me':
            return SimpleNamespace(is_creator=self.creator, add_admins=self.creator,
                                   invite_users=self.creator)
        if not self.member:
            raise errors.UserNotParticipantError(None)
        return SimpleNamespace(is_admin=False, is_creator=False)

    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, (InviteToChannelRequest, AddChatUserRequest)):
            if self.invite_error:
                raise self.invite_error
            self.member = True


def run(coro):
    return asyncio.run(coro)


def test_phone_country_code_and_format():
    assert normalize_phone('+91 98765-43210') == '+919876543210'
    with pytest.raises(ValueError):
        normalize_phone('not-a-phone')


def test_telegram_app_code_is_not_misreported_as_sms():
    state = {}
    record_code(state, SimpleNamespace(phone_code_hash='fresh', timeout=30,
                                      type=types.auth.SentCodeTypeApp(length=5)))
    assert state['phone_code_hash'] == 'fresh'
    assert 'not SMS' in state['delivery_hint']
    assert state['resend_at'] > 0


def test_resend_uses_latest_hash_and_failed_response_keeps_previous_hash():
    state = {'phone_code_hash': 'old'}
    record_code(state, SimpleNamespace(phone_code_hash='new', timeout=60,
                                      type=types.auth.SentCodeTypeSms(length=5)))
    assert state['phone_code_hash'] == 'new'
    with pytest.raises(RuntimeError):
        record_code(state, SimpleNamespace())
    assert state['phone_code_hash'] == 'new'


def test_target_added_before_promoting_in_supergroup():
    client = Client()
    ok, note = run(promote_in_chat(client, channel(), target(), types.ChatAdminRights(invite_users=True)))
    assert ok and note == 'added + promoted'
    assert [type(x) for x in client.calls] == [InviteToChannelRequest, EditAdminRequest]
    assert client.calls[0].channel.id == 100
    assert client.calls[0].users[0].id == 42


def test_basic_group_does_not_silently_grant_full_admin_for_limited_rights():
    client = Client()
    entity = types.Chat(id=77, title='Basic', photo=types.ChatPhotoEmpty(),
                        participants_count=1, date=None, version=1)
    ok, note = run(promote_in_chat(client, entity, target(), types.ChatAdminRights(invite_users=True)))
    assert ok is False
    assert 'upgrade to a supergroup' in note
    assert client.calls == []


def test_privacy_rejection_does_not_promote_or_claim_success():
    client = Client(invite_error=errors.UserPrivacyRestrictedError(None))
    ok, note = run(promote_in_chat(client, channel(), target(), types.ChatAdminRights(invite_users=True)))
    assert ok is False
    assert 'UserPrivacyRestrictedError' in note
    assert len(client.calls) == 1


def test_already_member_skips_invite_and_still_promotes():
    client = Client(member=True)
    ok, _ = run(promote_in_chat(client, channel(), target(), types.ChatAdminRights(invite_users=True)))
    assert ok
    assert [type(x) for x in client.calls] == [EditAdminRequest]


def test_no_add_admin_rights_never_invites_or_promotes():
    client = Client(creator=False)
    ok, _ = run(promote_in_chat(client, channel(), target(), types.ChatAdminRights(invite_users=True)))
    assert ok is False
    assert client.calls == []


def test_forward_broadcast_preserves_received_message_and_deduplicates_destinations():
    client = SimpleClient()
    msg = SimpleNamespace(id=50, fwd_from=object(), media=object())
    ok, fail, _ = run(broadcast_many([(client, -100123), (client, -100123), (client, 42)], msg))
    assert (ok, fail) == (2, 0)
    assert client.forward_messages.await_args_list[0].args == (-100123, msg)
    assert client.forward_messages.await_args_list[1].args == (42, msg)
    client.send_message.assert_not_awaited()


class SimpleClient:
    def __init__(self):
        self.forward_messages = AsyncMock()
        self.send_message = AsyncMock()


def test_protected_forward_failure_is_reported_without_copy_fallback():
    client = SimpleClient()
    client.forward_messages.side_effect = errors.ChatForwardsRestrictedError(None)
    ok, fail, notes = run(broadcast_many([(client, -100123)], SimpleNamespace(id=50), forward=True))
    assert (ok, fail) == (0, 1)
    assert 'ChatForwardsRestrictedError' in notes[0]
    client.send_message.assert_not_awaited()


def test_composed_media_is_sent_as_message_not_stripped_text():
    client = SimpleClient()
    msg = SimpleNamespace(id=50, media=object(), fwd_from=None)
    ok, fail, _ = run(broadcast_many([(client, 42)], msg))
    assert (ok, fail) == (1, 0)
    client.send_message.assert_awaited_once_with(42, msg)


def test_flood_wait_blocks_subsequent_writes_without_long_sleep():
    async def check():
        gate = AccountGate()
        action = AsyncMock(side_effect=errors.FloodWaitError(None, capture=120))
        with pytest.raises(errors.FloodWaitError):
            await gate.call(action)
        next_action = AsyncMock()
        with pytest.raises(RuntimeError, match='cooldown'):
            await gate.call(next_action)
        next_action.assert_not_awaited()
    run(check())


def test_bulk_promote_targets_all_own_groups_not_broadcast_channels():
    class BulkClient(Client):
        async def get_entity(self, arg):
            assert arg == '@target'
            return target()

        async def iter_dialogs(self):
            for n in range(2):
                ent = channel()
                ent.id += n
                yield SimpleNamespace(is_group=True, entity=ent)
            yield SimpleNamespace(is_group=False, entity=channel())
    client = BulkClient(member=True)
    results = run(bulk_promote(client, '@target', types.ChatAdminRights(invite_users=True)))
    assert len(results) == 2
    assert all(ok for _, ok, _ in results)
    assert [call.channel.id for call in client.calls] == [100, 101]


def load_function(name, namespace):
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'main.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'main.py', 'exec'), namespace)
    return namespace[name]


def test_cancel_closes_both_pending_login_clients_and_clears_state():
    import contextlib
    client = SimpleNamespace(disconnect=AsyncMock())
    pyro = SimpleNamespace(disconnect=AsyncMock())
    state = SimpleNamespace(auth_clients={42: client}, pyro_auth_clients={42: pyro},
                            asst_conversation_state={42: {'step': 'waiting_otp'}})
    cancel = load_function('_cancel_pending_login', {'state': state, 'asyncio': asyncio, 'contextlib': contextlib})
    run(cancel(42))
    client.disconnect.assert_awaited_once()
    pyro.disconnect.assert_awaited_once()
    assert state.auth_clients == {} and state.pyro_auth_clients == {}
    assert state.asst_conversation_state[42] is None


def test_cross_account_forward_uses_receivers_message_id():
    from unittest.mock import MagicMock
    sender = SimpleClient()
    receiver = SimpleClient()
    sender.get_me = AsyncMock(return_value=SimpleNamespace(id=42))
    receiver.get_me = AsyncMock(return_value=SimpleNamespace(id=50))
    sender.get_input_entity = AsyncMock(return_value='receiver-peer')
    receiver.get_input_entity = AsyncMock(return_value='sender-peer')
    received = SimpleNamespace(id=901, fwd_from=object())
    conversation = SimpleNamespace(get_response=AsyncMock(return_value=received))
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=conversation)
    manager.__aexit__ = AsyncMock(return_value=False)
    receiver.conversation = MagicMock(return_value=manager)
    source = SimpleNamespace(id=101, fwd_from=object())
    result = run(stage_message(sender, receiver, source, forward=True))
    assert result.id == 901
    sender.forward_messages.assert_awaited_once_with('receiver-peer', source)
    receiver.conversation.assert_called_once_with('sender-peer', timeout=20)


def test_onboarding_promotes_configured_bot_on_new_account_not_new_user_on_owner():
    new_client = object()
    bot = SimpleNamespace(username='ConfiguredBot')
    promote = AsyncMock()
    autojoin = AsyncMock()
    onboard = load_function('_onboard_logged_in_core', {
        'asstbot': SimpleNamespace(get_me=AsyncMock(return_value=bot)),
        '_run_bulk_promotion': promote, 'auto_join_and_start': autojoin,
        'bot_logger': lambda *args: None,
    })
    run(onboard(new_client))
    promote.assert_awaited_once_with(new_client, '@ConfiguredBot')
    autojoin.assert_awaited_once_with(new_client)


def test_resend_refreshes_expired_login_deadline(monkeypatch):
    import bot_workflows
    state = {'expires_at': 100, 'phone_code_hash': 'old'}
    monkeypatch.setattr(bot_workflows.time, 'monotonic', lambda: 1000)
    record_code(state, SimpleNamespace(phone_code_hash='new', timeout=30,
                                      type=types.auth.SentCodeTypeApp(length=5)))
    assert state['expires_at'] == 1600
    assert state['resend_at'] == 1030
    assert state['phone_code_hash'] == 'new'


def test_unverified_management_command_cannot_count_as_promoted():
    worker = load_function('_grow_cmd_promote', {})
    client = SimpleClient()
    assert run(worker({'client': client, 'entity': channel()}, '@target')) is False
    client.send_message.assert_not_awaited()


def test_idle_login_cleanup_disconnects_expired_client():
    import contextlib
    client = SimpleNamespace(disconnect=AsyncMock())
    state = SimpleNamespace(auth_clients={42: client}, pyro_auth_clients={},
                            asst_conversation_state={42: {'expires_at': 0}})
    namespace = {'state': state, 'asyncio': asyncio, 'contextlib': contextlib,
                 'time': SimpleNamespace(monotonic=lambda: 1),
                 '_login_lock': lambda uid: asyncio.Lock()}
    namespace['_cancel_pending_login'] = load_function('_cancel_pending_login', namespace)
    expire = load_function('_expire_pending_login', namespace)
    run(expire(42, 0))
    assert state.auth_clients == {}
    client.disconnect.assert_awaited_once()
