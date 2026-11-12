"""真实Live输出/抢话生命周期，以及供应商外部协议配置与诊断。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend
from support.live_runtime import audio_message, make_live_agent, settle

from dialogue import adapt_websocket as _AiohttpWs
from operations.models import VoiceAgentSettings

FRAME = b"\x00\x01" * 320
CHUNKS = 13


def test_interleaved_chunks_trigger_barge_in_once(tmp_path):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path, barge_in=True, barge_in_hold_ms=0)
        try:
            for _ in range(CHUNKS):
                await sockets[0].deliver(audio_message())
                await playback.capture("u1", FRAME)
            assert playback.stops == 1
            assert len([event for event in sockets[0].sent if "audio" in event.get("realtimeInput", {})]) == CHUNKS
        finally:
            await agent.leave()
    asyncio.run(run())


def test_speaking_is_false_after_playback_completion(tmp_path):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path, barge_in=True, barge_in_hold_ms=0)
        try:
            await sockets[0].deliver(audio_message(text="回答"))
            assert playback.chunks
            await sockets[0].deliver({"serverContent": {"turnComplete": True}})
            assert await agent.wait_for_reply_end(timeout=1)
            assert agent.status()["speaking"] is False
            for _ in range(50):
                await playback.capture("u1", FRAME)
            assert playback.stops == 0
            assert len([event for event in sockets[0].sent if "audio" in event.get("realtimeInput", {})]) == 50
        finally:
            await agent.leave()
    asyncio.run(run())


def test_turns_increments_once_per_reply(tmp_path):
    async def run():
        agent, _, sockets = await make_live_agent(tmp_path)
        try:
            for _ in range(CHUNKS):
                await sockets[0].deliver(audio_message(text="答"))
            assert agent.turns == 0
            await sockets[0].deliver({"serverContent": {"turnComplete": True}})
            assert await agent.wait_for_reply_end(timeout=1)
            assert agent.turns == 1
        finally:
            await agent.leave()
    asyncio.run(run())


def test_live_speak_text_counts_the_turn_at_the_boundary(tmp_path):
    async def run():
        agent, _, sockets = await make_live_agent(tmp_path)
        try:
            result = await agent.speak_text("你好")
            assert result["mode"] == "live"
            assert agent.turns == 0
            assert [message["realtimeInput"]["text"] for message in sockets[0].sent if "text" in message.get("realtimeInput", {})] == ["你好"]
            await sockets[0].deliver(audio_message(text="你好", done=True))
            assert await agent.wait_for_reply_end(timeout=1)
            assert agent.turns == 1
        finally:
            await agent.leave()
    asyncio.run(run())


def test_barge_in_rearms_for_the_next_turn(tmp_path):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path, barge_in=True, barge_in_hold_ms=0)
        try:
            for expected in (1, 2):
                await sockets[0].deliver(audio_message())
                await playback.capture("u1", FRAME)
                assert playback.stops == expected
                await sockets[0].deliver({"serverContent": {"turnComplete": True}})
        finally:
            await agent.leave()
    asyncio.run(run())


def test_server_side_interrupted_clears_local_playback(tmp_path):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path)
        try:
            await sockets[0].deliver(audio_message())
            assert playback.chunks
            await sockets[0].deliver({"serverContent": {"interrupted": True}})
            assert await agent.wait_for_reply_end(timeout=1)
            assert playback.stops == 1
            assert agent.status()["speaking"] is False
        finally:
            await agent.leave()
    asyncio.run(run())


async def _open(backend):
    from channels.models import RoomTarget, SessionRef
    from context.models import ContextRef, ContextRevision, PreparedContext
    from dialogue.models import LiveSessionRequest
    session = SessionRef("run", "binding", "session", RoomTarget("area", "room"))
    prepared = PreparedContext(ContextRef(session, 0, "local"), ContextRevision(), backend.settings.persona, ())
    return await backend.open_live(LiveSessionRequest(session, prepared))


class FakeLiveWs:
    """够用的假 Live socket：收到 setup 就回 setupComplete。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, raw: str) -> None:
        if self.closed:
            raise RuntimeError("Cannot write to closing transport")
        msg = json.loads(raw)
        self.sent.append(msg)
        if "setup" in msg:
            await self._queue.put(json.dumps({"setupComplete": {}}))

    def __aiter__(self) -> FakeLiveWs:
        return self

    async def __anext__(self) -> str:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        await self._queue.put(None)


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[FakeLiveWs]:
    created: list[FakeLiveWs] = []

    async def fake_open(url: str, *, proxy: str | None = None, **kwargs: Any) -> FakeLiveWs:
        # 这个 await 是**用例有效性**的前提：没有它，start_session 的临界区里
        # 一个真正的挂起点都没有，5 个协程会被逐个同步跑完，锁与复检根本没被走到 ——
        # 修锁之前这个用例也是绿的（空转）。见 test_concurrent_start_session_connects_once。
        await asyncio.sleep(0)
        ws = FakeLiveWs()
        created.append(ws)
        return ws

    from support import dialogue_fixtures as transport

    monkeypatch.setattr(transport, "live_connector", fake_open)
    return created


