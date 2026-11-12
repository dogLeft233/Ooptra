"""VOICE_API 不能和 Web 控制台抢同一个监听地址。

回归锁死一个真实故障：``VOICE_API_CONFIG.port`` 被填成与 ``WEBUI_CONFIG.port``
相同的 3090。Windows 下 aiohttp 默认开 SO_REUSEADDR，``0.0.0.0:3090`` 与
``127.0.0.1:3090`` **都能绑定成功**，但更具体的回环 socket 会抢走本机流量 ——
浏览器打开 ``http://127.0.0.1:3090`` 看到的是 VOICE_API 那个 app：
``/`` 、``/api/status``、``/api/update`` 全 404，``/health`` 则是 401。
表现为「WebUI 打不开、检查更新 404」，而用局域网 IP 打开一切正常，极难定位。
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from operations.models import VoiceApiSettings
from voice_agent.api import VoiceApiServer, _same_bind_target


class _FakeRuntime:
    """VoiceApiServer 只透传给 build_voice_routes，构建期不读属性。"""

    agent = None


def _free_port() -> int:
    """要一个当前空闲的端口：不写死 3090，免得测试依赖机器上跑着什么。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_api(host: str, port: int, *, token: str = "") -> VoiceApiServer:
    return VoiceApiServer(
        _FakeRuntime(), VoiceApiSettings(enabled=True, host=host, port=port, token=token)
    )


# ----------------------------------------------------------------------
# 地址重叠判定
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host_a", "host_b", "expected"),
    [
        ("0.0.0.0", "127.0.0.1", True),  # Windows 实测会抢流量
        ("127.0.0.1", "0.0.0.0", True),
        ("127.0.0.1", "127.0.0.1", True),
        ("127.0.0.1", "localhost", True),
        ("localhost", "::1", True),
        ("", "192.168.1.3", True),  # 空主机 = 通配
        ("127.0.0.1", "192.168.1.3", False),  # 不同网卡，互不干扰
        ("192.168.1.3", "192.168.1.4", False),
    ],
)
def test_same_bind_target_hosts(host_a: str, host_b: str, expected: bool) -> None:
    assert _same_bind_target(host_a, 3090, host_b, 3090) is expected


def test_different_port_never_collides() -> None:
    assert _same_bind_target("127.0.0.1", 3090, "127.0.0.1", 3091) is False


@pytest.mark.parametrize(("port_a", "port_b"), [(None, 3090), ("x", 3090), (3090, None)])
def test_bad_port_is_not_a_collision(port_a: object, port_b: object) -> None:
    """端口读不出来时不该拦着 VOICE_API 启动。"""
    assert _same_bind_target("127.0.0.1", port_a, "127.0.0.1", port_b) is False


# ----------------------------------------------------------------------
# start() 的实际行为
# ----------------------------------------------------------------------


def test_start_skips_when_webui_owns_the_same_address() -> None:
    """同地址必须**不启动**：不是警告了事，而是真的不绑。"""
    port = _free_port()
    api = _make_api("127.0.0.1", port)
    api.set_webui_endpoint("0.0.0.0", port)
    assert _start_then_stop(api) is False


def _start_then_stop(api: VoiceApiServer) -> bool:
    """在**同一个事件循环**里起停，返回是否真的绑上了。"""

    async def runner() -> bool:
        await api.start()
        bound = api._runner is not None
        await api.stop()
        return bound

    return asyncio.run(runner())


def test_start_binds_when_ports_differ() -> None:
    api_port = _free_port()
    api = _make_api("127.0.0.1", api_port)
    api.set_webui_endpoint("127.0.0.1", api_port + 1)  # 差 1 就一定不同，不受端口复用影响
    assert _start_then_stop(api) is True


def test_start_binds_when_webui_never_started() -> None:
    """WebUI 没起来（未启用 / 端口被占）时，VOICE_API 应当照常监听。"""
    api = _make_api("127.0.0.1", _free_port())
    assert _start_then_stop(api) is True


def test_disabled_never_binds() -> None:
    api = VoiceApiServer(_FakeRuntime(), VoiceApiSettings(enabled=False, host="127.0.0.1", port=0))
    asyncio.run(api.start())
    assert api._runner is None
