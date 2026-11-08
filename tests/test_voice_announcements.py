import asyncio
import base64
import json

import pytest
from test_gemini_session_lifecycle import FakeLiveWs
from test_voice_operations import make_agent


class AnnouncementDuplex:
    def __init__(self):
        self.audio = []
        self.drained = asyncio.Event()

    async def enable_listen(self, enabled=True):
        return {"ok": True}

    async def push_tts_pcm(self, pcm, rate, *, finish=False):
        self.audio.append(pcm)
        return {"ok": True}

    async def stop_tts(self):
        self.audio.clear()
        return {"ok": True}

    async def wait_tts_complete(self, timeout):
        await asyncio.wait_for(self.drained.wait(), timeout)
        return {"ok": True}


def test_mimo_every_request_rewrites_and_waits_for_tail(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        duplex = AnnouncementDuplex()
        agent.duplex = duplex
        await agent.join("a", "c", source="auto", visit_id="v")
        requests = []

        async def rewrite(kind, template):
            requests.append((kind, template))
            return "大家在玩什么呀？"

        async def tts(text):
            assert text == "大家在玩什么呀？"
            return b"\x00\x01" * 100, 24000

        agent.backend.rewrite_announcement = rewrite
        agent.backend.tts = tts
        task = asyncio.create_task(agent.speak_announcement("enter", "在玩什么", expected_visit_id="v"))
        await asyncio.sleep(0.04)
        assert not task.done()
        assert agent._announcement_task is not None
        duplex.drained.set()
        assert (await task)["ok"]
        assert (await agent.speak_announcement("leave", "再见", expected_visit_id="v"))["ok"]
        assert requests == [("enter", "在玩什么"), ("leave", "再见")]
        assert not agent.last_user_text
        await agent.leave()
    asyncio.run(run())


def test_empty_ai_result_and_timeout_restore_input(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")

        async def rewrite(kind, template):
            return ""

        agent.backend.rewrite_announcement = rewrite
        result = await agent.speak_announcement("enter", "hi", expected_visit_id="v")
        assert not result["ok"] and result["error"]
        assert not agent._announcement_active
        await agent.leave()
    asyncio.run(run())


def test_manual_join_cancels_generation_and_old_audio(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")
        began = asyncio.Event()

        async def rewrite(kind, template):
            began.set()
            await asyncio.Event().wait()

        agent.backend.rewrite_announcement = rewrite
        task = asyncio.create_task(agent.speak_announcement("enter", "hi", expected_visit_id="v"))
        await began.wait()
        # Python 3.10 Task has no cancelling(); keep the runtime contract explicit.
        original_current = asyncio.current_task
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "current_task", lambda: object())
            assert (await asyncio.wait_for(agent.join("b", "d"), 1))["ok"]
            assert not (await task)["ok"]
        assert asyncio.current_task is original_current
        assert agent.status()["area"] == "b"
        assert not agent._announcement_active
        await agent.leave()
    asyncio.run(run())


def test_announcement_caller_cancellation_propagates_and_cleans_generation(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")
        began, disposed = asyncio.Event(), asyncio.Event()

        async def rewrite(kind, template):
            began.set()
            try:
                await asyncio.Event().wait()
            finally:
                disposed.set()

        agent.backend.rewrite_announcement = rewrite
        task = asyncio.create_task(agent.speak_announcement("enter", "hi", expected_visit_id="v"))
        await began.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert disposed.is_set()
        assert not agent._announcement_active
        assert agent._announcement_task is None
        await agent.leave()
    asyncio.run(run())


def test_simultaneous_child_and_caller_cancellation_is_not_swallowed(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")
        began = asyncio.Event()

        async def rewrite(kind, template):
            began.set()
            await asyncio.Event().wait()

        agent.backend.rewrite_announcement = rewrite
        caller = asyncio.create_task(agent.speak_announcement("enter", "hi", expected_visit_id="v"))
        await began.wait()
        child = agent._announcement_task
        child.add_done_callback(lambda _: caller.cancel())
        child.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not agent._announcement_active
        assert agent._announcement_task is None
        await agent.leave()
    asyncio.run(run())


def test_gemini_uses_live_session_without_mimo_and_ignores_internal_user_memory(tmp_path, monkeypatch):
    async def run():
        from support import dialogue_fixtures as ws_transport
        from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend

        sockets = []

        async def open_ws(*args, **kwargs):
            ws = FakeLiveWs()
            sockets.append(ws)
            return ws

        monkeypatch.setattr(ws_transport, "live_connector", open_ws)
        agent = make_agent(tmp_path)
        agent.settings.gemini_api_key = "test"
        agent.settings.mimo_api_key = ""
        agent.backend = GeminiLiveBackend(agent.settings, agent.memory)
        agent.live_mode = True
        duplex = AnnouncementDuplex()
        agent.duplex = duplex
        await agent.join("a", "c", source="auto", visit_id="v")
        await agent._wire_live()
        duplex.drained.set()
        for kind in ("enter", "leave"):
            task = asyncio.create_task(agent.speak_announcement(kind, "意图", expected_visit_id="v"))
            while len(sockets[0].sent) < (2 if kind == "enter" else 3):
                await asyncio.sleep(0)
            assert agent.backend.reply_active
            await sockets[0]._queue.put(json.dumps({"serverContent": {
                "inputTranscription": {"text": "内部语音事件指令"},
                "outputTranscription": {"text": "大家好呀！"},
                "modelTurn": {"parts": [{"inlineData": {"data": base64.b64encode(b"\x01\x00" * 100).decode()}}]},
                "turnComplete": True,
            }}))
            assert (await task)["ok"]
        assert len(sockets) == 1
        assert len(sockets[0].sent) == 3
        assert all(row["role"] == "assistant" for row in agent.memory.recent())
        await agent.leave()
    asyncio.run(run())


def test_announcement_timeout_stops_live_session_and_restores_input(tmp_path, monkeypatch):
    async def run():
        from support import dialogue_fixtures as ws_transport
        from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend

        ws = FakeLiveWs()

        async def open_ws(*args, **kwargs):
            return ws

        monkeypatch.setattr(ws_transport, "live_connector", open_ws)
        agent = make_agent(tmp_path)
        agent.settings.gemini_api_key = "test"
        agent.backend = GeminiLiveBackend(agent.settings, agent.memory)
        agent.live_mode = True
        await agent.join("a", "c", source="auto", visit_id="v")
        result = await agent.speak_announcement("enter", "意图", expected_visit_id="v", timeout=0.02)
        assert not result["ok"] and "timed out" in result["error"]
        assert ws.closed and not agent._announcement_active
        assert agent.is_auto_visit_current("v")
        await agent.leave()
    asyncio.run(run())


def test_live_timeout_does_not_mute_next_session_callbacks(tmp_path, monkeypatch):
    async def run():
        from support import dialogue_fixtures as ws_transport
        from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend

        sockets = []

        async def open_ws(*args, **kwargs):
            ws = FakeLiveWs()
            sockets.append(ws)
            return ws

        monkeypatch.setattr(ws_transport, "live_connector", open_ws)
        agent = make_agent(tmp_path)
        agent.settings.gemini_api_key = "test"
        agent.backend = GeminiLiveBackend(agent.settings, agent.memory)
        agent.live_mode = True
        agent.duplex = AnnouncementDuplex()
        await agent.join("a", "c", source="auto", visit_id="v")
        await agent._wire_live()
        stale_audio = agent.backend._audio_out
        assert not (await agent.speak_announcement("enter", "hi", expected_visit_id="v", timeout=0.01))["ok"]
        await agent.backend.push_audio(b"\x00\x01" * 320, 16000)
        assert len(sockets) == 2, "input stayed disabled after failed announcement"
        await stale_audio(b"old", 24000)
        assert not agent.duplex.audio
        await agent.backend._audio_out(b"new", 24000)
        assert agent.duplex.audio == [b"new"]
        await agent.leave()
    asyncio.run(run())


def test_live_audio_without_any_text_is_an_empty_ai_result(tmp_path, monkeypatch):
    async def run():
        from support import dialogue_fixtures as ws_transport
        from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend

        ws = FakeLiveWs()

        async def open_ws(*args, **kwargs):
            return ws

        monkeypatch.setattr(ws_transport, "live_connector", open_ws)
        agent = make_agent(tmp_path)
        agent.settings.gemini_api_key = "test"
        agent.backend = GeminiLiveBackend(agent.settings, agent.memory)
        agent.live_mode = True
        agent.duplex = AnnouncementDuplex()
        await agent.join("a", "c", source="auto", visit_id="v")
        agent.duplex.drained.set()
        await agent._wire_live()
        task = asyncio.create_task(agent.speak_announcement("enter", "hi", expected_visit_id="v"))
        while len(ws.sent) < 2:
            await asyncio.sleep(0)
        await ws._queue.put(json.dumps({"serverContent": {
            "modelTurn": {"parts": [{"inlineData": {"data": base64.b64encode(b"pcm").decode()}}]},
            "turnComplete": True,
        }}))
        result = await task
        assert not result["ok"] and "empty text" in result["error"]
        await agent.leave()
    asyncio.run(run())


def test_manual_leave_cancels_live_audio_push_before_waiting_for_output_lock(tmp_path, monkeypatch):
    async def run():
        from support import dialogue_fixtures as ws_transport
        from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend

        ws = FakeLiveWs()

        async def open_ws(*args, **kwargs):
            return ws

        monkeypatch.setattr(ws_transport, "live_connector", open_ws)
        agent = make_agent(tmp_path)
        agent.settings.gemini_api_key = "test"
        agent.backend = GeminiLiveBackend(agent.settings, agent.memory)
        agent.live_mode = True
        await agent.join("a", "c")
        entered = asyncio.Event()

        async def stalled_push(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        agent.duplex.push_tts_pcm = stalled_push
        await ws._queue.put(json.dumps({"serverContent": {
            "modelTurn": {"parts": [{"inlineData": {"data": base64.b64encode(b"pcm").decode()}}]},
        }}))
        await entered.wait()
        assert (await asyncio.wait_for(agent.leave(), 0.2))["ok"]
        assert ws.closed and not agent.status()["joined"]
    asyncio.run(run())


def test_announcement_does_not_mix_with_an_existing_reply_tail(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        duplex = AnnouncementDuplex()
        agent.duplex = duplex
        await agent.join("a", "c", source="auto", visit_id="v")
        rewrites = []

        async def rewrite(kind, template):
            rewrites.append(template)
            return "大家好"

        async def tts(text):
            return b"pcm", 24000

        agent.backend.rewrite_announcement = rewrite
        agent.backend.tts = tts
        task = asyncio.create_task(agent.speak_announcement("enter", "hi", expected_visit_id="v"))
        await asyncio.sleep(0.02)
        assert not rewrites, "new AI output mixed with an undrained previous reply"
        duplex.drained.set()
        assert (await task)["ok"]
        await agent.leave()
    asyncio.run(run())


@pytest.mark.parametrize("repeat_caller_cancel", [False, True])
def test_caller_cancel_during_operation_cleanup_waits_then_resets_all_flags(tmp_path, repeat_caller_cancel):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")
        rewriting = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        cleaned = []

        async def rewrite(kind, template):
            rewriting.set()
            await asyncio.Event().wait()

        async def stop_tts():
            cleaning.set()
            await release.wait()
            cleaned.append(True)
            return {"ok": True}

        agent.backend.rewrite_announcement = rewrite
        agent.duplex.stop_tts = stop_tts
        caller = asyncio.create_task(agent.speak_announcement("enter", "hi", expected_visit_id="v"))
        await rewriting.wait()
        operation = asyncio.create_task(agent._cancel_announcement())
        await cleaning.wait()
        caller.cancel()
        await asyncio.sleep(0)
        if repeat_caller_cancel:
            caller.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await operation
        assert cleaned, "second cancellation aborted the audio cleanup"
        assert not agent._announcement_active
        assert not agent._announcement_collecting
        assert not agent._reply_generating
        assert not agent._speaking
        assert agent._announcement_task is None
        assert not agent._announcement_cancel_requests
        await agent.join("manual", "room")
        input_received = []

        class InputVad:
            def feed(self, pcm):
                input_received.append(pcm)

        agent._vad_for = lambda uid: InputVad()
        await agent._on_remote_pcm("human", b"pcm", 16000)
        assert input_received == [b"pcm"]
        await agent.leave()
    asyncio.run(run())
