"""Model intent controls a real room operation, with farewell playback first."""

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from test_gemini_session_lifecycle import FakeLiveWs
from test_voice_announcements import AnnouncementDuplex
from test_voice_operations import make_agent

from voice_agent.backends import create_backend
from voice_agent.settings import load_voice_agent_settings
from voice_agent.voice_control import VoiceControlDecision
from webui import config_editor


class DepartureDuplex(AnnouncementDuplex):
    async def wait_tts_complete(self, timeout):
        # stop_tts emptied the old reply; only newly queued farewell audio must drain.
        if not self.audio:
            return {"ok": True}
        return await super().wait_tts_complete(timeout)


def test_voice_leave_switch_defaults_on_and_can_be_saved_off(monkeypatch):
    import config

    monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", {})
    settings, _ = load_voice_agent_settings()
    assert settings.voice_leave_enabled is True
    field = config_editor.schema_payload()["voice"]["fields"]["voice_leave_enabled"]
    assert (field["type"], field["tier"], field["value"]) == ("bool", "basic", True)
    assert config_editor._normalize_updates({"voice": {"voice_leave_enabled": False}}) == {
        "voice": {"voice_leave_enabled": False},
    }
    monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", {"voice_leave_enabled": False})
    settings, _ = load_voice_agent_settings()
    assert settings.voice_leave_enabled is False


@pytest.mark.parametrize("raw,expected", [
    ('{"leave": true}', True), ('{"leave": false}', False),
    ('{"leave": "true"}', False), ('{"leave": 1}', False),
    ('[]', False), ('好的，我退语音了', False),
])
def test_mimo_intent_uses_isolated_model_request_and_strict_boolean(tmp_path, monkeypatch, raw, expected):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        requests = []
        agent.settings.mimo_api_key = "test"
        class Response:
            status = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def json(self, **kwargs):
                return {"choices": [{"message": {"content": raw}}]}
        class Session:
            def post(self, url, **kwargs):
                requests.append(kwargs["json"])
                return Response()
        async def http():
            return Session()
        monkeypatch.setattr(agent.backend, "_http", http)
        assert (await agent.backend.detect_control_intent("机器人，你先下吧")).leave is expected
        assert requests[0]["messages"][-1] == {"role": "user", "content": "机器人，你先下吧"}
        instructions = requests[0]["messages"][0]["content"]
        assert "不要退语音" in instructions
        assert "你出去吧" in instructions and "滚出去" in instructions
        assert "不要出去" in instructions and "他说你出去吧" in instructions
        assert not agent.memory.recent()
        await agent.backend.aclose()
    asyncio.run(run())


