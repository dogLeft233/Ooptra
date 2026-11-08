"""成员实时静音状态：接收（Agora stream message）与合并进成员列表。

背景：Oopz 的 REST（membersByChannels）与网关事件都不带麦克风/扬声器状态，
成员页这两列长期只能显示「未知」（曾被当成 bug 报回来）。唯一来源是各客户端
用 Agora sendStreamMessage 广播的 ``{m, uid, cid, hm}``，而播放器此前只发不收。

这个文件锁两件事：
  1. 收到合法广播要记下来，收到垃圾一律丢弃（不猜、不伪造）
  2. 成员列表里实时状态优先，没有实时状态时不能凭空变成「开麦」

真机是否广播由 ``voice_state_received`` 计数暴露，0 表示数据源没在发 ——
界面应继续显示「未知」，而不是把它当成「都没闭麦」。
"""

from __future__ import annotations

from typing import Any

import pytest

from channels import create_oopz_directory
from oopz_sdk.transport.voice_browser import BrowserVoiceTransport
from webui.voice_routes import _live_muted, normalize_member


class _Cfg:
    """BrowserVoiceTransport 只用到 config 的属性，构造时不需要真浏览器。"""

    person_uid = "bot-uid"


@pytest.fixture
def transport() -> BrowserVoiceTransport:
    return BrowserVoiceTransport(_Cfg())  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# 接收侧：_remember_voice_state
# ----------------------------------------------------------------------


def test_valid_broadcast_is_remembered(transport: BrowserVoiceTransport) -> None:
    assert transport._remember_voice_state("u1", '{"m":1,"hm":0,"cid":374,"ts":1727000000000}') is True

    states = transport.voice_states()
    assert states["u1"]["m"] == 1
    assert states["u1"]["hm"] == 0
    assert states["u1"]["cid"] == 374
    assert states["u1"]["ts"] == pytest.approx(1727000000.0)
    assert transport.voice_state_received == 1


def test_broadcast_without_ts_uses_now(transport: BrowserVoiceTransport) -> None:
    assert transport._remember_voice_state("u1", {"m":0,"hm":0}) is True
    assert transport.voice_states()["u1"]["ts"] > 0


def test_uid_is_trimmed_and_stringified(transport: BrowserVoiceTransport) -> None:
    transport._remember_voice_state("  u1  ", {"m": 0})
    assert "u1" in transport.voice_states()


def test_latest_broadcast_wins(transport: BrowserVoiceTransport) -> None:
    transport._remember_voice_state("u1", {"m": 0, "hm": 0})
    transport._remember_voice_state("u1", {"m": 1, "hm": 1})
    states = transport.voice_states()
    assert len(states) == 1
    assert (states["u1"]["m"], states["u1"]["hm"]) == (1, 1)


@pytest.mark.parametrize(
    "payload",
    [
        "",                     # 空串
        "not-json",             # 不是 JSON
        "[1,2,3]",              # JSON 但不是对象
        '{"cid":374}',          # 没有 m/hm
        '{"m":null,"hm":null}', # 显式 null
        '{"m":"x","hm":"y"}',   # 值非法
        None,                   # 非字符串非 dict
        12345,
    ],
)
def test_garbage_is_dropped(transport: BrowserVoiceTransport, payload: Any) -> None:
    assert transport._remember_voice_state("u1", payload) is False
    assert transport.voice_states() == {}
    assert transport.voice_state_received == 0


@pytest.mark.parametrize("uid", ["", "   ", None])
def test_missing_uid_is_dropped(transport: BrowserVoiceTransport, uid: Any) -> None:
    assert transport._remember_voice_state(uid, {"m": 0}) is False
    assert transport.voice_state_received == 0


def test_partial_flags_kept_as_none(transport: BrowserVoiceTransport) -> None:
    """只有 m 的消息也要收下：扬声器保持未知，不能补成 0。"""
    transport._remember_voice_state("u1", {"m": 1})
    row = transport.voice_states()["u1"]
    assert row["m"] == 1
    assert row["hm"] is None


