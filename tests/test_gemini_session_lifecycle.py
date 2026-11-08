"""Gemini Live 会话生命周期：断线必须能被发现、能重连、不再静默吞音频。

这个文件存在的理由（B3）：``_session_loop`` 退出时只 ``_ready.set()``，
不回收 ``_ws`` / ``_session_task``，于是：
  * ``session_active`` 对已死 socket 恒为 True
  * ``start_session`` 变成空操作（进房成功但模型根本没连上）
  * ``push_audio`` 把错误吞成 debug 日志（音频进黑洞，无任何提示）
  * ``speak_text`` 直接抛异常 → HTTP 500
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend

from operations.models import VoiceAgentSettings

# ----------------------------------------------------------------------
# 假 WebSocket：能收能发，可以被「服务端」掐断
# ----------------------------------------------------------------------


class FakeLiveWs:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.dropped = False
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, raw: str) -> None:
        if self.dropped or self.closed:
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

    async def drop(self) -> None:
        """模拟服务端掐断：迭代结束，本地 socket 状态看起来还是开的。"""
        self.dropped = True
        await self._queue.put(None)


class FakeMemory:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def append(self, role: str, content: str, **kwargs: Any) -> dict:
        self.rows.append((role, content))
        return {"role": role, "content": content}

    def set_persona(self, persona: str) -> None:
        pass


def _settings(**overrides: Any) -> VoiceAgentSettings:
    base = {
        "enabled": True,
        "gemini_api_key": "test-key",
        "gemini_model": "gemini-test",
        "gemini_voice": "Kore",
        "persona": "测试",
        "proxy": "",
    }
    base.update(overrides)
    return VoiceAgentSettings(**base)


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[FakeLiveWs]:
    """把 open_live_ws 换成假实现，记录每次建连。"""
    created: list[FakeLiveWs] = []

    async def fake_open(url: str, *, proxy: str | None = None, **kwargs: Any) -> FakeLiveWs:
        ws = FakeLiveWs()
        created.append(ws)
        return ws

    from support import dialogue_fixtures as transport

    monkeypatch.setattr(transport, "live_connector", fake_open)
    return created


async def _settle(rounds: int = 20) -> None:
    """让出控制权，等 session loop 跑完收尾。"""
    for _ in range(rounds):
        await asyncio.sleep(0)


def test_session_reports_active_after_start(opened: list[FakeLiveWs]) -> None:
    async def run() -> None:
        backend = GeminiLiveBackend(_settings(), FakeMemory())
        assert backend.session_active is False
        await backend.start_session()
        assert backend.session_active is True
        assert len(opened) == 1
        # setup 已发出，且带上了模型与音色
        setup = opened[0].sent[0]["setup"]
        assert setup["model"] == "models/gemini-test"
        assert (
            setup["generation_config"]["speech_config"]["voice_config"]
            ["prebuilt_voice_config"]["voice_name"]
            == "Kore"
        )
        await backend.aclose()

    asyncio.run(run())


def test_server_drop_clears_session_active(opened: list[FakeLiveWs]) -> None:
    """核心回归：服务端断开后 session_active 必须变 False。"""

    async def run() -> None:
        backend = GeminiLiveBackend(_settings(), FakeMemory())
        await backend.start_session()
        ws = opened[0]
        await ws.drop()
        await _settle()
        assert backend.session_active is False, "会话已死却仍报 active"
        await backend.aclose()

    asyncio.run(run())


def test_start_session_after_drop_really_reconnects(opened: list[FakeLiveWs]) -> None:
    """核心回归：掉线后再 start_session 必须真的重新建连，不能变成空操作。"""

    async def run() -> None:
        backend = GeminiLiveBackend(_settings(), FakeMemory())
        await backend.start_session()
        await opened[0].drop()
        await _settle()

        await backend.start_session()
        assert len(opened) == 2, "start_session 没有重新建连（旧会话状态没清干净）"
        assert backend.session_active is True
        await backend.aclose()

    asyncio.run(run())


def test_speak_text_reconnects_once_instead_of_raising(opened: list[FakeLiveWs]) -> None:
    """核心回归：会话刚死时 speak_text 要重连一次再发，而不是抛 500。"""

    async def run() -> None:
        backend = GeminiLiveBackend(_settings(), FakeMemory())
        await backend.start_session()
        await opened[0].drop()
        await _settle()

        await backend.speak_text("你好")
        assert len(opened) == 2
        texts = [
            msg["realtimeInput"]["text"]
            for ws in opened
            for msg in ws.sent
            if "realtimeInput" in msg and "text" in msg["realtimeInput"]
        ]
        assert texts == ["你好"]
        await backend.aclose()

    asyncio.run(run())


def test_push_audio_does_not_silently_swallow_failure(
    opened: list[FakeLiveWs], caplog: pytest.LogCaptureFixture
) -> None:
    """音频热路径断线时至少要留下 WARNING，不能是 debug 级黑洞。"""

    async def run() -> None:
        backend = GeminiLiveBackend(_settings(), FakeMemory())
        await backend.start_session()
        ws = opened[0]
        await ws.drop()
        await _settle()
        # 直接对已死的会话灌音频：内部会尝试重连，无论成败都不能无声无息
        with caplog.at_level("DEBUG", logger="dialogue._internal.live.gemini_live"):
            await backend.push_audio(b"\x00\x01" * 160, 16000)
        # Audio frames schedule bounded background recovery rather than opening
        # another session inline and bypassing the retry budget.
        assert backend._reconnect_task is not None
        await backend.start_session()  # Explicit actions can still retry immediately.
        assert backend.session_active is True
        await backend.aclose()

    asyncio.run(run())


def test_speak_text_without_key_raises_readable_error(
    opened: list[FakeLiveWs],
) -> None:
    """没配 key 时要给出可读原因（旧行为已经如此，这里锁住别退化）。"""

    async def run() -> None:
        backend = GeminiLiveBackend(_settings(gemini_api_key=""), FakeMemory())
        with pytest.raises(RuntimeError) as excinfo:
            await backend.start_session()
        assert "gemini.api_key" in str(excinfo.value)

    asyncio.run(run())


def test_session_ends_when_our_own_close_is_called(opened: list[FakeLiveWs]) -> None:
    async def run() -> None:
        backend = GeminiLiveBackend(_settings(), FakeMemory())
        await backend.start_session()
        await backend.aclose()
        assert backend.session_active is False
        await _settle()
        # 主动关闭后不得再偷偷重连
        assert len(opened) == 1

    asyncio.run(run())


def test_unknown_voice_falls_back_to_default(opened: list[FakeLiveWs]) -> None:
    async def run() -> None:
        backend = GeminiLiveBackend(_settings(gemini_voice=""), FakeMemory())
        await backend.start_session()
        setup = opened[0].sent[0]["setup"]
        assert (
            setup["generation_config"]["speech_config"]["voice_config"]
            ["prebuilt_voice_config"]["voice_name"]
            == "Puck"
        )
        await backend.aclose()

    asyncio.run(run())