def test_mimo_zero_probability_still_detects_intent_and_leaves_after_audio_tail(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.voice_leave_enabled = True
        agent.settings.reply_probability_percent = 0
        events = []
        agent.set_operation_callback(lambda event: record(events, event))
        duplex = DepartureDuplex()
        agent.duplex = duplex
        await agent.join("a", "c")
        calls = []
        async def asr(*args):
            calls.append("asr")
            return "机器人，你先退语音吧"
        async def intent(text):
            calls.append("intent")
            return VoiceControlDecision(leave=True)
        async def rewrite(kind, template):
            calls.append("farewell")
            assert kind == "leave"
            return "拜拜，我先下啦"
        async def tts(text):
            return b"\x01\x00" * 20, 24000
        monkeypatch.setattr(agent.backend, "asr", asr)
        monkeypatch.setattr(agent.backend, "detect_control_intent", intent)
        monkeypatch.setattr(agent.backend, "rewrite_announcement", rewrite)
        monkeypatch.setattr(agent.backend, "tts", tts)
        # Exercise the actual mailbox worker; departure must not cancel/await itself.
        agent._offer_utterance("member", b"spoken command", 16000)
        for _ in range(100):
            if "farewell" in calls:
                break
            await asyncio.sleep(.005)
        assert calls == ["asr", "intent", "farewell"]
        assert agent._bot.voice.leaves == 0 and agent.status()["joined"]
        assert agent._voice_control_task is not None
        task = agent._voice_control_task
        duplex.drained.set()
        await asyncio.wait_for(task, 1)
        assert not agent.status()["joined"] and agent._bot.voice.leaves == 1
        assert (events[-1].kind, events[-1].source) == ("leave", "manual")
        assert not agent._reply_generating and not agent._announcement_active
    asyncio.run(run())


async def record(events, event):
    events.append(event)


@pytest.mark.parametrize("switch,classification", [(False, True), (True, False)])
def test_mimo_switch_off_or_non_leave_intent_never_leaves(tmp_path, monkeypatch, switch, classification):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.voice_leave_enabled = switch
        agent.settings.reply_probability_percent = 0
        called = []
        async def asr(*args):
            return "不要退语音"
        async def intent(text):
            called.append(text)
            return VoiceControlDecision(leave=classification)
        monkeypatch.setattr(agent.backend, "asr", asr)
        monkeypatch.setattr(agent.backend, "detect_control_intent", intent)
        await agent.join("a", "c")
        await agent._handle_utterance("member", b"spoken words", 16000)
        assert bool(called) is switch
        assert agent.status()["joined"] and agent._bot.voice.leaves == 0
        await agent.leave()
    asyncio.run(run())


def test_manual_room_change_cancels_voice_departure_without_leaving_new_room(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.voice_leave_enabled = True
        await agent.join("a", "c")
        began = asyncio.Event()
        async def rewrite(*args):
            began.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(agent.backend, "rewrite_announcement", rewrite)
        assert agent.request_voice_leave()
        assert agent.request_voice_leave()  # Same operation, never duplicate farewell/exit.
        await asyncio.wait_for(began.wait(), 1)
        task = agent._voice_control_task
        await asyncio.wait_for(agent.join("b", "d"), 1)
        assert task.done()
        assert agent.status()["joined"] and agent.status()["area"] == "b"
        assert agent._bot.voice.leaves == 1
        await agent.leave()
    asyncio.run(run())


def test_failed_farewell_does_not_block_real_departure(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.voice_leave_enabled = True
        await agent.join("a", "c")
        async def rewrite(*args):
            raise RuntimeError("AI unavailable")
        monkeypatch.setattr(agent.backend, "rewrite_announcement", rewrite)
        assert agent.request_voice_leave()
        await asyncio.wait_for(agent._voice_control_task, 1)
        assert not agent.status()["joined"] and agent._bot.voice.leaves == 1
    asyncio.run(run())


def test_disabling_voice_control_cancels_pending_farewell_and_keeps_room(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.voice_leave_enabled = True
        await agent.join("a", "c")
        began = asyncio.Event()
        async def rewrite(*args):
            began.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(agent.backend, "rewrite_announcement", rewrite)
        assert agent.request_voice_leave()
        await asyncio.wait_for(began.wait(), 1)
        task = agent._voice_control_task
        agent.settings.voice_leave_enabled = False
        await asyncio.wait_for(agent.refresh(["voice_leave_enabled"]), 1)
        assert task.done() and agent._voice_control_task is None
        assert agent.status()["joined"] and agent._bot.voice.leaves == 0
        assert not agent.request_voice_leave()
        await agent.leave()
    asyncio.run(run())


def test_voice_departure_uses_area_presets_and_reports_failed_leave(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "VOICE_AUTO_VISIT_CONFIG", {
        "areas": {"a": {"overrides": {"leave_prompts": ["下次见", "晚安"]}}},
    }, raising=False)
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.voice_leave_enabled = True
        await agent.join("a", "c")
        agent._bot.voice.fail_leave = True
        async def announce(kind, template, **kwargs):
            assert (kind, template) in [("leave", "下次见"), ("leave", "晚安")]
            return {"ok": True}
        monkeypatch.setattr(agent, "speak_announcement", announce)
        assert agent.request_voice_leave()
        await asyncio.wait_for(agent._voice_control_task, 1)
        assert agent.status()["joined"] and agent._bot.voice.leaves == 0
        assert "退房失败" in agent.last_reply
        agent._bot.voice.fail_leave = False
        await agent.leave()
    asyncio.run(run())


def test_failed_voice_departure_restores_worker_and_accepts_spoken_retry(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.voice_leave_enabled = True
        await agent.join("a", "c")
        agent._bot.voice.fail_leave = True
        async def announce(*args, **kwargs):
            return {"ok": True}
        async def asr(*args):
            return "再试一次，退语音"
        async def intent(text):
            return VoiceControlDecision(leave=True)
        monkeypatch.setattr(agent, "speak_announcement", announce)
        monkeypatch.setattr(agent.backend, "asr", asr)
        monkeypatch.setattr(agent.backend, "detect_control_intent", intent)
        assert agent.request_voice_leave()
        await asyncio.wait_for(agent._voice_control_task, 1)
        assert agent.status()["joined"] and "退房失败" in agent.last_reply
        assert agent._round_task is not None and not agent._round_task.done()
        agent._bot.voice.fail_leave = False
        agent._offer_utterance("member", b"retry", 16000)
        for _ in range(100):
            if not agent.status()["joined"]:
                break
            await asyncio.sleep(.005)
        assert not agent.status()["joined"] and agent._bot.voice.leaves == 1
    asyncio.run(run())


@pytest.mark.parametrize("enabled,duration,minimum,intent,expected", [
    (True, 240, 500, True, []),
    (True, 270, 500, True, ["asr", "intent", "leave"]),
    (True, 270, 500, False, ["asr", "intent"]),
    (False, 270, 500, True, []),
    (True, 220, 200, False, ["asr", "intent_chat"]),
])
def test_short_voice_control_preserves_normal_reply_length_filter(
    tmp_path, monkeypatch, enabled, duration, minimum, intent, expected,
):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.voice_leave_enabled = enabled
        agent.settings.reply_probability_percent = 100
        agent.settings.force_reply_keywords = ["ooptra"]
        agent.settings.min_utterance_ms = minimum
        await agent.join("a", "c")
        calls = []
        pcm = b"\x00\x20" * (duration * 16)
        monkeypatch.setattr(agent, "_vad_for", lambda uid: SimpleNamespace(feed=lambda data: pcm))
        async def asr(*args):
            calls.append("asr")
            return "ooptra，退语音" if intent else "ooptra，好呀"
        async def classify(text):
            calls.append("intent")
            return VoiceControlDecision(leave=intent)
        async def chat(*args, **kwargs):
            calls.append("chat")
            return ""
        async def intent_chat(*args, **kwargs):
            calls.append("intent_chat")
            return VoiceControlDecision(leave=intent), ""
        async def tts(*args):
            pytest.fail("short generic utterance must not synthesize speech")
        async def announce(*args, **kwargs):
            calls.append("leave")
            return {"ok": True}
        monkeypatch.setattr(agent.backend, "asr", asr)
        monkeypatch.setattr(agent.backend, "detect_control_intent", classify)
        monkeypatch.setattr(agent.backend, "chat", chat)
        monkeypatch.setattr(agent.backend, "chat_with_intent", intent_chat)
        monkeypatch.setattr(agent.backend, "tts", tts)
        monkeypatch.setattr(agent, "speak_announcement", announce)
        await agent._on_remote_pcm("member", pcm, 16000)
        await asyncio.sleep(.01)
        # Let the real mailbox worker and any command operation complete.
        for _ in range(100):
            if len(calls) >= len(expected):
                break
            await asyncio.sleep(.005)
        assert calls == expected
        if "leave" in expected:
            task = agent._voice_control_task
            if task is not None:
                await asyncio.wait_for(task, 1)
            assert not agent.status()["joined"]
        await agent.leave()
    asyncio.run(run())


@pytest.mark.parametrize("state", ["disabled", "manual", "announcement", "unknown"])
def test_live_rejects_disabled_non_voice_and_unknown_control_calls(tmp_path, monkeypatch, state):
    from voice_agent.backends.gemini_live import GeminiLiveBackend

    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        agent.settings.voice_leave_enabled = state != "disabled"
        controls, sent = [], []
        backend.set_voice_leave_out(lambda: controls.append(True) or True)
        backend._explicit_turn = state == "manual"
        backend._announcement = state == "announcement"
        async def send(payload):
            sent.append(payload)
        monkeypatch.setattr(backend, "_send", send)
        name = "unknown" if state == "unknown" else "leave_voice_room"
        await backend._handle_server_msg({"toolCall": {"functionCalls": [{"id": "x", "name": name}]}})
        assert not controls
        assert sent[0]["toolResponse"]["functionResponses"][0]["response"]["status"] == "rejected"
        await backend.aclose()
    asyncio.run(run())


def test_gemini_live_tool_intent_really_says_farewell_and_leaves(tmp_path, monkeypatch):
    from voice_agent import ws_transport

    sockets = []
    class SpeakingWs(FakeLiveWs):
        async def send(self, raw):
            await super().send(raw)
            payload = json.loads(raw)
            if "text" in payload.get("realtimeInput", {}):
                await self._queue.put(json.dumps({"serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {
                        "data": base64.b64encode(b"bye").decode(), "mimeType": "audio/pcm;rate=24000",
                    }}]}, "outputTranscription": {"text": "拜拜，我下了"}, "turnComplete": True,
                }}))
    async def open_ws(*args, **kwargs):
        ws = SpeakingWs()
        sockets.append(ws)
        return ws
    monkeypatch.setattr(ws_transport, "open_live_ws", open_ws)
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.backend = "gemini_live"
        agent.settings.gemini_api_key = "test"
        agent.settings.voice_leave_enabled = True
        agent.settings.reply_probability_percent = 0
        agent.backend = create_backend(agent.settings, agent.memory)
        agent.live_mode = True
        duplex = DepartureDuplex()
        agent.duplex = duplex
        await agent.join("a", "c")
        await agent._wire_live()
        assert "tools" in sockets[0].sent[0]["setup"]
        await sockets[0]._queue.put(json.dumps({"toolCall": {"functionCalls": [
            {"name": "leave_voice_room", "id": "exit-1", "args": {}},
        ]}}))
        for _ in range(100):
            if duplex.audio:
                break
            await asyncio.sleep(.005)
        assert duplex.audio == [b"bye"]
        assert agent._bot.voice.leaves == 0
        task = agent._voice_control_task
        duplex.drained.set()
        await asyncio.wait_for(task, 1)
        assert not agent.status()["joined"] and agent._bot.voice.leaves == 1
        assert all(ws.closed for ws in sockets)
    asyncio.run(run())


