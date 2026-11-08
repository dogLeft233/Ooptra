from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from operations.models import VoiceAgentSettings, VoiceApiSettings
from voice_agent.runtime import VoiceRuntime
from webui.voice_routes import build_voice_routes


def make_runtime(tmp_path, monkeypatch):
    import config
    from voice_agent import runtime

    monkeypatch.setattr(config, "VOICE_AUTO_VISIT_CONFIG", {}, raising=False)
    monkeypatch.setattr(runtime, "load_voice_agent_settings", lambda: (
        VoiceAgentSettings(enabled=False, memory_path=str(tmp_path / "memory.jsonl")),
        VoiceApiSettings()))
    return VoiceRuntime(state_path=tmp_path / "auto.json")


def test_runtime_hot_reload_does_not_restart_manual_voice(tmp_path, monkeypatch):
    async def run():
        import config

        rt = make_runtime(tmp_path, monkeypatch)
        agent = rt.agent
        config.VOICE_AUTO_VISIT_CONFIG = {"daily_limit": 7, "areas": {"a": {"enabled": True}}}
        result = await rt.reload_settings()
        assert rt.agent is agent
        assert rt.auto_visit.config.daily_limit == 7
        assert "auto_visit" in result["changed"]
        await rt.stop()
    asyncio.run(run())


def test_auto_routes_get_pause_resume_and_invalid_save(tmp_path, monkeypatch):
    async def run():
        rt = make_runtime(tmp_path, monkeypatch)
        app = web.Application()
        app.add_routes(build_voice_routes(rt, with_api_alias=True))
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/voice/auto-visit")
            assert response.status == 200
            payload = await response.json()
            assert payload["config"]["daily_limit"] == 3
            assert payload["config"]["areas"] == {}
            response = await client.post("/voice/auto-visit/pause")
            assert (await response.json())["status"]["paused"]
            response = await client.post("/voice/auto-visit/resume")
            assert not (await response.json())["status"]["paused"]
            response = await client.post("/api/voice/auto-visit/config", json={"updates": []})
            assert response.status == 400
            response = await client.get("/voice/status")
            payload = await response.json()
            assert payload["state"] == "idle"
            assert "auto_visit" in payload
        await rt.stop()
    asyncio.run(run())


def test_bind_registers_presence_once_and_disconnect_is_system_leave(tmp_path, monkeypatch):
    async def run():
        from oopz_sdk.events.registry import EventRegistry

        rt = make_runtime(tmp_path, monkeypatch)
        bot = SimpleNamespace(registry=EventRegistry())
        monkeypatch.setattr(rt.agent, "bind_bot", lambda value: None)
        rt.bind_bot(bot)
        rt.bind_bot(bot)
        assert len(bot.registry.get_handlers("voice.enter")) == 1
        sources = []

        async def leave(*, source):
            sources.append(source)

        monkeypatch.setattr(rt.agent, "leave", leave)
        await rt.set_bot_ready(False)
        assert sources == ["system"]
        assert not rt.auto_visit._ready
        await rt.stop()
    asyncio.run(run())


def test_hot_enable_from_default_off_starts_and_stops_scheduler(tmp_path, monkeypatch):
    async def run():
        from voice_agent import runtime

        rt = make_runtime(tmp_path, monkeypatch)
        monkeypatch.setattr(runtime, "load_voice_agent_settings", lambda: (
            VoiceAgentSettings(enabled=True, memory_path=str(tmp_path / "memory.jsonl")),
            VoiceApiSettings()))
        await rt.reload_settings()
        assert rt._started
        assert rt.auto_visit._loop is not None
        assert rt.agent.running
        await rt.stop()
        assert not rt.agent.running
        assert rt.auto_visit._loop is None
    asyncio.run(run())


def test_real_config_save_hot_applies_and_aliases_require_auth(tmp_path, monkeypatch):
    async def run():
        import config
        from voice_agent.api import VoiceApiServer
        from webui import config_editor
        from webui.server import WebUIConsole

        rt = make_runtime(tmp_path, monkeypatch)
        path = tmp_path / "config.py"
        path.write_text('VOICE_AUTO_VISIT_CONFIG = {"areas": {"a": {"enabled": False}}}\n', encoding="utf-8")
        monkeypatch.setattr(config_editor, "CONFIG_PATH", str(path))
        monkeypatch.setattr(config_editor, "_runtime_config", lambda: config)
        console = WebUIConsole(SimpleNamespace(), SimpleNamespace(), voice_runtime=rt)
        console._token = "fixture-token"
        api = VoiceApiServer(rt, VoiceApiSettings(token="fixture-token"))
        web_app = web.Application(middlewares=[console._auth_middleware])
        web_app.add_routes(build_voice_routes(rt, with_api_alias=True))
        standalone = web.Application()
        standalone.add_routes(build_voice_routes(rt, wrap=api._wrap))
        headers = {"Authorization": "Bearer fixture-token"}
        async with TestClient(TestServer(web_app)) as client, TestClient(TestServer(standalone)) as other:
            for url in ("/voice/auto-visit", "/api/voice/auto-visit"):
                assert (await client.get(url)).status == 401
            assert (await other.post("/voice/auto-visit/pause")).status == 401
            response = await client.post("/api/voice/auto-visit/config", headers=headers, json={
                "updates": {"daily_limit": 5, "areas": {"a": {"enabled": True}}}})
            assert response.status == 200
            payload = await response.json()
            assert payload["config"]["daily_limit"] == 5
            assert payload["config"]["areas"]["a"]["enabled"]
            assert not payload["restart_required"]
            assert rt.auto_visit.config.daily_limit == 5
            persisted = {}
            exec(path.read_text(encoding="utf-8"), persisted)
            assert persisted["VOICE_AUTO_VISIT_CONFIG"]["daily_limit"] == 5
            response = await other.get("/voice/auto-visit", headers=headers)
            assert (await response.json())["config"] == payload["config"]
            await other.post("/voice/auto-visit/pause", headers=headers)
            response = await client.get("/api/voice/auto-visit", headers=headers)
            assert (await response.json())["status"]["paused"]
            before = path.read_text(encoding="utf-8")
            response = await client.post("/api/voice/auto-visit/config", headers=headers,
                                         json={"updates": {"daily_limit": -1}})
            assert response.status == 400
            assert path.read_text(encoding="utf-8") == before
        await rt.stop()
    asyncio.run(run())


def test_disconnect_negative_result_pauses_auto_visit(tmp_path, monkeypatch):
    async def run():
        rt = make_runtime(tmp_path, monkeypatch)

        async def leave(**kwargs):
            return {"ok": False, "error": "browser unavailable"}

        monkeypatch.setattr(rt.agent, "leave", leave)
        await rt.set_bot_ready(False)
        assert rt.auto_visit.status()["paused"]
        await rt.stop()
    asyncio.run(run())
