"""诊断库锁等待不得阻止音频所在事件循环继续调度。"""
import asyncio
import sqlite3
import threading

from app.observation import FlowObservations
from channels.models import RoomTarget, SessionRef
from context import create_context_service
from context.models import VoiceTraceQuery


def test_locked_diagnostic_database_does_not_block_the_event_loop(tmp_path):
    async def run():
        context = create_context_service(str(tmp_path / 'memory.jsonl'))
        session = SessionRef('run', 'bind', 'session', RoomTarget('a', 'b'))
        observations = FlowObservations(journal=context)
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
        try:
            asyncio.get_running_loop().call_soon(heartbeat.set)
            observations.trace(session, 'input', 'alice', 'finished', text='已识别输入',
                               reason='semantic_silent', outcome='not_executed')
            await asyncio.sleep(0)
            await asyncio.to_thread(thread.join)
            assert ran_before_unlock == [True], 'SQLite waiting blocked the ready event-loop callback'
            await context.flush_voice_traces()
            rows = context.voice_traces(VoiceTraceQuery()).rows
            assert len(rows) == 1 and rows[0].trace.input_ref == 'input'
            assert rows[0].trace.session == session
        finally:
            heartbeat.set()
            await asyncio.to_thread(thread.join)
            await context.close()
    asyncio.run(run())
