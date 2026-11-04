import asyncio
from unittest.mock import AsyncMock, MagicMock
from plugins.fake_typing import setup, _tasks


def _make_client():
    registered = []
    client = MagicMock()

    def fake_on(event):
        def dec(fn):
            registered.append(fn)
            return fn
        return dec

    client.on = fake_on
    client.get_input_entity = AsyncMock(return_value="peer_123")
    client.get_entity = AsyncMock(return_value=MagicMock(id=123))
    client.side_effect = AsyncMock()
    return client, registered


def _event(arg):
    ev = MagicMock()
    ev.pattern_match = MagicMock(group=lambda idx: arg)
    ev.edit = AsyncMock()
    return ev


def test_fake_typing_keeps_running_until_owner_stops():
    async def run():
        _tasks.clear()
        client, handlers = _make_client()
        setup(client)
        start, stop = handlers
        await start(_event("123"))
        await asyncio.sleep(0.05)
        assert 123 in _tasks and not _tasks[123].done()
        assert client.call_count >= 1  # typing request sent
        await stop(_event("123"))
        assert 123 not in _tasks

    asyncio.run(run())


def test_stoptyping_without_args_stops_all():
    async def run():
        _tasks.clear()
        client, handlers = _make_client()
        setup(client)
        start, stop = handlers
        await start(_event("123"))
        await stop(_event(None))
        assert _tasks == {}

    asyncio.run(run())
