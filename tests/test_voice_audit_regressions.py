import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from support.channel_fixtures import establish_room
from test_voice_session_regressions import make_agent

from oopz_sdk.services.voice import Voice
from voice_agent.duplex import VoiceDuplex
from voice_agent.ws_transport import open_live_ws


def test_remote_pcm_wakes_owner_loop_from_real_thread():
    async def run():
        transport = SimpleNamespace(start=AsyncMock(), close=AsyncMock(),
                                    set_remote_pcm_callback=lambda cb: None)
        duplex = VoiceDuplex(transport)
        received = asyncio.Event()
        duplex.set_remote_pcm_handler(lambda *args: received.set())
        await duplex.start()
        await asyncio.sleep(0)
        errors = []
        def deliver():
            try:
                duplex.enqueue_remote_pcm('human', b'pcm', 16000)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=deliver)
        thread.start()
        thread.join()
        try:
            assert not errors
            await asyncio.wait_for(received.wait(), 1)
        finally:
            await duplex.close()
        duplex.enqueue_remote_pcm('late', b'pcm', 16000)
        assert duplex._pcm_queue.empty()
    asyncio.run(run(), debug=True)


@pytest.mark.parametrize('stage', ['join', 'identity'])
@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_sdk_cancelled_join_rolls_back_and_retains_retry_target(stage, cleanup_fails):
    async def run():
        reached = asyncio.Event()
        async def blocked(*args, **kwargs):
            reached.set()
            await asyncio.Event().wait()
        membership = AsyncMock(side_effect=RuntimeError('retry') if cleanup_fails else None)
        sign = SimpleNamespace(rtc_token='token', rtc_channel_name='room')
        owner = SimpleNamespace(channels=SimpleNamespace(
            enter_channel=AsyncMock(return_value=sign), leave_voice_channel=membership))
        voice = Voice(owner, SimpleNamespace(person_uid='self', agora_app_id='app'), None, None, None)
        voice.backend = SimpleNamespace(join=blocked if stage == 'join' else AsyncMock(return_value=True),
                                        send_identity=blocked, leave=AsyncMock())
        task = asyncio.create_task(voice.join(area='area', channel='channel', rtc_uid='123'))
        await reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert membership.await_count == 1
        assert voice._current_area == ('area' if cleanup_fails else None)
        if cleanup_fails:
            membership.side_effect = None
            await voice.leave()
            assert voice._current_area is None
    asyncio.run(run())


def test_proxy_connection_cancel_closes_unowned_session(monkeypatch):
    import aiohttp
    async def run():
        session = SimpleNamespace(ws_connect=AsyncMock(side_effect=asyncio.CancelledError), close=AsyncMock())
        monkeypatch.setattr(aiohttp, 'ClientSession', lambda **kw: session)
        with pytest.raises(asyncio.CancelledError):
            await open_live_ws('wss://example.test', proxy='http://localhost:7890')
        session.close.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('backend', ['mimo_cascade', 'gemini_live'])
def test_speak_before_join_has_no_model_request_or_memory(tmp_path, monkeypatch, backend):
    async def run():
        agent = make_agent(tmp_path, backend=backend)
        call = AsyncMock(return_value=(b'pcm', 16000))
        monkeypatch.setattr(agent.backend, 'speak_text' if agent.live_mode else 'tts', call)
        result = await agent.speak_text('hello')
        assert not result['ok']
        call.assert_not_awaited()
        assert not agent.memory.recent()
        await agent.backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('backend', ['mimo_cascade', 'gemini_live'])
@pytest.mark.parametrize('state', ['suspended', 'disconnected', 'no_duplex'])
def test_speak_without_playback_has_no_request(tmp_path, monkeypatch, backend, state):
    async def run():
        agent = make_agent(tmp_path, backend=backend)
        await establish_room(agent)
        agent._connection_ready = state != 'disconnected'
        agent._voice_suspended = state == 'suspended'
        if state == 'no_duplex':
            agent.duplex = None
        call = AsyncMock(return_value=(b'pcm', 16000))
        monkeypatch.setattr(agent.backend, 'speak_text' if agent.live_mode else 'tts', call)
        assert not (await agent.speak_text('hello'))['ok']
        call.assert_not_awaited()
        assert not agent.memory.recent()
        await agent.backend.aclose()
    asyncio.run(run())


def test_successful_proxy_session_stays_open_until_owner_closes(monkeypatch):
    import aiohttp
    async def run():
        ws = SimpleNamespace(close=AsyncMock())
        session = SimpleNamespace(ws_connect=AsyncMock(return_value=ws), close=AsyncMock())
        monkeypatch.setattr(aiohttp, 'ClientSession', lambda **kw: session)
        owner = await open_live_ws('wss://example.test', proxy='http://localhost:7890')
        session.close.assert_not_awaited()
        await owner.close()
        ws.close.assert_awaited_once()
        session.close.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('replace_owner', [False, True])
def test_repeated_cancellation_waits_for_rollback_and_preserves_new_owner(replace_owner):
    async def run():
        joining = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        async def join(**kwargs):
            joining.set()
            await asyncio.Event().wait()
        async def leave():
            cleaning.set()
            await release.wait()
        membership = AsyncMock()
        sign = SimpleNamespace(rtc_token='token', rtc_channel_name='room')
        owner = SimpleNamespace(channels=SimpleNamespace(
            enter_channel=AsyncMock(return_value=sign), leave_voice_channel=membership))
        voice = Voice(owner, SimpleNamespace(person_uid='self', agora_app_id='app'), None, None, None)
        voice.backend = SimpleNamespace(join=join, leave=leave)
        task = asyncio.create_task(voice.join(area='old', channel='old', rtc_uid='123'))
        await joining.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        prematurely_done = task.done()
        if replace_owner:
            voice._join_generation = getattr(voice, '_join_generation', 0) + 1
            voice._current_area = voice._current_channel = 'new'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not prematurely_done
        membership.assert_awaited_once()
        assert voice._current_area == ('new' if replace_owner else None)
    asyncio.run(run())