def test_zero_and_one_are_not_confused(transport: BrowserVoiceTransport) -> None:
    transport._remember_voice_state("a", {"m": 1, "hm": 0})
    transport._remember_voice_state("b", {"m": 0, "hm": 1})
    states = transport.voice_states()
    assert (states["a"]["m"], states["a"]["hm"]) == (1, 0)
    assert (states["b"]["m"], states["b"]["hm"]) == (0, 1)


def test_snapshot_is_a_copy(transport: BrowserVoiceTransport) -> None:
    transport._remember_voice_state("u1", {"m": 1})
    transport.voice_states()["u1"]["m"] = 0
    assert transport.voice_states()["u1"]["m"] == 1


# ----------------------------------------------------------------------
# 合并侧：normalize_member
# ----------------------------------------------------------------------


def test_live_state_fills_unknown_columns() -> None:
    row = normalize_member({"uid": "u1", "name": "小明"}, live={"m": 1, "hm": 0})
    assert row["mic_muted"] is True
    assert row["speaker_muted"] is False
    assert row["m"] == 1 and row["hm"] == 0
    assert row["mic"] is False and row["speaker"] is True  # true=开
    assert row["live"] is True


def test_no_live_state_stays_unknown() -> None:
    row = normalize_member({"uid": "u1", "name": "小明"})
    assert row["mic_muted"] is None
    assert row["speaker_muted"] is None
    assert row["live"] is False
    # 关键：不能把「不知道」渲染成「开麦」
    assert row["mic"] is None and row["speaker"] is None


def test_live_state_wins_over_row_fields() -> None:
    row = normalize_member(
        {"uid": "u1", "mic_muted": False, "speaker_muted": False},
        live={"m": 1, "hm": 1},
    )
    assert row["mic_muted"] is True
    assert row["speaker_muted"] is True


def test_partial_live_state_keeps_row_fallback() -> None:
    """实时消息只带 m 时，扬声器仍可用 REST 字段兜底。"""
    row = normalize_member(
        {"uid": "u1", "speaker_muted": True},
        live={"m": 0},
    )
    assert row["mic_muted"] is False  # 来自实时
    assert row["speaker_muted"] is True  # 来自 REST
    assert row["live"] is True


def test_row_fields_still_work_without_live() -> None:
    row = normalize_member({"uid": "u1", "mic": True, "speaker": False})
    assert row["mic_muted"] is False
    assert row["speaker_muted"] is True
    assert row["live"] is False


@pytest.mark.parametrize("bad", ["x", {}, None, []])
def test_live_muted_tolerates_junk(bad: Any) -> None:
    assert _live_muted(bad, "m") is None
    assert _live_muted({"m": "x"}, "m") is None
    assert _live_muted({"m": 1}, "m") is True
    assert _live_muted({"m": 0}, "m") is False


# ----------------------------------------------------------------------
# 取用侧：_voice_live_states（任何异常都不能让成员页 500）
# ----------------------------------------------------------------------


class _Voice:
    def __init__(self, states: Any, received: Any = 0) -> None:
        self._states = states
        self._received = received

    def voice_states(self) -> Any:
        return self._states

    @property
    def voice_state_received(self) -> Any:
        return self._received


class _Bot:
    def __init__(self, voice: Any) -> None:
        self.voice = voice


def test_live_states_read_from_bot() -> None:
    bot = _Bot(_Voice({"u1": {"m": 1, "hm": 0}}, 7))
    states, received = create_oopz_directory(bot).live_states()
    assert states == {"u1": {"m": 1, "hm": 0}}
    assert received == 7


def test_live_states_without_bot() -> None:
    assert create_oopz_directory(None).live_states() == ({}, 0)


def test_live_states_swallows_errors() -> None:
    class _Boom:
        @property
        def voice(self) -> Any:
            raise RuntimeError("boom")

    assert create_oopz_directory(_Boom()).live_states() == ({}, 0)


def test_live_states_ignores_non_mapping_entries() -> None:
    bot = _Bot(_Voice({"u1": {"m": 1}, "u2": "junk"}, 3))
    states, received = create_oopz_directory(bot).live_states()
    assert set(states) == {"u1"}
    assert received == 3


def test_live_states_handles_missing_attribute() -> None:
    """旧对象没有 voice_states（例如测试替身）时静默降级，不能抛。"""

    class _Old:
        voice = object()

    assert create_oopz_directory(_Old()).live_states() == ({}, 0)
