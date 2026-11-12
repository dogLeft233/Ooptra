"""Gemini Live 公开会话生命周期：断线可查询，原绑定失效，显式重建。

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


def _request():
    from channels.models import RoomTarget, SessionRef
    from context.models import ContextRef, ContextRevision, PreparedContext
    from dialogue.models import LiveSessionRequest

    session = SessionRef("run", "binding", "session", RoomTarget("area", "room"))
    prepared = PreparedContext(ContextRef(session, 0, "local"), ContextRevision(), "测试", ())
    return LiveSessionRequest(session, prepared)


def test_session_reports_active_after_start(opened):
    async def run():
        backend = GeminiLiveBackend(_settings())
        live = await backend.open_live(_request())
        assert live.snapshot().state == "ready"
        setup = opened[0].sent[0]["setup"]
        assert setup["model"] == "models/gemini-test"
        assert setup["generation_config"]["speech_config"]["voice_config"]["prebuilt_voice_config"]["voice_name"] == "Kore"
        await live.aclose()
    asyncio.run(run())


def test_server_drop_is_queryable_and_stale_input_is_rejected(opened):
    from dialogue.models import AudioInput
    async def run():
        backend = GeminiLiveBackend(_settings())
        request = _request()
        live = await backend.open_live(request)
        await opened[0].drop()
        await _settle()
        assert live.snapshot().state == "failed"
        result = await live.send_input(AudioInput("i", request.session, b"\0\1" * 160, 16000))
        assert result.status == "unavailable"
        await live.aclose()
    asyncio.run(run())


def test_open_new_binding_after_drop_reconnects_once(opened):
    async def run():
        backend = GeminiLiveBackend(_settings())
        first = await backend.open_live(_request())
        await opened[0].drop()
        await _settle()
        second = await backend.open_live(_request())
        assert first.snapshot().state in {"failed", "closed"}
        assert second.snapshot().state == "ready"
        assert second.live_session_id != first.live_session_id
        assert len(opened) == 2
        await second.aclose()
    asyncio.run(run())


def test_failed_binding_never_reconnects_for_each_audio_frame(opened):
    from dialogue.models import AudioInput
    async def run():
        backend = GeminiLiveBackend(_settings())
        request = _request()
        live = await backend.open_live(request)
        await opened[0].drop()
        await _settle()
        for index in range(10):
            assert (await live.send_input(AudioInput(str(index), request.session, b"\0\1" * 160, 16000))).status == "unavailable"
        assert len(opened) == 1
        await live.aclose()
    asyncio.run(run())


def test_open_without_key_raises_readable_error(opened):
    async def run():
        backend = GeminiLiveBackend(_settings(gemini_api_key=""))
        with pytest.raises(RuntimeError, match=r"gemini\.api_key"):
            await backend.open_live(_request())
        assert not opened
    asyncio.run(run())


def test_explicit_close_closes_transport_and_rejects_future_input(opened):
    from dialogue.models import AudioInput
    async def run():
        backend = GeminiLiveBackend(_settings())
        request = _request()
        live = await backend.open_live(request)
        await live.aclose()
        assert live.snapshot().state == "closed"
        assert opened[0].closed
        assert (await live.send_input(AudioInput("i", request.session, b"\0\1" * 160, 16000))).status == "unavailable"
        await _settle()
        assert len(opened) == 1
    asyncio.run(run())


def test_unknown_voice_falls_back_to_default(opened):
    async def run():
        backend = GeminiLiveBackend(_settings(gemini_voice=""))
        live = await backend.open_live(_request())
        setup = opened[0].sent[0]["setup"]
        assert setup["generation_config"]["speech_config"]["voice_config"]["prebuilt_voice_config"]["voice_name"] == "Puck"
        await live.aclose()
    asyncio.run(run())
