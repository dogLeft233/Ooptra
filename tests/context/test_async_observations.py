"""异步观察的取消、容量、原会话与关闭契约，使用临时真实SQLite。"""
import asyncio
import sqlite3
import threading

import pytest
from support.context_fixtures import prepare_source

from channels.models import RoomTarget, SessionRef
from context import create_context_service
from context.models import ContextRequest, Observation


def input_fact(session, key, sequence=1):
    return Observation(key, session, 'asr', sequence, 'recognized', key, 'alice', key)


def gate_input_write(service, monkeypatch):
    """仅控制真实事务开始的时机，不替换存储或事务结果。"""
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = service.history.commit_input

    def write(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), 'test did not release the real database write'
        return original(*args, **kwargs)

    monkeypatch.setattr(service.history, 'commit_input', write)
    return entered, release


def test_cancel_before_start_admits_nothing_and_close_drains_cancelled_waiters(tmp_path, monkeypatch):
    async def run():
        path = str(tmp_path / 'memory.jsonl')
        service = create_context_service(path)
        session = SessionRef('run', 'bind', 'old', RoomTarget('a', 'room'))
        entered, release = gate_input_write(service, monkeypatch)
        never = asyncio.create_task(service.observe_async(input_fact(session, 'never')))
        never.cancel()
        with pytest.raises(asyncio.CancelledError):
            await never
        first = asyncio.create_task(service.observe_async(input_fact(session, 'first', 2)))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(service.observe_async(input_fact(session, 'second', 1)))
        await asyncio.sleep(0)  # 让第二个协程受理；落盘仍由事件门闩阻止。
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        closing = asyncio.create_task(service.close())
        try:
            await asyncio.sleep(0)
            assert not closing.done()
            with pytest.raises(RuntimeError, match='关闭'):
                await service.observe_async(input_fact(session, 'after-close'))
        finally:
            release.set()
            await closing
        restored = create_context_service(path)
        try:
            assert [row['content'] for row in restored.history.recent()] == ['first', 'second']
        finally:
            await restored.close()
    asyncio.run(run())


