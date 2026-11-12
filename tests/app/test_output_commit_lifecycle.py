"""应用流程拥有已开始的提交，停止不能越过仍在运行的历史写入。"""
import asyncio
import threading

import pytest
from support.live_runtime import Socket, audio_message

from app.flows import run_expression
from app.live import LiveFlow
from behavior import create_behavior
from behavior.models import ReplySettings
from channels import create_channel
from channels.models import AudioPushResult, JoinRoom, PlaybackResult, RoomTarget
from context import create_context_service
from dialogue import create_expression_job, create_gemini_backend
from dialogue.models import LiveLimits
from operations.models import VoiceAgentSettings


class Transport:
    def __init__(self):
        self.draining = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
    async def join(self, target): return 'rtc'
    async def leave(self): pass
    async def retire_output(self): pass
    async def push_audio(self, pcm, rate, *, finish=False): return AudioPushResult(True)
    async def wait_playback(self, timeout):
        self.draining.set()
        await self.release.wait()
        return AudioPushResult(True)


class Model:
    def generate(self, intent, prepared, *, tools):
        async def produce(emit):
            await emit('text_final', text='已播放的回复')
            await emit('audio', pcm16=b'\1\0' * 10, sample_rate=24000)
        return create_expression_job(intent.action_id, intent.session, produce, deadline=intent.deadline)


def test_cancelled_expression_waits_for_accepted_sqlite_commit_before_returning(tmp_path):
    async def scenario():
        context = create_context_service(str(tmp_path / 'history.jsonl'))
        channel = create_channel(Transport())
        session = (await channel.execute(JoinRoom('join', RoomTarget('a', 'b'), 0))).session
        entered, release = threading.Event(), threading.Event()
        original = context.record_output
        def write(record):
            entered.set()
            assert release.wait(2)
            return original(record)
        context.record_output = write
        flow = asyncio.create_task(run_expression(backend=Model(), context=context, channel=channel,
            session=session, text='reply', current=lambda: True))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            flow.cancel()
            await asyncio.sleep(.01)
            assert not flow.done(), '取消等待不能遗留仍在写SQLite的线程'
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await flow
        assert [row['content'] for row in context.history.recent()] == ['已播放的回复']
        await context.close()
    asyncio.run(scenario())


def test_live_cancelled_playback_reports_channel_terminal_and_keeps_only_human_input(tmp_path):
    async def scenario():
        transport = Transport()
        transport.release.clear()
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom('join', RoomTarget('a', 'b'), 0))).session
        context = create_context_service(str(tmp_path / 'history.jsonl'))
        behavior = create_behavior()
        feedback = []
        original = behavior.feedback
        def observe(result):
            feedback.append(result)
            return original(result)
        behavior.feedback = observe
        socket = Socket()
        async def connect(*args, **kwargs): return socket
        backend = create_gemini_backend(VoiceAgentSettings(gemini_api_key='synthetic'), connector=connect,
            limits=LiveLimits(late_transcription_seconds=.1))
        flow = LiveFlow(backend=backend, context=context, behavior=behavior,
            channel=channel, session=session, current=lambda:True,
            settings=lambda:ReplySettings(probability=100), on_state=lambda *args:None,
            on_leave=lambda **kwargs:False, output_state=lambda:{'volume':50,'muted':False})
        await flow.start()
        await flow.send_input('human', b'\0\x40' * 320, 16000)
        await socket.deliver(audio_message(user_text='人的输入', text='尚未播完', done=True))
        await asyncio.wait_for(transport.draining.wait(), 1)
        await flow.close()
        actual = [result for result in feedback if isinstance(result, PlaybackResult)]
        assert len(actual) == 1 and actual[0].status in {'cancelled', 'partial'}
        assert [row['content'] for row in context.history.recent()] == ['人的输入']
        await context.close()
    asyncio.run(scenario())


