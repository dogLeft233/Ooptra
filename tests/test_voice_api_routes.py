"""VOICE_API 路由契约：3090（WebUI）与 3091（独立端口）必须完全一致。

这个文件存在的理由：旧版 3091 自带一份手写路由表，悄悄缺了
``GET /voice/channels``、``/voice/members`` 还会因为 pydantic 模型不可
序列化而 500。旧的 ``test_voice_api_contract.py`` 只做形状检查、从不启动
aiohttp app，所以结构上发现不了这类问题。

这里用真实 aiohttp Application 断言路由集合，并真实发请求。

注意：一个 ``web.Application`` 只能绑定一个事件循环，所以每个 app 的所有
请求必须在**同一次** ``asyncio.run`` 里发完（``_run`` 一次收一批）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.http import VoiceApiServer
from operations.models import VoiceApiSettings
from webui.voice_routes import build_voice_routes, mount_voice_routes

# ----------------------------------------------------------------------
# 假 runtime / 假 bot
# ----------------------------------------------------------------------


class FakeSettings:
    enabled = True
    persona = "测试人格"
    area = ""
    channel = ""


class FakeChannels:
    def __init__(self, result: Any) -> None:
        self._result = result

    async def get_voice_channel_members(self, area: str) -> Any:
        return self._result


class FakeAreas:
    async def get_area_channels(self, area: str) -> list:
        return []

    async def get_joined_areas(self) -> list:
        return []


class FakeBot:
    def __init__(self, result: Any = None) -> None:
        self.channels = FakeChannels(result)
        self.areas = FakeAreas()


class FakeMemory:
    def as_messages(self, user_key: str = "", limit: int | None = None) -> list:
        return []


class FakeAgent:
    @property
    def directory(self):
        from channels import create_oopz_directory
        return create_oopz_directory(self._bot)

    def __init__(self, members_result: Any = None) -> None:
        self.settings = FakeSettings()
        self.memory = FakeMemory()
        self._bot = FakeBot(members_result)
        self._area = "area-1"
        self._channel = "chan-1"

    def status(self) -> dict:
        return {
            "enabled": True,
            "backend": "gemini_live",
            "mode": "live",
            "joined": True,
            "area": "area-1",
            "channel": "chan-1",
            "speaking": False,
            "turns": 3,
            "last_reply": "",
            "last_user_text": "",
        }

    async def join(self, area: str = "", channel: str = "") -> dict:
        return {"area": area or "area-1", "channel": channel or "chan-1", "mode": "live"}

    async def leave(self) -> dict:
        return {"ok": True}

    async def speak_text(self, text: str) -> dict:
        # 与 VoiceCoordinator.speak_text 保持一致：空文本是错误，不是成功
        if not (text or "").strip():
            return {"ok": False, "error": "text required"}
        return {"ok": True, "chars": len(text), "mode": "live"}

    def update_persona(self, persona: str) -> None:
        self.settings.persona = persona

    async def refresh(self, changed: list[str]) -> list[str]:
        return []


class FakeRuntime:
    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------


def _build_apps(agent: FakeAgent) -> tuple[web.Application, web.Application]:
    """返回 (WebUI app, 独立 VOICE_API app)。"""
    runtime = FakeRuntime(agent)

    webui_app = web.Application()
    mount_voice_routes(webui_app, runtime)

    api = VoiceApiServer(runtime, VoiceApiSettings(enabled=True, token="fixture-api"))
    standalone_app = web.Application()
    standalone_app.add_routes(build_voice_routes(runtime, wrap=api._wrap))
    return webui_app, standalone_app


def _contract_keys(app: web.Application) -> set[tuple[str, str]]:
    """路由集合；WebUI 的 /api/* 历史别名归一掉，便于与独立端口直接比对。"""
    keys: set[tuple[str, str]] = set()
    for route in app.router.routes():
        path = str(route.resource.canonical)
        if path.startswith("/api/"):
            path = path[4:]
        keys.add((str(route.method), path))
    return keys


def _run(app: web.Application, calls: list[tuple[str, str, dict]]) -> list[tuple[int, Any]]:
    """在同一个事件循环里依次发完这批请求。"""

    async def runner() -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        async with TestClient(TestServer(app)) as client:
            for method, path, kwargs in calls:
                kwargs = {"headers": {"Authorization": "Bearer fixture-api"}, **kwargs}
                resp = await client.request(method, path, **kwargs)
                out.append((resp.status, await resp.text()))
        return out

    parsed: list[tuple[int, Any]] = []
    for status, body in asyncio.run(runner()):
        try:
            parsed.append((status, json.loads(body)))
        except json.JSONDecodeError:
            parsed.append((status, body))
    return parsed


# ----------------------------------------------------------------------
# 路由集合一致性 —— 直接锁死 B1 类回归
# ----------------------------------------------------------------------


def test_standalone_and_webui_expose_same_contract_routes() -> None:
    agent = FakeAgent()
    webui_app, standalone_app = _build_apps(agent)

    webui = _contract_keys(webui_app)
    standalone = _contract_keys(standalone_app)

    assert webui == standalone, (
        f"独立 VOICE_API 缺少：{sorted(webui - standalone)}；"
        f"多出：{sorted(standalone - webui)}"
    )


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/health"),
        ("GET", "/voice/status"),
        ("GET", "/voice/members"),
        ("GET", "/voice/channels"),
        ("POST", "/voice/join"),
        ("POST", "/voice/leave"),
        ("POST", "/voice/speak"),
    ],
)
def test_contract_paths_present_on_both(method: str, path: str) -> None:
    """插件 astrbot_plugin_ooptra 0.3.0+ 依赖这些路径。"""
    agent = FakeAgent()
    webui_app, standalone_app = _build_apps(agent)
    for app in (webui_app, standalone_app):
        assert (method, path) in _contract_keys(app), f"{method} {path} 缺失"


# ----------------------------------------------------------------------
# 真实请求
# ----------------------------------------------------------------------


def test_members_serializes_pydantic_result() -> None:
    """B2：SDK 返回 pydantic 模型时不能 500。"""
    from oopz_sdk.models.channel import VoiceChannelMemberInfo, VoiceChannelMembersResult

    result = VoiceChannelMembersResult(
        channelMembers={
            "chan-1": [
                VoiceChannelMemberInfo.model_validate(
                    {"uid": "u1", "isBot": False, "enterTime": "1"}
                ),
                VoiceChannelMemberInfo.model_validate(
                    {"uid": "u2", "isBot": True, "enterTime": "2"}
                ),
            ]
        }
    )
    agent = FakeAgent(result)
    webui_app, standalone_app = _build_apps(agent)

    for app in (webui_app, standalone_app):
        [(status, payload)] = _run(app, [("GET", "/voice/members?area=area-1", {})])
        assert status == 200, payload
        assert payload["ok"] is True
        assert payload["count"] == 2
        assert [row["uid"] for row in payload["members"]] == ["u1", "u2"]
        # 两套字段族都要在：插件读 mic/m/hm，WebUI 读 mic_muted/speaker_muted
        for key in ("uid", "name", "mic", "speaker", "m", "hm", "mic_muted", "speaker_muted"):
            assert key in payload["members"][0], f"成员缺少字段 {key}"
        # 静音状态 SDK 拿不到 → 必须是 None（未知），不能伪造成「开麦」
        assert payload["members"][0]["mic_muted"] is None
        assert payload["members"][0]["mic"] is None
        # bot 不计入分频道人数
        assert payload["channel_counts"]["chan-1"] == 1


def test_status_is_flat_on_both() -> None:
    """插件 format_status 读扁平字段；独立端口以前返回的是嵌套结构。"""
    agent = FakeAgent()
    webui_app, standalone_app = _build_apps(agent)

    for app in (webui_app, standalone_app):
        [(status, payload)] = _run(app, [("GET", "/voice/status", {})])
        assert status == 200, payload
        assert payload["joined"] is True
        assert payload["area"] == "area-1"
        assert payload["channel"] == "chan-1"
        assert payload["state"] == "joined"


def test_join_and_leave_return_flat_contract() -> None:
    agent = FakeAgent()
    _webui, standalone = _build_apps(agent)

    results = _run(
        standalone,
        [
            ("POST", "/voice/join", {"json": {"area": "a", "channel": "c"}}),
            ("POST", "/voice/leave", {}),
        ],
    )
    status, payload = results[0]
    assert status == 200 and payload["ok"] is True
    assert payload["joined"] is True and payload["area"] == "a"
    status, payload = results[1]
    assert status == 200 and payload["joined"] is False


def test_speak_without_text_is_400_not_500() -> None:
    agent = FakeAgent()
    _webui, standalone = _build_apps(agent)
    [(status, payload)] = _run(standalone, [("POST", "/voice/speak", {"json": {"text": ""}})])
    assert status == 400, payload
    assert payload["ok"] is False


def test_join_failure_is_json_not_bare_500() -> None:
    """未进房/机器人没起来时，插件要能拿到可读原因，而不是裸 500。"""

    class BrokenAgent(FakeAgent):
        async def join(self, area: str = "", channel: str = "") -> dict:
            raise RuntimeError("Oopz bot 尚未就绪")

    agent = BrokenAgent()
    _webui, standalone = _build_apps(agent)
    [(status, payload)] = _run(
        standalone, [("POST", "/voice/join", {"json": {"area": "a", "channel": "c"}})]
    )
    assert status == 503, payload
    assert payload["ok"] is False
    assert "尚未就绪" in payload["error"]


@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize("operation", ["join", "leave"])
def test_room_operation_rejection_is_not_reported_as_success(prefix, operation):
    class RejectedAgent(FakeAgent):
        async def join(self, area="", channel=""):
            return {"ok": False, "error": "old room leave failed"}

        async def leave(self):
            return {"ok": False, "error": "old room leave failed"}

    agent = RejectedAgent()
    app, _ = _build_apps(agent)
    [(status, payload)] = _run(app, [
        ("POST", f"{prefix}/voice/{operation}", {"json": {"area": "new", "channel": "new"}}),
    ])
    assert status == 503, payload
    assert payload["ok"] is False
    assert payload["error"] == "old room leave failed"
    assert "joined" not in payload


def test_route_handlers_follow_runtime_agent_swap() -> None:
    """热重载会替换 runtime.agent：handler 必须每次请求重新读取。"""
    agent = FakeAgent()
    runtime = FakeRuntime(agent)
    app = web.Application()
    mount_voice_routes(app, runtime)

    new_agent = FakeAgent()
    new_agent.settings.persona = "换过的人格"
    runtime.agent = new_agent

    [(status, payload)] = _run(app, [("GET", "/persona", {})])
    assert status == 200
    assert payload["persona"] == "换过的人格"


def test_persona_save_persists_before_applying_and_survives_settings_reload(monkeypatch, tmp_path):
    import config
    from operations import load_voice_agent_settings
    from webui import config_editor
    path = tmp_path / "config.py"
    path.write_text('VOICE_AGENT_CONFIG = {"persona": "original"}\n', encoding="utf-8")
    monkeypatch.setattr(config_editor, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", {"persona": "original"})
    agent = FakeAgent()
    app, _ = _build_apps(agent)
    [(status, payload)] = _run(app, [("PUT", "/api/persona", {"json": {"persona": "新的自然聊天人格"}})])
    assert status == 200, payload
    assert "新的自然聊天人格" in path.read_text(encoding="utf-8")
    assert load_voice_agent_settings()[0].persona == "新的自然聊天人格"
    assert agent.settings.persona == "新的自然聊天人格"


def test_persona_write_failure_keeps_running_persona(monkeypatch):
    from webui import config_editor
    def fail(_updates):
        raise OSError("read only")
    monkeypatch.setattr(config_editor, "apply_updates", fail)
    agent = FakeAgent()
    app, _ = _build_apps(agent)
    [(status, payload)] = _run(app, [("PUT", "/api/persona", {"json": {"persona": "new"}})])
    assert status == 500 and payload["ok"] is False
    assert agent.settings.persona == "测试人格"


def test_standalone_with_token_rejects_missing_token() -> None:
    """独立端口配了 token 就必须校验（未配置时才是开放的）。"""
    agent = FakeAgent()
    runtime = FakeRuntime(agent)
    api = VoiceApiServer(runtime, VoiceApiSettings(enabled=True, token="s3cret"))
    app = web.Application()
    app.add_routes(build_voice_routes(runtime, wrap=api._wrap))

    results = _run(
        app,
        [
            ("GET", "/voice/status", {}),
            ("GET", "/voice/status", {"params": {"token": "s3cret"}}),
        ],
    )
    assert results[0][0] == 401
    assert results[1][0] == 200


def test_standalone_without_shared_password_is_closed():
    runtime = FakeRuntime(FakeAgent())
    api = VoiceApiServer(runtime, VoiceApiSettings(enabled=True, token=""))
    app = web.Application()
    app.add_routes(build_voice_routes(runtime, wrap=api._wrap))
    assert _run(app, [("GET", "/voice/status", {})])[0][0] == 401


# ----------------------------------------------------------------------
# 成员范围与实时状态计数
# ----------------------------------------------------------------------


class FakeVoice:
    """只提供 _voice_live_states 需要的两个接口。"""

    def __init__(self, states: dict, received: int) -> None:
        self._states = states
        self.voice_state_received = received

    def voice_states(self) -> dict:
        return self._states


class FakeBotWithVoice(FakeBot):
    def __init__(self, result: Any, states: dict, received: int) -> None:
        super().__init__(result)
        self.voice = FakeVoice(states, received)


TWO_ROOMS = {
    "channelMembers": {
        "chan-1": [{"uid": "u1", "name": "本房甲"}, {"uid": "u2", "name": "本房乙"}],
        "chan-2": [{"uid": "u9", "name": "别房丙"}],
    }
}


def _agent_with_voice(states: dict, received: int) -> FakeAgent:
    agent = FakeAgent(TWO_ROOMS)
    agent._bot = FakeBotWithVoice(TWO_ROOMS, states, received)  # type: ignore[assignment]
    return agent


def test_members_without_channel_aggregates_every_room() -> None:
    """不传 channel 时是「整个域汇总」——所以别房的成员也会出现，
    而他们的静音状态永远拿不到（广播只在同一个 Agora 房间内）。"""
    agent = _agent_with_voice({"u1": {"m": 1, "hm": 0}}, 3)
    webui_app, _ = _build_apps(agent)

    [(status, payload)] = _run(
        webui_app, [("GET", "/voice/members?area=area-1", {})]
    )
    assert status == 200, payload
    assert payload["count"] == 3
    assert {row["uid"] for row in payload["members"]} == {"u1", "u2", "u9"}
    # 只有 u1 有实时状态；u9 在别的房，永远不会广播
    assert payload["live_members"] == 1
    assert payload["live_state_received"] == 3
    by_uid = {row["uid"]: row for row in payload["members"]}
    assert by_uid["u1"]["mic_muted"] is True
    assert by_uid["u9"]["mic_muted"] is None


def test_members_scoped_to_channel_excludes_other_rooms() -> None:
    """前端传 bot 所在频道后，表里只剩本房成员 —— 这才是能拿到状态的集合。"""
    agent = _agent_with_voice({"u1": {"m": 0, "hm": 1}, "u2": {"m": 1}}, 2)
    webui_app, _ = _build_apps(agent)

    [(status, payload)] = _run(
        webui_app, [("GET", "/voice/members?area=area-1&channel=chan-1", {})]
    )
    assert status == 200, payload
    assert payload["count"] == 2
    assert {row["uid"] for row in payload["members"]} == {"u1", "u2"}
    assert payload["channel"] == "chan-1"
    # 2 人全部拿到状态
    assert payload["live_members"] == 2
    by_uid = {row["uid"]: row for row in payload["members"]}
    assert by_uid["u1"]["mic_muted"] is False and by_uid["u1"]["speaker_muted"] is True
    assert by_uid["u2"]["mic_muted"] is True


def test_live_members_never_exceeds_count() -> None:
    """广播里可能有已经离开房间的人：计数不能超过表内行数，也不能为负。"""
    agent = _agent_with_voice({"u1": {"m": 0}, "ghost": {"m": 0}}, 9)
    webui_app, _ = _build_apps(agent)

    [(status, payload)] = _run(
        webui_app, [("GET", "/voice/members?area=area-1&channel=chan-1", {})]
    )
    assert status == 200, payload
    assert payload["live_members"] == 1  # ghost 不在本房成员表里，不计
    assert payload["live_state_received"] == 9  # 累计收到条数是独立指标


def test_members_unknown_channel_is_empty_not_error() -> None:
    """查一个不存在的频道：空表 + 200，前端能把原因说清楚。"""
    agent = _agent_with_voice({}, 0)
    webui_app, _ = _build_apps(agent)

    [(status, payload)] = _run(
        webui_app, [("GET", "/voice/members?area=area-1&channel=nope", {})]
    )
    assert status == 200, payload
    assert payload["count"] == 0 and payload["members"] == []
    assert payload["live_members"] == 0


@pytest.mark.parametrize('payload,status', [('broken-json', 400), ('x' * (3 * 1024 * 1024 + 1), 413)], ids=['invalid-json', 'oversized'])
def test_standalone_guard_preserves_http_body_errors(payload, status):
    async def run():
        server = VoiceApiServer(FakeRuntime(FakeAgent()), VoiceApiSettings(
            enabled=True, host='127.0.0.1', port=0, token='fixture-api',
        ))
        await server.start()
        try:
            port = server._runner.addresses[0][1]
            from aiohttp import ClientSession
            async with ClientSession() as client, client.delete(
                f'http://127.0.0.1:{port}/memory', data=payload,
                headers={'Authorization': 'Bearer fixture-api', 'Content-Type': 'application/json'},
            ) as response:
                assert response.status == status
        finally:
            await server.stop()
    asyncio.run(run())