def test_old_session_and_out_of_order_inputs_cannot_replace_new_projection(tmp_path, monkeypatch):
    async def run():
        service = create_context_service(str(tmp_path / 'memory.jsonl'))
        old = SessionRef('run', 'bind', 'old', RoomTarget('a', 'room'))
        new = SessionRef('run', 'bind', 'new', old.room_target)
        entered, release = gate_input_write(service, monkeypatch)
        first = asyncio.create_task(service.observe_async(input_fact(old, 'old-first', 2)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            switch = asyncio.create_task(service.observe_async(Observation('new-session', new, 'new', 0, 'session')))
            await asyncio.sleep(0)
            current = asyncio.create_task(service.observe_async(input_fact(new, 'new-input', 3)))
            await asyncio.sleep(0)
            late = asyncio.create_task(service.observe_async(input_fact(old, 'old-late', 1)))
            release.set()
            await asyncio.gather(first, switch, current, late)
            await service.observe_async(input_fact(old, 'old-late', 1))
            snapshot = await service.snapshot_async(new.room_target)
            assert snapshot.session == new
            assert snapshot.latest_input == 'new-input'
            assert [row.content for row in snapshot.history] == ['old-first', 'new-input', 'old-late']
            prepared = await service.prepare_context(ContextRequest(new, 'alice', 'next', known_revision=snapshot.revision))
            assert prepared.history == snapshot.history
            assert prepared.version.history_revision == 3
        finally:
            release.set()
            await service.close()
    asyncio.run(run())


def test_observation_capacity_includes_cancelled_waiters(tmp_path, monkeypatch):
    async def run():
        service = create_context_service(str(tmp_path / 'memory.jsonl'))
        session = SessionRef('run', 'bind', 's', RoomTarget('a', 'room'))
        entered, release = gate_input_write(service, monkeypatch)
        callers = [asyncio.create_task(service.observe_async(input_fact(session, '0', 0)))]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            callers.extend(asyncio.create_task(service.observe_async(input_fact(session, str(i), i)))
                           for i in range(1, 128))
            await asyncio.sleep(0)
            for caller in callers:
                caller.cancel()
            await asyncio.gather(*callers, return_exceptions=True)
            with pytest.raises(BufferError):
                await service.observe_async(input_fact(session, 'overflow', 129))
        finally:
            release.set()
            await service.close()
        restored = create_context_service(str(tmp_path / 'memory.jsonl'), max_turns=200)
        try:
            assert len(restored.history.recent()) == 128
        finally:
            await restored.close()
    asyncio.run(run())


def test_prepare_keeps_original_source_when_binding_changes_during_database_read(tmp_path, monkeypatch):
    async def run():
        service = create_context_service(str(tmp_path / 'memory.jsonl'))
        session = SessionRef('run', 'bind', 's', RoomTarget('a', 'c'))
        await prepare_source(service.bridge, {'ok': True, 'persona': 'first', 'provider': 'memory',
            'memory_scopes': [{'umo': 'first'}]})
        original_ref = (await service.prepare_context(ContextRequest(session))).context_ref
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = service.history.scoped_history_revision

        def read(**kwargs):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            return original(**kwargs)

        monkeypatch.setattr(service.history, 'scoped_history_revision', read)
        preparing = asyncio.create_task(service.prepare_context(ContextRequest(session)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await prepare_source(service.bridge, {'ok': True, 'persona': 'second', 'provider': 'memory',
                'memory_scopes': [{'umo': 'second'}]})
            release.set()
            prepared = await preparing
            assert prepared.context_ref == original_ref
            assert prepared.persona == 'first'
            assert not service.tools(prepared).active
        finally:
            release.set()
            await asyncio.gather(preparing, return_exceptions=True)
            await service.close()
    asyncio.run(run())


def test_prepare_captures_source_before_waiting_for_observation_lock(tmp_path, monkeypatch):
    async def run():
        service = create_context_service(str(tmp_path / 'memory.jsonl'))
        session = SessionRef('run', 'bind', 's', RoomTarget('a', 'c'))
        await prepare_source(service.bridge, {'ok': True, 'persona': 'first', 'provider': 'memory',
            'memory_scopes': [{'umo': 'first'}]})
        await service.observe_async(Observation('remember', session, 'asr', 1, 'recognized',
            '请记住我喜欢数学', 'alice', 'remember'))
        original_ref = (await service.prepare_context(ContextRequest(session))).context_ref
        entered, release = gate_input_write(service, monkeypatch)
        blocker = asyncio.create_task(service.observe_async(input_fact(session, 'blocked', 2)))
        preparing = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            preparing = asyncio.create_task(service.prepare_context(ContextRequest(session, 'alice', 'remember')))
            await asyncio.sleep(0)  # 请求已进入内部锁等待，来源必须已固定。
            await prepare_source(service.bridge, {'ok': True, 'persona': 'second', 'provider': 'memory',
                'memory_scopes': [{'umo': 'second'}]})
            release.set()
            prepared = await preparing
            assert prepared.context_ref == original_ref
            assert prepared.persona == 'first'
            assert not prepared.authorized_write_grants
            assert not service.tools(prepared).active
        finally:
            release.set()
            await asyncio.gather(blocker, *([preparing] if preparing else []), return_exceptions=True)
            await service.close()
    asyncio.run(run())


def test_cancelled_waiter_failure_is_observed_once_and_close_finishes(tmp_path, monkeypatch, caplog):
    async def run():
        service = create_context_service(str(tmp_path / 'memory.jsonl'))
        session = SessionRef('run', 'bind', 's', RoomTarget('a', 'c'))
        with sqlite3.connect(service.history.path) as db:
            db.execute("CREATE TRIGGER fail_input BEFORE INSERT ON history BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        entered, release = gate_input_write(service, monkeypatch)
        caller = asyncio.create_task(service.observe_async(input_fact(session, 'cancelled')))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
        finally:
            release.set()
            await service.close()
        errors = [row for row in caplog.records if row.exc_info]
        assert len(errors) == 1
        assert 'session_id=s input_ref=cancelled' in errors[0].message
    asyncio.run(run())