def test_real_completed_output_is_committed_to_original_scope_after_session_becomes_stale(tmp_path):
    async def scenario():
        current = [True]
        class SwitchingTransport(Transport):
            async def wait_playback(self, timeout):
                current[0] = False
                return AudioPushResult(True)
        context = create_context_service(str(tmp_path / 'history.jsonl'))
        channel = create_channel(SwitchingTransport())
        session = (await channel.execute(JoinRoom('join', RoomTarget('a', 'b'), 0))).session
        outcome = await run_expression(backend=Model(), context=context, channel=channel,
            session=session, text='reply', current=lambda:current[0])
        assert outcome.outcome.status == 'completed'
        assert [row.content for row in context.snapshot(RoomTarget('a', 'b')).history] == ['已播放的回复']
        assert context.snapshot(RoomTarget('new', 'room')).history == ()
        await context.close()
    asyncio.run(scenario())


def test_live_close_does_not_hide_a_failed_transaction_commit(tmp_path):
    async def scenario():
        channel = create_channel(Transport())
        session = (await channel.execute(JoinRoom('join', RoomTarget('a', 'b'), 0))).session
        context = create_context_service(str(tmp_path / 'history.jsonl'))
        failed = threading.Event()
        def unavailable(record):
            failed.set()
            raise OSError('synthetic disk unavailable')
        context.record_output = unavailable
        socket = Socket()
        async def connect(*args, **kwargs): return socket
        backend = create_gemini_backend(VoiceAgentSettings(gemini_api_key='synthetic'), connector=connect,
            limits=LiveLimits(late_transcription_seconds=.1))
        flow = LiveFlow(backend=backend, context=context, behavior=create_behavior(),
            channel=channel, session=session, current=lambda:True,
            settings=lambda:ReplySettings(probability=100), on_state=lambda *args:None,
            on_leave=lambda **kwargs:False, output_state=lambda:{'volume':50,'muted':False})
        await flow.start()
        await socket.deliver(audio_message(text='已播放但磁盘失败', done=True))
        assert await asyncio.to_thread(failed.wait, 1)
        # Let the public flow consume the failure before requesting resource shutdown.
        while flow.busy:
            await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match='未确认'):
            await flow.close()
        await context.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["output", "silent_input"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_delivery_transaction_failure_keeps_prior_input_and_retry_is_scoped_once(tmp_path, monkeypatch, kind, cancelled):
    import sqlite3

    from support.context_fixtures import prepare_source

    from app.flows import commit_output, finish_input
    from channels.models import ActionOutcome
    from context.models import ConfirmedTurn, ContextRequest, Observation, OutputRecord
    async def scenario():
        context = create_context_service(str(tmp_path / "history.jsonl"))
        channel = create_channel(Transport())
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        await prepare_source(context.bridge, {"ok": True, "provider": "memory_companion",
            "memory_scopes": [{"umo": "original"}]}, "a", "b")
        context.observe(Observation("input", session, "wire", 1, "recognized", "original question", "speaker", "input"))
        prepared = await context.prepare_context(ContextRequest(session, "speaker", "input"))
        record = OutputRecord("output", session, ConfirmedTurn("original question", "answer", "completed", "speaker"),
                              ActionOutcome("output", "completed", session), prepared.context_ref, "input")
        entered, release = threading.Event(), threading.Event()
        connect = sqlite3.connect
        def failing_delivery(*args, **kwargs):
            db = connect(*args, **kwargs)
            def authorize(operation, table, *_):
                if operation == sqlite3.SQLITE_INSERT and table == "deliveries":
                    entered.set()
                    assert release.wait(3)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            return db
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", failing_delivery)
            pending = asyncio.create_task(commit_output(context, record) if kind == "output" else finish_input(context, "input"))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                if cancelled:
                    pending.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError if cancelled else sqlite3.DatabaseError):
                    await pending
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
        assert context.history.as_messages() == [{"role": "user", "content": "original question"}]
        assert await context.bridge.poll(wait=0) == []
        context.finish_input("input")
        context.finish_input("input")
        await asyncio.sleep(0)
        events = await context.bridge.poll(wait=0)
        assert len(events) == 1
        assert events[0]["scope"]["memory_scopes"] == [{"umo": "original"}]
        assert events[0]["payload"]["user_text"] == "original question"
        await context.close()
    asyncio.run(scenario())
