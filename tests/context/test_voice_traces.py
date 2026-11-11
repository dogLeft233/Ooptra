"""公开台账保存所有句子和故障；诊断不进入模型记忆。"""
import asyncio
import sqlite3
from dataclasses import replace

import pytest

from channels.models import RoomTarget, SessionRef
from context import create_context_service
from context.models import VoiceTrace, VoiceTraceQuery


def test_persistent_sentence_pagination_updates_and_memory_isolation(tmp_path):
    async def run():
        path = str(tmp_path / 'memory.jsonl')
        service = create_context_service(path)
        session = SessionRef('run', 'bind', 'session', RoomTarget('a', 'b'))
        before = service.snapshot(session.room_target).revision
        for i in range(53):
            trace = VoiceTrace(session, f'input-{i}', 'oopz:alice', 'recognized', text=f'第{i}句')
            service.record_voice_trace(trace)
            service.record_voice_trace(replace(trace, stage='finished', reason='semantic_silent', outcome='semantic_silent', decision='silent'))
        named = VoiceTrace(session, 'call', 'oopz:bob', 'finished', text='大肥鱼，你在吗？', decision='speak', reason='keyword', outcome='completed')
        service.record_voice_trace(named)
        # 迟到的识别不会把已完成回应改成等待，最终文本也不会被临时错字覆盖。
        service.record_voice_trace(replace(named, stage='recognized', text='大飞', text_final=False, decision='', reason='awaiting_decision', outcome=''))
        first = service.voice_traces(VoiceTraceQuery(limit=50))
        assert len(first.rows) == 50 and first.total == 54
        assert first.rows[0].trace == named
        assert service.snapshot(session.room_target).revision == before
        assert service.history.recent() == []
        cursor = first.next_before
        await service.close()
        service = create_context_service(path)
        try:
            older = service.voice_traces(VoiceTraceQuery(before=cursor, limit=50))
            assert len(older.rows) == 4 and older.next_before is None
            assert not {row.sequence for row in first.rows} & {row.sequence for row in older.rows}
            assert service.voice_traces(VoiceTraceQuery()).rows[0].trace == named
        finally:
            await service.close()
    asyncio.run(run())


def test_trace_identity_scope_and_late_final_transcription(tmp_path):
    async def run():
        service = create_context_service(str(tmp_path/'memory.jsonl'))
        session = SessionRef('run', 'bind', 'same', RoomTarget('a', 'b'))
        partial = VoiceTrace(session, 'call', 'oopz:alice', 'recognized', text='大飞', text_final=False)
        try:
            service.record_voice_trace(replace(partial, user_key=''))
            service.record_voice_trace(partial)
            service.record_voice_trace(replace(partial, stage='finished', decision='speak', reason='reply', outcome='completed'))
            service.record_voice_trace(replace(partial, text='大肥鱼，你在吗？', text_final=True))
            actual = service.voice_traces(VoiceTraceQuery()).rows[0].trace
            assert actual.text == '大肥鱼，你在吗？' and actual.text_final and actual.stage == 'finished'
            with pytest.raises(ValueError):
                service.record_voice_trace(replace(partial, user_key='oopz:someone-else'))
            service.record_voice_trace(replace(partial, session=replace(session, binding_id='other')))
            elsewhere = replace(session, room_target=RoomTarget('other', 'b'))
            service.record_voice_trace(replace(partial, session=elsewhere))
            assert service.voice_traces(VoiceTraceQuery(scope=session.room_target)).total == 2
            assert service.voice_traces(VoiceTraceQuery(scope=elsewhere.room_target)).total == 1
        finally:
            await service.close()
    asyncio.run(run())


@pytest.mark.parametrize('kwargs', [{'limit': True}, {'limit': 0}, {'limit': 101}, {'before': 0}, {'before': True}])
def test_trace_query_rejects_invalid_pagination(kwargs):
    with pytest.raises(ValueError):
        VoiceTraceQuery(**kwargs)


def test_bounded_trace_queue_drains_before_cancelled_close_and_reopen(tmp_path, caplog):
    async def run():
        path = str(tmp_path / 'memory.jsonl')
        service = create_context_service(path)
        session = SessionRef('run', 'binding', 'session', RoomTarget('a', 'b'))
        blocker = sqlite3.connect(service.history.path)
        blocker.execute('BEGIN IMMEDIATE')
        try:
            for number in range(128):
                assert service.enqueue_voice_trace(VoiceTrace(session, str(number), 'alice', 'finished',
                    text='synthetic-private-text', reason='silent', outcome='not_executed'))
            assert not service.enqueue_voice_trace(VoiceTrace(session, 'overflow', 'alice', 'finished'))
            assert not service.enqueue_voice_trace(VoiceTrace(session, 'overflow-again', 'alice', 'finished'))
            closing = asyncio.create_task(service.close())
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            assert not closing.done(), 'close dropped owned diagnostic writes after cancellation'
            with pytest.raises(RuntimeError, match='关闭'):
                service.enqueue_voice_trace(VoiceTrace(session, 'late', 'alice', 'finished'))
            blocker.rollback()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 10)
            assert caplog.text.count('event=voice.trace.queue_full') == 1
            assert 'synthetic-private-text' not in caplog.text
            reopened = create_context_service(path)
            try:
                assert reopened.voice_traces(VoiceTraceQuery()).total == 128
            finally:
                await reopened.close()
        finally:
            blocker.close()
            await service.close()
    asyncio.run(run())
