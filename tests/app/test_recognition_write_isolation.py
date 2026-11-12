"""完整识别落盘等待不得阻止音频事件循环。"""
import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from support.channel_fixtures import make_voice_agent
from support.live_runtime import audio_message, make_live_agent, wait_until

from app.recognition import PreparedRecognition, RecognitionInputs
from behavior import create_behavior
from channels.models import AudioInputPolicy, IncomingAudio, RoomTarget, SessionRef, UtteranceResult
from context import create_context_service
from context.models import ContextRequest
from dialogue.models import Recognition


@pytest.mark.parametrize('operation', ['recognition', 'snapshot', 'prepare'])
def test_locked_recognition_database_does_not_block_the_event_loop(tmp_path, operation):
    async def run():
        context = create_context_service(str(tmp_path / 'memory.jsonl'))
        session = SessionRef('run', 'bind', 'session', RoomTarget('a', 'b'))
        heartbeat = threading.Event()
        ran_before_unlock = []
        blocker = sqlite3.connect(context.history.path, check_same_thread=False)
        blocker.execute('BEGIN IMMEDIATE')

        def release():
            ran_before_unlock.append(heartbeat.wait(1))
            blocker.rollback()
            blocker.close()

        thread = threading.Thread(target=release)
        thread.start()
        result = asyncio.get_running_loop().create_future()
        result.set_result(Recognition('input', session, 'attempt', 'alice', 'recognized', '现场输入'))
        traces = []
        host = SimpleNamespace(context_service=context,
            observations=SimpleNamespace(trace=lambda *args, **kwargs: traces.append(kwargs)))
        inputs = RecognitionInputs(host)
        item = PreparedRecognition(
            SimpleNamespace(trigger_id='input', session=session, user_key='alice'),
            SimpleNamespace(attempt_number=1), SimpleNamespace(result=lambda: result), 16000)
        try:
            asyncio.get_running_loop().call_soon(heartbeat.set)
            if operation == 'recognition':
                await inputs._watch(item)
            elif operation == 'snapshot':
                snapshot = await context.snapshot_async(session.room_target)
                assert snapshot.history == ()
            else:
                prepared = await context.prepare_context(ContextRequest(session))
                assert prepared.history == ()
            await asyncio.to_thread(thread.join)
            assert ran_before_unlock == [True], 'SQLite wait blocked the ready loop callback'
            if operation == 'recognition':
                assert [row['content'] for row in context.history.recent()] == ['现场输入']
                assert traces[-1]['recognition_status'] == 'recognized'
        finally:
            heartbeat.set()
            await asyncio.to_thread(thread.join)
            await context.close()
    asyncio.run(run())


def test_input_gap_during_snapshot_cannot_resurrect_input_or_erase_new_prefix(tmp_path, monkeypatch):
    async def run():
        context = create_context_service(str(tmp_path / 'memory.jsonl'))
        session = SessionRef('run', 'bind', 's', RoomTarget('a', 'room'))
        entered, resume = asyncio.Event(), asyncio.Event()
        original = context.snapshot_async
        calls = 0

        async def snapshot(scope):
            nonlocal calls
            result = await original(scope)
            calls += 1
            if calls == 1:
                entered.set()
                await resume.wait()
            return result

        monkeypatch.setattr(context, 'snapshot_async', snapshot)
        opened, pcm = [], bytearray()
        pending = asyncio.get_running_loop().create_future()

        async def close_stream():
            pending.cancel()

        def open_recognition(input, permit):
            opened.append(input)
            return SimpleNamespace(feed=pcm.extend, result=lambda: pending, aclose=close_stream)

        behavior = create_behavior()
        behavior.start_session(session)
        host = SimpleNamespace(context_service=context, behavior=behavior,
            channels=SimpleNamespace(snapshot=lambda: SimpleNamespace(session=session)),
            observations=SimpleNamespace(trace=lambda *args, **kwargs: None),
            backend=SimpleNamespace(open_recognition=open_recognition),
            settings=SimpleNamespace(cascade_turn_limit=0), _reply_cooldown=lambda: 0,
            _input_policy=lambda: AudioInputPolicy(min_utterance_ms=100))
        inputs = RecognitionInputs(host)

        async def feed(data, sequence):
            return await inputs.feed(IncomingAudio('alice', data, 16000, sequence),
                                     UtteranceResult('accumulating', stream_pcm16=data))

        old = asyncio.create_task(feed(b'\0\x40' * 1600, 1))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await inputs.reset()
            await feed(b'\0\x20' * 800, 2)
            resume.set()
            assert await old == ''
            assert opened == []
            await feed(b'\0\x20' * 800, 3)
            assert len(opened) == 1
            assert pcm == b'\0\x20' * 1600
        finally:
            resume.set()
            await asyncio.gather(old, return_exceptions=True)
            await inputs.reset()
            await context.close()
    asyncio.run(run())