def _backend(persona: str = "测试"):
    return GeminiLiveBackend(
        VoiceAgentSettings(
            enabled=True,
            gemini_api_key="test-key",
            gemini_model="gemini-test",
            persona=persona,
            proxy="",
        ),
    )


def test_concurrent_start_session_connects_once(opened: list[FakeLiveWs]) -> None:
    """并发 start_session（push_audio 在热路径上也会调）只真正建连一次。"""

    async def run() -> None:
        backend = _backend()
        results = await asyncio.gather(*(_open(backend) for _ in range(5)), return_exceptions=True)
        assert sum(not isinstance(result, BaseException) for result in results) == 1
        assert len(opened) == 1, f"并发建连了 {len(opened)} 次会话"
        assert backend.session_active is True
        await backend.aclose()

    asyncio.run(run())


# ----------------------------------------------------------------------
# 9：抢话不再向服务端发 audioStreamEnd
# ----------------------------------------------------------------------


def test_interrupt_sends_no_server_signal(opened: list[FakeLiveWs]) -> None:
    """``audioStreamEnd`` 的语义是「用户说完了，立刻回答」，拿它抢话说反了。

    它会让模型把缓冲里的音频**碎片**当成一个完整回合去回答 —— 一整段没有内容的
    输入，模型只能瞎猜（语言乱切换就是从这里开始的）。抢话由服务端自动 VAD 判定，
    客户端只需要停掉本地播放。
    """

    async def run() -> None:
        backend = _backend()
        live = await _open(backend)
        ws = opened[0]
        await live.interrupt()

        assert not [
            m for m in ws.sent if "audioStreamEnd" in json.dumps(m)
        ], "抢话又向服务端发了 audioStreamEnd"
        assert backend.session_active is True, "interrupt 不该把会话搞掉"
        await backend.aclose()

    asyncio.run(run())


# ----------------------------------------------------------------------
# 10：语言约束必须跟着人格一起下发
# ----------------------------------------------------------------------


def test_setup_appends_the_language_guard(opened: list[FakeLiveWs]) -> None:
    """Live 原生音频模型不支持 speech_config.language_code 锁语言，只能靠提示词。"""

    async def run() -> None:
        backend = _backend("用中文回答。你是测试人格。")
        await _open(backend)
        text = opened[0].sent[0]["setup"]["system_instruction"]["parts"][0]["text"]

        assert "用中文回答。你是测试人格。" in text, "人格本身不能被吞掉"
        assert "你一律只用中文普通话回答" in text, "语言约束没有下发"
        assert text.index("你一律只用中文普通话回答") > text.index("测试人格"), (
            "语言约束必须追加在人格之后（收尾位置权重更高）"
        )
        await backend.aclose()

    asyncio.run(run())


def test_language_guard_survives_an_empty_persona(opened: list[FakeLiveWs]) -> None:
    """人格是用户可清空的配置项；清空后语言约束仍要在。"""

    async def run() -> None:
        backend = _backend("")
        await _open(backend)
        text = opened[0].sent[0]["setup"]["system_instruction"]["parts"][0]["text"]
        assert "你一律只用中文普通话回答" in text
        await backend.aclose()

    asyncio.run(run())


def test_setup_disables_server_side_barge_in(opened: list[FakeLiveWs]) -> None:
    """setup 必须关掉服务端的自动抢话，否则房间一热闹 bot 就说不完整句话。

    实测（2026-09-30）：灌进去的是整个房间的混流，服务端只要认出「有人在说话」
    就取消生成 —— 投喂真语音从 0.02（远处背景人声）到 0.15（贴麦）**任何音量都会
    打断**。降灵敏度救不了：``start_of_speech_sensitivity`` 调到 LOW 后六个音量档
    的打断次数一模一样，因为背景人声本来就是真语音。

    这条用例同时锁死**拼写**：服务端**只认 snake_case**，写成
    ``realtimeInputConfig`` 之类的 camelCase 会让整个连接以 close_code=1007 断开
    —— 不是「配置没生效」，是 bot 根本连不上。同理 ``START_OF_SPEECH_SENSITIVITY_LOW``
    这种想当然的枚举名也是 1007（正确的是 ``START_SENSITIVITY_LOW``）。
    """

    async def run() -> None:
        backend = _backend()
        await _open(backend)
        ric = opened[0].sent[0]["setup"]["realtime_input_config"]

        assert ric["activity_handling"] == "NO_INTERRUPTION", "服务端仍会自动抢话"
        assert "automatic_activity_detection" in ric, "snake_case，别改成 camelCase"
        assert "prefix_padding_ms" in ric["automatic_activity_detection"]
        assert "silence_duration_ms" in ric["automatic_activity_detection"]
        await backend.aclose()

    asyncio.run(run())


