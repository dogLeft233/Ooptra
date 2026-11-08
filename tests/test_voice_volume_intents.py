"""Strict semantic decisions and actual backend control dispatch, without model keys."""

import asyncio
import json

import pytest
from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend
from test_voice_operations import make_agent

from behavior import models as voice_control


@pytest.mark.parametrize("raw", [
    {}, [], "mute", {"action": "unknown"}, {"action": "set", "value": True},
    {"action": "set", "value": 9}, {"action": "set", "value": 101},
    {"action": "set", "value": "50"}, {"action": "set", "value": 50.0},
    {"action": "increase", "value": 10}, {"action": "mute", "value": None},
    {"action": "restore", "extra": True},
])
def test_volume_parser_rejects_invalid_commands(raw):
    with pytest.raises(ValueError):
        voice_control.parse_volume_command(raw)


@pytest.mark.parametrize("action,value", [
    ("increase", None), ("decrease", None), ("mute", None), ("restore", None),
    ("set", 10), ("set", 50), ("set", 100),
])
def test_volume_parser_accepts_only_valid_command_shapes(action, value):
    raw = {"action": action}
    if value is not None:
        raw["value"] = value
    assert voice_control.parse_volume_command(raw) == voice_control.VolumeCommand(action, value)
    assert voice_control.parse_volume_command(None) is None


@pytest.mark.parametrize("raw", [
    None, [], {}, {"leave": "false"}, {"leave": 0}, {"leave": True, "volume": {"action": "mute"}},
    {"leave": False, "volume": {"action": "set", "value": 0}},
    {"leave": False, "unexpected": True},
])
def test_control_parser_rejects_ambiguous_or_malformed_decisions(raw):
    with pytest.raises(ValueError):
        voice_control.parse_control_decision(raw)


def test_control_parser_preserves_leave_and_volume_independently():
    assert voice_control.parse_control_decision({"leave": True}) == voice_control.VoiceControlDecision(leave=True)
    assert voice_control.parse_control_decision({"leave": False, "volume": None}) == voice_control.VoiceControlDecision()
    assert voice_control.parse_control_decision({"leave": False, "volume": {"action": "mute"}}).volume.action == "mute"


@pytest.mark.parametrize("percent,leave_enabled,volume_enabled,raw,expected", [
    (100, False, True, {"action": "set", "value": 70}, "volume"),
    (0, False, True, {"action": "increase"}, "volume"),
    (0, True, False, {"action": "mute"}, None),
    (100, True, False, {"action": "restore"}, None),
])
def test_mimo_volume_intent_is_independent_of_reply_probability_and_leave_switch(
    tmp_path, monkeypatch, percent, leave_enabled, volume_enabled, raw, expected,
):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_leave_enabled = leave_enabled
        agent.settings.voice_volume_enabled = volume_enabled
        agent.settings.reply_probability_percent = percent
        agent.settings.min_utterance_ms = 0
        agent.settings.mimo_api_key = "test"
        requests = []

        class Response:
            status = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def json(self, **kwargs):
                decision = {"leave": False, "volume": raw}
                if requests[-1]["max_tokens"] != 64:
                    decision["reply"] = "好的，已调整"
                return {"choices": [{"message": {"content": json.dumps(decision)}}]}

        class Session:
            def post(self, url, **kwargs):
                requests.append(kwargs["json"])
                return Response()

        async def http():
            return Session()
        async def asr(*args):
            return "机器人，把音量调到百分之七十"
        async def tts(*args):
            pytest.fail("control decisions must never synthesize a model promise")

        monkeypatch.setattr(agent.backend, "_http", http)
        agent.backend.asr, agent.backend.tts = asr, tts
        reply = await agent.backend.handle_utterance(b"audio", sample_rate=16000)
        actual = ("leave" if reply.control.leave else "volume") if reply.control else None
        assert actual == expected
        if expected:
            assert reply.control is not None
            assert reply.control.volume == voice_control.parse_volume_command(raw)
        assert not reply.pcm16 and not reply.text
        assert len(requests) == 1
        instructions = requests[0]["messages"][0]["content"]
        for guard in ("不要闭麦", "他说", "不确定", "内部", "篡改", "10", "100"):
            assert guard in instructions
        assert "调音量会自动开麦" in instructions
        assert "不会自动开麦" not in instructions
    asyncio.run(run())


def test_mimo_strict_combined_decision_rejects_leave_plus_volume(tmp_path, monkeypatch):
    async def run():
        backend = make_agent(tmp_path).backend
        async def chat(*args, **kwargs):
            return '{"leave": true, "volume": {"action": "mute"}, "reply": "已完成"}'
        monkeypatch.setattr(backend, "chat", chat)
        with pytest.raises(ValueError):
            await backend.chat_with_intent("你闭麦")
    asyncio.run(run())