def test_live_failed_departure_reconnects_and_rebinds_control_callback(tmp_path, monkeypatch):
    from voice_agent import ws_transport

    sockets = []
    async def open_ws(*args, **kwargs):
        ws = FakeLiveWs()
        sockets.append(ws)
        return ws
    monkeypatch.setattr(ws_transport, "open_live_ws", open_ws)
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.backend = "gemini_live"
        agent.settings.gemini_api_key = "test"
        agent.settings.voice_leave_enabled = True
        agent.backend = create_backend(agent.settings, agent.memory)
        agent.live_mode = True
        await agent.join("a", "c")
        async def announce(*args, **kwargs):
            return {"ok": True}
        monkeypatch.setattr(agent, "speak_announcement", announce)
        agent._bot.voice.fail_leave = True
        assert agent.request_voice_leave()
        await asyncio.wait_for(agent._voice_control_task, 1)
        assert agent.status()["joined"] and agent.backend.session_active
        assert sockets[-1].closed is False
        agent._bot.voice.fail_leave = False
        await agent.backend._handle_server_msg({"toolCall": {"functionCalls": [
            {"name": "leave_voice_room", "id": "retry", "args": {}},
        ]}})
        assert agent._voice_control_task is not None
        await asyncio.wait_for(agent._voice_control_task, 1)
        assert not agent.status()["joined"] and agent._bot.voice.leaves == 1
        assert all(ws.closed for ws in sockets)
    asyncio.run(run())