def test_selected_observation_failure_records_one_stack_and_flow_terminal(tmp_path, monkeypatch, caplog):
    async def run():
        agent = make_voice_agent(tmp_path)
        await agent.join('a', 'room')

        async def recognize(input, permit):
            return Recognition(input.input_ref, input.session, permit.attempt_id,
                               input.user_key, 'recognized', 'private transcript')

        monkeypatch.setattr(agent.backend, 'recognize', recognize)
        with sqlite3.connect(agent.context_service.history.path) as db:
            db.execute("CREATE TRIGGER fail_input BEFORE INSERT ON history BEGIN SELECT RAISE(ABORT,'synthetic disk failure'); END")
        try:
            await agent._handle_utterance_reply('alice', b'\0\x40' * 16000, 16000)
            failure = [row for row in caplog.records if row.exc_info]
            assert len(failure) == 1
            assert 'context.observation.failed' in failure[0].message
            terminal = [row for row in caplog.records if 'voice.flow.failed' in row.message]
            assert len(terminal) == 1 and not terminal[0].exc_info
            assert agent.observations.snapshot()['recent_decisions'][-1]['outcome'] == 'failed'
            assert 'private transcript' not in caplog.text
        finally:
            await agent.close()
    asyncio.run(run())


def test_live_observation_failure_records_one_stack_and_flow_terminal(tmp_path, caplog):
    async def run():
        agent, _, sockets = await make_live_agent(tmp_path, reply_probability_percent=0,
                                                 force_reply_keywords=[])
        with sqlite3.connect(agent.context_service.history.path) as db:
            db.execute("CREATE TRIGGER fail_input BEFORE INSERT ON history BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        try:
            await sockets[0].deliver(audio_message(user_text='private transcript', done=True))
            await wait_until(lambda: any('voice.live.flow_failed' in row.message for row in caplog.records))
            stacks = [row for row in caplog.records if row.exc_info]
            assert len(stacks) == 1 and 'context.observation.failed' in stacks[0].message
            terminal = [row for row in caplog.records if 'voice.live.flow_failed' in row.message]
            assert len(terminal) == 1 and not terminal[0].exc_info
            assert 'private transcript' not in caplog.text
        finally:
            await agent.leave()
            assert not agent.channels.snapshot().confirmed
            await agent.context_service.close()
    asyncio.run(run())


def test_failed_input_transaction_keeps_recognition_and_releases_stream(tmp_path, caplog):
    async def run():
        context = create_context_service(str(tmp_path / 'memory.jsonl'))
        session = SessionRef('run', 'bind', 's', RoomTarget('a', 'room'))
        with sqlite3.connect(context.history.path) as db:
            db.execute("CREATE TRIGGER fail_input BEFORE INSERT ON history BEGIN SELECT RAISE(ABORT,'synthetic disk failure'); END")
        result = asyncio.get_running_loop().create_future()
        result.set_result(Recognition('input', session, 'attempt', 'alice', 'recognized', 'private transcript'))
        released = asyncio.Event()

        async def close_stream():
            released.set()

        traces, attempts = [], []
        host = SimpleNamespace(context_service=context,
            observations=SimpleNamespace(trace=lambda *args, **kwargs: traces.append(kwargs)),
            behavior=SimpleNamespace(finish_attempt=attempts.append))
        inputs = RecognitionInputs(host)
        item = PreparedRecognition(
            SimpleNamespace(trigger_id='input', session=session, user_key='alice'),
            SimpleNamespace(attempt_number=1),
            SimpleNamespace(result=lambda: result, aclose=close_stream), 16000)
        try:
            await inputs._watch(item)
            await inputs.release(item)
            assert released.is_set() and len(attempts) == 1
            assert traces[0]['recognition_status'] == 'recognized'
            assert all(row.get('recognition_status') != 'failed' for row in traces)
            assert context.history.recent() == []
            errors = [row for row in caplog.records if 'context.observation.failed' in row.message]
            assert len(errors) == 1
            assert 'session_id=s input_ref=input' in errors[0].message
            assert 'private transcript' not in caplog.text
        finally:
            await context.close()
    asyncio.run(run())


@pytest.mark.parametrize('cancel', [False, True])
def test_join_observation_failure_always_releases_joined_room(tmp_path, monkeypatch, cancel):
    async def run():
        agent = make_voice_agent(tmp_path)

        async def fail(observation):
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError('synthetic observation failure')

        monkeypatch.setattr(agent.context_service, 'observe_async', fail)
        try:
            with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
                await agent.join('a', 'room')
            assert not agent.channels.snapshot().confirmed
            assert agent.test_platform.voice.leaves == 1
        finally:
            await agent.close()
    asyncio.run(run())