def test_live_volume_tool_executes_real_callback_and_reuses_actual_result(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = True
        agent.settings.voice_leave_enabled = False
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        state, sent = {"volume": 50}, []
        async def apply(command):
            assert command == voice_control.VolumeCommand("increase")
            state["volume"] += 10
            return {"ok": True, "volume": state["volume"], "muted": False,
                    "saved": True, "message": "已保存"}
        async def send(payload):
            sent.append(payload)
        backend.set_voice_volume_out(apply)
        monkeypatch.setattr(backend, "_send", send)
        packet = {"toolCall": {"functionCalls": [{"id": "volume-1", "name": "control_voice_output", "args": {"action": "increase"}}]}}
        await backend._handle_server_msg(packet)
        await backend._handle_server_msg(packet)
        assert state["volume"] == 60
        responses = [p["toolResponse"]["functionResponses"][0]["response"] for p in sent]
        assert responses == [{"ok": True, "volume": 60, "muted": False, "saved": True, "message": "已保存"}] * 2
        await backend._reset_session()
        await backend._handle_server_msg(packet)
        assert state["volume"] == 70
    asyncio.run(run())


@pytest.mark.parametrize("state,args", [
    ("disabled", {"action": "mute"}), ("manual", {"action": "mute"}),
    ("announcement", {"action": "restore"}), ("missing_callback", {"action": "mute"}),
    ("invalid", {"action": "set", "value": 101}), ("invalid", {"action": "set", "value": True}),
    ("invalid", {"action": "mute", "value": 10}), ("invalid", None),
    ("invalid", {"action": "set", "value": "50"}),
    ("invalid", {"action": "set", "value": 50.0}),
    ("invalid", {"action": "set", "value": 0}),
    ("invalid", {"action": "unknown"}), ("invalid", {}),
])
def test_live_volume_tool_rejects_invalid_or_internal_control_without_execution(tmp_path, monkeypatch, state, args):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = state != "disabled"
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        sent = []
        async def apply(raw):
            pytest.fail("invalid or internal control cannot execute")
        async def send(payload):
            sent.append(payload)
        if state != "missing_callback":
            backend.set_voice_volume_out(apply)
        backend._explicit_turn = state == "manual"
        backend._announcement = state == "announcement"
        monkeypatch.setattr(backend, "_send", send)
        await backend._handle_server_msg({"toolCall": {"functionCalls": [{"id": "x", "name": "control_voice_output", "args": args}]}})
        result = sent[0]["toolResponse"]["functionResponses"][0]["response"]
        assert result["ok"] is False and result["error"]
    asyncio.run(run())


def test_live_volume_tool_callback_failure_never_claims_success(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = True
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        sent = []
        async def apply(raw):
            raise RuntimeError("disk unavailable")
        async def send(payload):
            sent.append(payload)
        backend.set_voice_volume_out(apply)
        monkeypatch.setattr(backend, "_send", send)
        await backend._handle_server_msg({"toolCall": {"functionCalls": [{"id": "x", "name": "control_voice_output", "args": {"action": "decrease"}}]}})
        result = sent[0]["toolResponse"]["functionResponses"][0]["response"]
        assert result["ok"] is False and result["error"]
    asyncio.run(run())


def test_live_setup_advertises_volume_without_departure_tool(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_leave_enabled = False
        agent.settings.voice_volume_enabled = True
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        sent = []
        async def send(payload):
            sent.append(payload)
        monkeypatch.setattr(backend, "_send", send)
        await backend._send_setup()
        declarations = sent[0]["setup"]["tools"][0]["function_declarations"]
        assert [tool["name"] for tool in declarations] == ["control_voice_output"]
        params = declarations[0]["parameters"]
        assert params["required"] == ["action"]
        assert params["properties"]["value"]["type"] == "INTEGER"
    asyncio.run(run())


def test_live_concurrent_duplicate_calls_apply_volume_once(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = True
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        state = {"volume": 50}
        began, complete = asyncio.Event(), asyncio.Event()
        async def apply(raw):
            began.set()
            await complete.wait()
            state["volume"] += 10
            return {"ok": True, "volume": state["volume"]}
        async def send(payload):
            pass
        backend.set_voice_volume_out(apply)
        monkeypatch.setattr(backend, "_send", send)
        packet = {"toolCall": {"functionCalls": [{"id": "same", "name": "control_voice_output", "args": {"action": "increase"}}]}}
        first = asyncio.create_task(backend._handle_server_msg(packet))
        await began.wait()
        second = asyncio.create_task(backend._handle_server_msg(packet))
        await asyncio.sleep(0)
        complete.set()
        await asyncio.gather(first, second)
        assert state["volume"] == 60
    asyncio.run(run())


def test_live_volume_tool_preserves_failed_application_result(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = True
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        sent = []
        failure = {"ok": False, "volume": 50, "muted": True, "saved": False, "error": "保存失败"}
        async def apply(raw):
            return failure
        async def send(payload):
            sent.append(payload)
        backend.set_voice_volume_out(apply)
        monkeypatch.setattr(backend, "_send", send)
        await backend._handle_server_msg({"toolCall": {"functionCalls": [{"id": "failed", "name": "control_voice_output", "args": {"action": "set", "value": 70}}]}})
        assert sent[0]["toolResponse"]["functionResponses"][0]["response"] == failure
    asyncio.run(run())


@pytest.mark.parametrize("invalid_result", [None, True, {}, {"ok": "true"}])
def test_live_volume_tool_does_not_coerce_bad_application_results_to_success(tmp_path, monkeypatch, invalid_result):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = True
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        sent = []
        async def apply(raw):
            return invalid_result
        async def send(payload):
            sent.append(payload)
        backend.set_voice_volume_out(apply)
        monkeypatch.setattr(backend, "_send", send)
        await backend._handle_server_msg({"toolCall": {"functionCalls": [{"id": "failed", "name": "control_voice_output", "args": {"action": "mute"}}]}})
        assert sent[0]["toolResponse"]["functionResponses"][0]["response"]["ok"] is False
    asyncio.run(run())


def test_live_control_cache_remains_bounded(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        for index in range(33):
            await backend._control_call_result({"id": str(index), "name": "unknown"})
        assert len(backend._control_results) == 32
        assert "0" not in backend._control_results and "32" in backend._control_results
    asyncio.run(run())


def test_live_mixed_leave_and_volume_packet_rejects_both_without_execution(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_leave_enabled = True
        agent.settings.voice_volume_enabled = True
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        state, sent = {"volume": 50, "left": False}, []
        def leave():
            state["left"] = True
            return True
        async def apply(raw):
            state["volume"] += 10
            return {"ok": True, "volume": state["volume"]}
        async def send(payload):
            sent.append(payload)
        backend.set_voice_leave_out(leave)
        backend.set_voice_volume_out(apply)
        monkeypatch.setattr(backend, "_send", send)
        packet = {"toolCall": {"functionCalls": [
            {"id": "leave-mixed", "name": "leave_voice_room", "args": {}},
            {"id": "volume-mixed", "name": "control_voice_output", "args": {"action": "increase"}},
        ]}}
        await backend._handle_server_msg(packet)
        # Replaying one part of the rejected packet cannot turn ambiguity into an operation.
        await backend._handle_server_msg({"toolCall": {"functionCalls": [packet["toolCall"]["functionCalls"][1]]}})
        assert state == {"volume": 50, "left": False}
        responses = sent[0]["toolResponse"]["functionResponses"]
        assert responses[0]["response"]["status"] == "rejected"
        assert responses[1]["response"]["ok"] is False
        assert responses[1]["response"]["error"]
        assert sent[1]["toolResponse"]["functionResponses"][0]["response"] == responses[1]["response"]
        await backend._handle_server_msg({"toolCall": {"functionCalls": [
            {"id": "volume-clear", "name": "control_voice_output", "args": {"action": "increase"}},
        ]}})
        assert state == {"volume": 60, "left": False}
    asyncio.run(run())


def test_live_mixed_packet_preserves_prior_duplicate_id_result_without_new_operations(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_leave_enabled = True
        agent.settings.voice_volume_enabled = True
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        state, sent = {"volume": 50}, []
        def leave():
            pytest.fail("new leave from mixed packet must not execute")
        async def apply(raw):
            state["volume"] += 10
            return {"ok": True, "volume": state["volume"]}
        async def send(payload):
            sent.append(payload)
        backend.set_voice_leave_out(leave)
        backend.set_voice_volume_out(apply)
        monkeypatch.setattr(backend, "_send", send)
        volume = {"id": "volume-done", "name": "control_voice_output", "args": {"action": "increase"}}
        await backend._handle_server_msg({"toolCall": {"functionCalls": [volume]}})
        await backend._handle_server_msg({"toolCall": {"functionCalls": [
            volume, {"id": "leave-new", "name": "leave_voice_room", "args": {}},
        ]}})
        assert state["volume"] == 60
        responses = sent[-1]["toolResponse"]["functionResponses"]
        assert responses[0]["response"] == {"ok": True, "volume": 60}
        assert responses[1]["response"]["status"] == "rejected"
    asyncio.run(run())
