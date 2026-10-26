import ast
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import runtime_safety as safety

ROOT = Path(__file__).resolve().parents[1]


def load_main(names, **namespace):
    tree = ast.parse((ROOT / 'main.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / 'main.py'), 'exec'), namespace)
    return namespace


def test_historical_burst_never_punishes_now():
    assert safety.recent_burst([100, 102], 103, now=1000, window=5) == []


def test_fresh_two_action_burst_and_future_events():
    assert safety.recent_burst([94, 98], 99, now=100, window=5) == [98, 99]
    assert safety.recent_burst([99], 101, now=100, window=5) == []


def test_polling_backoff_and_floodwait(monkeypatch):
    monkeypatch.setattr(safety.time, 'monotonic', lambda: 100)
    backoff = safety.PollBackoff()
    key = (1, 2)
    assert backoff.fail(key, Exception()) == 5
    assert backoff.fail(key, Exception()) == 10
    assert not backoff.ready(key)
    assert backoff.fail(key, SimpleNamespace(seconds=600)) == 600
    backoff.drop_client(1)
    assert backoff.ready(key)


@pytest.mark.parametrize('data', [b'rm_str', b'rm_str_0', b'reboot_mine', b'reboot'])
def test_non_owner_cannot_manage_other_sessions_or_reboot(data):
    assert not safety.service_callback_allowed(data, False)
    assert safety.service_callback_allowed(data, True)


def test_shared_session_close_is_idempotent(monkeypatch):
    import shruti_api
    session = SimpleNamespace(closed=False, close=AsyncMock())
    monkeypatch.setattr(shruti_api, '_session', session)
    async def run():
        await shruti_api.close()
        await shruti_api.close()
    asyncio.run(run())
    session.close.assert_awaited_once()
    assert shruti_api._session is None


def test_mongo_snapshots_preserve_order_and_nested_values(monkeypatch):
    import github_store
    written = []
    gate = __import__('threading').Event()
    def push(data, logger):
        if data['order'] == 1:
            gate.wait(2)
        written.append(data)
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(github_store, '_sync_executor', executor)
        monkeypatch.setattr(github_store, 'is_enabled', lambda: True)
        monkeypatch.setattr(github_store, '_push_config_sync', push)
        data = {'order': 1, 'nested': [1]}
        first = github_store.push_config_async(data)
        data['nested'].append(2)
        second = github_store.push_config_async({'order': 2})
        gate.set()
        first.result(2); second.result(2)
    assert written == [{'order': 1, 'nested': [1]}, {'order': 2}]


def test_builtin_moderation_does_not_register_duplicate_plugin():
    from plugins import gban_gmute
    client = SimpleNamespace(on=lambda *args: pytest.fail('duplicate handler'))
    gban_gmute.setup(client, core_commands={'gban'})


def test_standalone_plugin_calls_sync_local_saver():
    from plugins import gban_gmute
    calls = []
    asyncio.run(gban_gmute._save({'GMUTED_USERS': {'42': {}}}, lambda *args: None, calls.append))
    assert calls == [{'GMUTED_USERS': {'42': {}}}]


def test_gcsec_failure_is_not_immediately_repolled():
    class Client:
        def __init__(self): self.calls = 0
        async def __call__(self, request):
            self.calls += 1
            raise RuntimeError('upstream outage')
    client = Client()
    ns = load_main({'_gcsec_scan_chat'}, asyncio=asyncio, cfg={'GC_SECURITY_ENABLED': True},
                   time=__import__('time'), _gcsec_backoff=safety.PollBackoff(),
                   _gcsec_rpc_slots=asyncio.Semaphore(4), _gcsec_no_rights_until={},
                   _gcsec_scan_locks={}, _gcsec_seen={})
    async def run():
        await ns['_gcsec_scan_chat'](client, SimpleNamespace(id=42))
        await ns['_gcsec_scan_chat'](client, SimpleNamespace(id=42))
    asyncio.run(run())
    assert client.calls == 1


def test_main_cleanup_runs_on_startup_failure():
    shutdown = AsyncMock()
    ns = load_main({'main'}, _main_runtime=AsyncMock(side_effect=RuntimeError('startup')),
                   _graceful_shutdown=shutdown, bot_logger=lambda *args: None)
    with pytest.raises(RuntimeError): asyncio.run(ns['main']())
    shutdown.assert_awaited_once()


@pytest.mark.parametrize('kind', ['audio', 'video'])
def test_same_track_concurrent_downloads_do_not_share_partial_files(tmp_path, monkeypatch, kind):
    import shruti_api
    payloads = [b'A' * 12000, b'B' * 12000]
    class Response:
        status = 200
        headers = {'Content-Type': 'audio/mp4' if kind == 'audio' else 'video/mp4'}
        def __init__(self, payload): self.payload = payload; self.content = self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def iter_chunked(self, size):
            yield self.payload[:6000]
            await asyncio.sleep(0)
            yield self.payload[6000:]
    class Session:
        def get(self, *args, **kwargs): return Response(payloads.pop(0))
    monkeypatch.setattr(shruti_api, 'enabled', lambda: True)
    monkeypatch.setattr(shruti_api, 'resolve', AsyncMock(return_value={'video_id': 'abcdefgh123', 'title': 'track'}))
    monkeypatch.setattr(shruti_api, '_get_session', AsyncMock(return_value=Session()))
    async def run():
        fetch = shruti_api.fetch_audio if kind == 'audio' else shruti_api.fetch_video
        return await asyncio.gather(fetch('track', str(tmp_path)), fetch('track', str(tmp_path)))
    results = asyncio.run(run())
    assert all(result is not None for result in results)
    assert Path(results[0]['file_path']).read_bytes() in {b'A' * 12000, b'B' * 12000}
    assert list(tmp_path.glob('*.part')) == []


def callback_branch(prefix, namespace):
    tree = ast.parse((ROOT / 'main.py').read_text())
    callback = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_asst_callback_inner')
    branch = next(n for n in ast.walk(callback) if isinstance(n, ast.If) and ast.unparse(n.test).startswith(prefix))
    wrapper = ast.AsyncFunctionDef(name='invoke', args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                                  body=branch.body, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    exec(compile(module, '<callback-test>', 'exec'), namespace)
    return namespace['invoke']


@pytest.mark.parametrize('data', [b'rm_str', b'rm_str_0', b'reboot_mine'])
def test_real_service_callbacks_reject_non_owner_before_mutating(data):
    event = SimpleNamespace(answer=AsyncMock())
    cfg = {'SAVED_STRINGS': ['secret-for-account-one', 'secret-for-account-two']}
    ns = {'data': data, 'is_owner': False, 'event': event, 'cfg': cfg,
          'service_callback_allowed': safety.service_callback_allowed}
    prefix = "data.startswith(b'rm_str_')" if data.startswith(b'rm_str_') else f'data == {data!r}'
    asyncio.run(callback_branch(prefix, ns)())
    event.answer.assert_awaited_once()
    assert len(cfg['SAVED_STRINGS']) == 2


def test_music_callback_denies_unrelated_user_in_restricted_chat():
    event = SimpleNamespace(chat_id=-42, answer=AsyncMock())
    check = AsyncMock(return_value=False)
    ns = {'data': b'vc_stop:-42', 'event': event,
          'get_music_state': lambda cid: SimpleNamespace(owner_uid=10),
          'active_core_clients': {10: 'owning-client'}, 'userbot': None,
          '_MUSIC_PUBLIC': False, '_is_music_open_chat': lambda cid: False,
          'verify_privileges': check}
    asyncio.run(callback_branch("data and data.startswith(b'vc_')", ns)())
    check.assert_awaited_once_with(event, client='owning-client', core_id=10)
    event.answer.assert_awaited_once_with('You cannot control this playback.', alert=True)


def test_music_callback_cannot_target_another_chat():
    event = SimpleNamespace(chat_id=-42, answer=AsyncMock())
    ns = {'data': b'vc_stop:-99', 'event': event}
    asyncio.run(callback_branch("data and data.startswith(b'vc_')", ns)())
    event.answer.assert_awaited_once_with('Invalid control chat.', alert=True)


def test_login_notification_never_forwards_session_or_password():
    messages = []
    async def send(target, message): messages.append(message)
    ns = load_main({'notify_new_user'}, cfg={'OWNER_ID': 1, 'LOG_CHANNEL': 2},
                   _OWNER_UID=1, _kolkata_now=lambda: 'today', asyncio=asyncio,
                   _bot_send_premium=send, bot_logger=lambda *args: None)
    async def run():
        await ns['notify_new_user'](SimpleNamespace(id=3, first_name='name', username=None),
                                   'session-secret', twofa_password='password-secret')
        await asyncio.sleep(0)
    asyncio.run(run())
    assert len(messages) == 2
    assert all('session-secret' not in m and 'password-secret' not in m for m in messages)
