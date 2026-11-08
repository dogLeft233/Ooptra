"""Room operations keep ownership, cancellation and observable success honest."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from support.channel_fixtures import make_voice_agent as make_agent

from operations import load_voice_agent_settings


@pytest.mark.parametrize("operation", ["leave", "stop_current_reply"])
def test_stop_reply_when_backend_query_completes_does_not_wait_forever(tmp_path, monkeypatch, operation):
    from dialogue.models import Recognition

    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c")
        worker = agent._round_task
        tasks = []

        async def completed_query():
            asyncio.get_running_loop().call_soon(
                lambda: tasks.append(asyncio.create_task(getattr(agent, operation)()))
            )
            return Recognition("input", agent.channels.snapshot().session, "attempt", "member", "no_speech")

        async def handle(*args, **kwargs):
            return await asyncio.wait_for(completed_query(), 1)

        monkeypatch.setattr(agent.backend, "recognize", handle)
        agent._offer_utterance("member", b"\x00\x00", 16000)
        try:
            while not tasks:
                await asyncio.sleep(0)
            done, _ = await asyncio.wait(tasks, timeout=.2)
            assert done, "room operation waited forever for a cancelled reply worker"
            assert tasks[0].exception() is None
            if operation == "leave":
                assert not agent.status()["joined"]
            else:
                assert agent._round_task is not worker
                assert agent.status()["joined"]
        finally:
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, *tasks, return_exceptions=True)
            await agent.leave()

    asyncio.run(run())


def test_events_are_outside_lock_and_duplicate_leave_is_silent(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        events = []

        async def callback(event):
            assert not agent._operation_lock.locked()
            events.append(event)

        agent.set_operation_callback(callback)
        await agent.join("a", "c")
        await agent.leave()
        await agent.leave()
        assert [(e.kind, e.source, e.area, e.channel) for e in events] == [
            ("join", "manual", "a", "c"), ("leave", "manual", "a", "c")
        ]
        assert agent.status()["join_source"] == ""
    asyncio.run(run())


def test_old_visit_cannot_leave_new_room(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="old")
        await agent.join("b", "d")
        result = await agent.leave(source="auto", expected_visit_id="old")
        assert not result["ok"]
        assert agent.status()["joined"] and agent.status()["area"] == "b"
        assert not agent.is_auto_visit_current("old")
        assert "visit_id" not in agent.status()
        await agent.leave()
    asyncio.run(run())


def test_old_empty_room_check_cannot_leave_a_rejoined_room(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c")
        old_epoch = agent.operation_epoch
        await agent.join("a", "c")
        result = await agent.leave(expected_operation_epoch=old_epoch)
        assert not result["ok"]
        assert agent.status()["joined"] and agent.status()["channel"] == "c"
        assert agent.test_platform.voice.leaves == 1
        await agent.leave()
    asyncio.run(run())


def test_empty_guard_uses_real_agent_and_retries_failed_manual_exit(tmp_path):
    async def run():
        from behavior import create_auto_visits as AutoVisitController
        from behavior import create_visit_store as AutoVisitStateStore
        from behavior import parse_auto_visit_config

        agent = make_agent(tmp_path)
        agent.test_platform.config = SimpleNamespace(person_uid="bot")

        async def members(area):
            assert area == "a"
            return {"channelMembers": {"c": [{"uid": "bot"}]}}

        agent.test_platform.channels = SimpleNamespace(get_voice_channel_members=members)
        elapsed = 0
        clock = SimpleNamespace(
            monotonic=lambda: elapsed,
            utcnow=lambda: datetime(2026, 10, 7, tzinfo=timezone.utc) + timedelta(seconds=elapsed),
            sleep=asyncio.sleep,
        )
        store = AutoVisitStateStore(tmp_path / "visits.json")
        ctrl = AutoVisitController(agent, parse_auto_visit_config({}), store, enabled=lambda: agent.settings.enabled, clock=clock)
        agent.set_operation_callback(ctrl.on_operation)
        ctrl.set_bot_ready(True)
        try:
            await agent.join("a", "c")
            await ctrl._check_empty_room()
            agent.test_platform.voice.fail_leave = True
            elapsed = 30
            await ctrl._check_empty_room()
            assert agent.status()["joined"]
            assert "leave failed" in ctrl.status()["last_error"]
            assert "a" not in store.snapshot(clock.utcnow())["area_cooldowns"]
            agent.test_platform.voice.fail_leave = False
            await ctrl._check_empty_room()
            elapsed = 59
            await ctrl._check_empty_room()
            assert agent.status()["joined"]
            elapsed = 60
            await ctrl._check_empty_room()
            assert not agent.status()["joined"]
            assert agent.test_platform.voice.leaves == 1
            assert store.snapshot(clock.utcnow())["area_cooldowns"]["a"]
        finally:
            await ctrl.stop()
            await agent.stop()
    asyncio.run(run())


def test_room_switch_emits_system_leave_even_if_new_join_fails(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        events = []

        async def callback(event):
            events.append(event)

        agent.set_operation_callback(callback)
        await agent.join("a", "c", source="auto", visit_id="v")

        async def fail(**kwargs):
            raise RuntimeError("join failed")

        agent.test_platform.voice.join = fail
        with pytest.raises(RuntimeError, match="join failed"):
            await agent.join("b", "d")
        assert [(e.kind, e.source) for e in events] == [("join", "auto"), ("leave", "system")]
        assert not agent.status()["joined"]
    asyncio.run(run())


def test_leave_failure_preserves_room_and_emits_no_success(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")
        agent.test_platform.voice.fail_leave = True
        agent._speaking = True
        result = await agent.leave(source="auto", expected_visit_id="v")
        assert not result["ok"] and agent.is_auto_visit_current("v")
        assert not agent.status()["speaking"]
        agent.test_platform.voice.fail_leave = False
        await agent.leave()
    asyncio.run(run())


def test_generation_before_audio_is_still_a_reply(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        agent._reply_generating = True
        assert not await agent.wait_for_reply_end(timeout=0.01)
        agent._reply_generating = False
        assert await agent.wait_for_reply_end(timeout=0.1)
    asyncio.run(run())


def test_legacy_auto_join_is_ignored(monkeypatch):
    import config
    monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", {"auto_join": True})
    settings, _ = load_voice_agent_settings()
    assert not hasattr(settings, "auto_join")


@pytest.mark.parametrize("failure", ["browser", "membership", "rejected"])
def test_sdk_leave_failure_does_not_clear_membership(failure):
    async def run():
        from oopz_sdk.services.voice import Voice

        voice = Voice.__new__(Voice)
        voice._identity_task = None
        voice._current_area = "a"
        voice._current_channel = "c"
        voice._current_sign = object()
        voice._current_uid = "self"
        voice._config = SimpleNamespace(person_uid="self")

        async def leave_browser():
            if failure == "browser":
                raise RuntimeError("browser leave failed")

        async def leave_membership(**kwargs):
            if failure == "membership":
                raise RuntimeError("membership leave failed")
            if failure == "rejected":
                return SimpleNamespace(ok=False, message="membership rejected")

        voice.backend = SimpleNamespace(leave=leave_browser)
        voice._bot = SimpleNamespace(channels=SimpleNamespace(leave_voice_channel=leave_membership))
        with pytest.raises(RuntimeError):
            await voice.leave()
        assert voice._current_area == "a" and voice._current_channel == "c"
    asyncio.run(run())


def test_concurrent_join_and_leave_never_overlap_sdk_operations(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        joining = asyncio.Event()
        release = asyncio.Event()
        trace = []

        async def join(**kwargs):
            trace.append("joining")
            joining.set()
            await release.wait()
            trace.append("joined")
            return SimpleNamespace(rtc_channel_name="rtc")

        async def leave():
            trace.append("leave")

        agent.test_platform.voice.join = join
        agent.test_platform.voice.leave = leave
        task = asyncio.create_task(agent.join("a", "c"))
        await joining.wait()
        exit_task = asyncio.create_task(agent.leave())
        await asyncio.sleep(0)
        assert trace == ["joining"]
        release.set()
        await asyncio.gather(task, exit_task)
        assert trace == ["joining", "joined", "leave"]
        assert not agent.status()["joined"]
    asyncio.run(run())


def test_manual_leave_cancels_quick_speak_before_tts_is_generated(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c")
        started = asyncio.Event()

        async def tts(text):
            started.set()
            await asyncio.Event().wait()

        from support.expression_runtime import ScriptedExpression
        agent.backend = ScriptedExpression()
        agent.backend.tts = tts
        task = asyncio.create_task(agent.speak_text("text"))
        await started.wait()
        assert not await agent.wait_for_reply_end(0.01)
        await asyncio.wait_for(agent.leave(), 1)
        assert task.done(), "generating reply outlived the departed room"
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_auto_join_cannot_replace_a_manual_room(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("manual-area", "manual-room")
        with pytest.raises(RuntimeError, match="occupied"):
            await agent.join("auto-area", "auto-room", source="auto", visit_id="v")
        assert agent.status()["area"] == "manual-area"
        assert agent.test_platform.voice.leaves == 0
        await agent.leave()
    asyncio.run(run())


def test_auto_join_rejects_epoch_replaced_by_manual_join_then_leave(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        epoch = agent.operation_epoch
        await agent.join("manual-area", "manual-room")
        await agent.leave()
        with pytest.raises(RuntimeError, match="superseded"):
            await agent.join("auto-area", "auto-room", source="auto", visit_id="v",
                             expected_operation_epoch=epoch)
        assert not agent.status()["joined"]
    asyncio.run(run())


def test_failed_system_leave_closes_backend_and_suspends_listening(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")
        closes = []
        listening = []

        async def close():
            closes.append(True)

        async def listen(enabled=True):
            listening.append(enabled)
            return {"ok": True}

        agent.backend.aclose = close
        agent.duplex.enable_listen = listen
        agent.test_platform.voice.fail_leave = True
        agent.set_connection_ready(False)
        result = await agent.leave(source="system")
        assert not result["ok"] and agent.is_auto_visit_current("v")
        assert closes and listening[-1] is False
        assert agent._round_task is None and agent._voice_suspended
        agent.test_platform.voice.fail_leave = False
        assert (await agent.leave())["ok"]
    asyncio.run(run())


def test_retirement_blocks_new_remote_input_without_stopping_current_reply(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")
        agent._speaking = True
        assert not agent.begin_auto_retirement("old")
        assert agent.begin_auto_retirement("v")
        await agent._on_remote_pcm("human", b"\x00\x40" * 320, 16000)
        assert agent._speaking, "new human audio barged into the retiring reply"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(agent.reply_scheduler.next(0), 0.01)
        await agent.leave()
        assert not agent._retiring_visit_id
    asyncio.run(run())