# ----------------------------------------------------------------------
# 8：断线原因不再被吞掉
# ----------------------------------------------------------------------


class _FakeMsg:
    def __init__(self, type_: Any, data: Any = "") -> None:
        self.type = type_
        self.data = data


class _FakeAiohttpWs:
    def __init__(self, msgs: list[_FakeMsg]) -> None:
        self._msgs = msgs
        self.close_code = 1000

    def __aiter__(self) -> Any:
        return self._iter()

    async def _iter(self) -> Any:
        for msg in self._msgs:
            yield msg

    def exception(self) -> Exception:
        return RuntimeError("connection reset by peer")

    async def close(self) -> None:
        pass


def test_aiohttp_close_logs_reason(caplog: pytest.LogCaptureFixture) -> None:
    """CLOSE 时既要把已收到的数据放出去，也要留下 close_code 与异常。"""

    async def run() -> None:
        import aiohttp

        inner = _FakeAiohttpWs(
            [
                _FakeMsg(aiohttp.WSMsgType.TEXT, "hello"),
                _FakeMsg(aiohttp.WSMsgType.CLOSE, "server said bye"),
            ]
        )
        adapted = _AiohttpWs(inner, None)
        with caplog.at_level("INFO", logger="dialogue._internal.ws_transport"):
            got = [chunk async for chunk in adapted]

        assert got == ["hello"], "关闭前收到的数据不能丢"
        assert "close_code=1000" in caplog.text, "断线日志里没有 close_code"
        assert "exception_type=RuntimeError" in caplog.text
        assert "connection reset by peer" not in caplog.text
        assert "server said bye" not in caplog.text

    asyncio.run(run())


# ----------------------------------------------------------------------
# 服务端消息日志：留痕要留对的，别把断线线索埋进心跳里
# ----------------------------------------------------------------------


def test_session_lifecycle_messages_are_logged(caplog: pytest.LogCaptureFixture, opened) -> None:
    """goAway / sessionResumptionUpdate 必须留痕。

    2026-10-01 实测：会话每 22~40 秒静默断一次，断开时**既没有 ws 关闭日志、
    也没有异常**，只留一句 session ended —— 唯一的证据就是这两类消息，它们走的
    正是 ``serverContent`` 为空的那条分支。静默 return 会让它变成无头案。
    """

    async def run() -> None:
        backend = _backend()
        await _open(backend)
        with caplog.at_level("INFO", logger="dialogue._internal.live.gemini_live"):
            await opened[0]._queue.put(json.dumps({"sessionResumptionUpdate": {"newHandle": "h1", "resumable": True}}))
            await opened[0]._queue.put(json.dumps({"goAway": {"timeLeft": "10s"}}))
            await settle()
        assert "sessionResumptionUpdate" in caplog.text, "续接句柄没留痕"
        assert "goAway" in caplog.text, "服务端主动断开没留痕"
        await backend.aclose()

    asyncio.run(run())


def test_heartbeat_messages_do_not_flood_the_log(caplog: pytest.LogCaptureFixture, opened) -> None:
    """空包 / 空 serverContent / voiceActivity 都是心跳，一条都不该记。

    实测 ``voiceActivity`` 一项就刷掉约 100 行/分钟（每句话 START + END），
    把上面那两类真正要看的消息埋掉了。
    """

    async def run() -> None:
        backend = _backend()
        await _open(backend)
        with caplog.at_level("INFO", logger="dialogue._internal.live.gemini_live"):
            for message in ({}, {"serverContent": {}}, {"voiceActivity": {"type": "ACTIVITY_START"}}, {"voiceActivity": {"type": "ACTIVITY_END"}}):
                await opened[0]._queue.put(json.dumps(message))
            await settle()
        assert "dialogue.live.unhandled" not in caplog.text, f"心跳消息进了日志：{caplog.text}"
        await backend.aclose()

    asyncio.run(run())
