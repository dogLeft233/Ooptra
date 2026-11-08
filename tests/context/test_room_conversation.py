"""通过公开上下文端口验证多人房间历史与个人授权边界。"""
import asyncio

from support.context_fixtures import prepare_source

from channels.models import ActionOutcome, RoomTarget, SessionRef
from context import create_context_service
from context.models import ConfirmedTurn, ContextRequest, Observation, OutputRecord


def test_room_history_keeps_other_speakers_and_excludes_only_current_input(tmp_path):
    async def run():
        service = create_context_service(str(tmp_path / 'history.jsonl'), max_turns=4)
        session = SessionRef('run', 'binding', 's', RoomTarget('a', 'c'))
        other = SessionRef('run', 'binding', 'other', RoomTarget('a', 'private'))
        try:
            service.observe(Observation('private', other, 'conn', 1, 'recognized', '私密内容', 'alice', 'private'))
            for number, (speaker, text) in enumerate([
                ('alice', '题目是求积分'), ('bob', '上下限是多少'),
                ('alice', '从零到一'), ('bob', '那怎么做'),
            ], 1):
                service.observe(Observation(str(number), session, 'conn', number, 'recognized', text, speaker, str(number)))
            prepared = await service.prepare_context(ContextRequest(session, 'bob', '4'))
            assert [row.content for row in prepared.history] == ['题目是求积分', '上下限是多少', '从零到一']
            assert [row.user_key for row in prepared.history] == ['alice', 'bob', 'alice']
            assert all(row.ts > 0 for row in prepared.history)
            assert not prepared.authorized_write_grants
            assert [row['content'] for row in service.history.recent(user_key='bob')] == ['上下限是多少', '那怎么做']
            await service.close()
            service = create_context_service(str(tmp_path / 'history.jsonl'))
            restored = await service.prepare_context(ContextRequest(session, 'bob', '4'))
            assert restored.history == prepared.history
        finally:
            await service.close()
    asyncio.run(run())


def test_late_asr_is_retained_once_without_rewinding_room_projection(tmp_path):
    async def run():
        service = create_context_service(str(tmp_path / 'history.jsonl'))
        session = SessionRef('run', 'binding', 's', RoomTarget('a', 'c'))
        late = Observation('slow', session, 'asr', 1, 'recognized', '慢识别的甲', 'alice', 'slow')
        try:
            service.observe(Observation('fast', session, 'asr', 2, 'recognized', '快识别的乙', 'bob', 'fast'))
            service.observe(late)
            service.observe(late)
            assert service.snapshot(session.room_target).latest_input == '快识别的乙'
            prepared = await service.prepare_context(ContextRequest(session, 'bob', 'next'))
            assert {row.content for row in prepared.history} == {'慢识别的甲', '快识别的乙'}
            assert len(prepared.history) == 2
        finally:
            await service.close()
    asyncio.run(run())


def test_room_history_does_not_expand_personal_write_grants_or_mix_source_replies(tmp_path):
    async def run():
        service = create_context_service(str(tmp_path / 'history.jsonl'), timeout=.01)
        session = SessionRef('run', 'binding', 's', RoomTarget('a', 'c'))
        try:
            await prepare_source(service.bridge, {'ok': True, 'persona': 'first', 'provider': 'memory',
                'memory_scopes': [{'umo': 'first'}]})
            service.observe(Observation('alice', session, 'asr', 1, 'recognized', '请记住我喜欢数学', 'alice', 'alice'))
            first = await service.prepare_context(ContextRequest(session, 'alice', 'alice'))
            assert len(first.authorized_write_grants) == 1
            service.record_output(OutputRecord('reply', session,
                ConfirmedTurn('请记住我喜欢数学', '记下了', 'completed', 'alice', 'c'),
                ActionOutcome('reply', 'completed', session), first.context_ref, 'alice'))
            service.observe(Observation('bob', session, 'asr', 2, 'recognized', '我呢', 'bob', 'bob'))
            bob = await service.prepare_context(ContextRequest(session, 'bob', 'bob'))
            assert not bob.authorized_write_grants
            forged = await service.prepare_context(ContextRequest(session, 'bob', 'alice'))
            assert not forged.authorized_write_grants
            await asyncio.sleep(0)
            pending = await service.bridge.poll(wait=0)
            commits = [row for row in pending if row.get('operation') == 'commit']
            assert len(commits) == 1
            assert commits[0]['payload']['speaker'] == 'alice'
            assert commits[0]['scope']['memory_scopes'] == [{'umo': 'first'}]
            await prepare_source(service.bridge, {'ok': True, 'persona': 'second', 'provider': 'memory',
                'memory_scopes': [{'umo': 'second'}]})
            changed = await service.prepare_context(ContextRequest(session, 'bob', 'bob'))
            assert [row.content for row in changed.history] == ['请记住我喜欢数学']
        finally:
            await service.close()
    asyncio.run(run())


def test_recent_room_window_is_bounded_without_deleting_personal_history(tmp_path, monkeypatch):
    async def run():
        service = create_context_service(str(tmp_path / 'history.jsonl'), max_turns=3)
        session = SessionRef('run', 'binding', 's', RoomTarget('a', 'c'))
        clock = [1000.0]
        monkeypatch.setattr('time.time', lambda: clock[0])
        try:
            service.observe(Observation('old', session, 'asr', 1, 'recognized', '上周末散心', 'alice', 'old'))
            clock[0] += 901
            for number in range(4):
                service.observe(Observation(str(number), session, 'asr', number+2, 'recognized', str(number), 'bob', str(number)))
            prepared = await service.prepare_context(ContextRequest(session, 'alice', 'next'))
            assert [row.content for row in prepared.history] == ['1', '2', '3']
            assert service.history.recent(user_key='alice')[0]['content'] == '上周末散心'
        finally:
            await service.close()
    asyncio.run(run())
