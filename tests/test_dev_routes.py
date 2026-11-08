import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from webui.voice_routes import build_voice_routes


def test_diagnostic_audio_and_stop_routes_use_current_agent(monkeypatch):
    from app import diagnostics

    async def run():
        agent = SimpleNamespace(stop_current_reply=AsyncMock())
        diagnose = AsyncMock(return_value={"checks": [], "passed": True})
        monkeypatch.setattr(diagnostics, "diagnose", diagnose)
        app = web.Application(client_max_size=3 * 1024 * 1024)
        runtime = SimpleNamespace(agent=agent)
        app.add_routes(build_voice_routes(runtime, with_api_alias=True))
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/voice/diagnostics", json={
                "network": False, "audio_wav_base64": base64.b64encode(b"fixture-wav").decode(),
            })
            assert response.status == 200
            diagnose.assert_awaited_once_with(agent, network=False, audio_wav=b"fixture-wav")
            replacement = SimpleNamespace(stop_current_reply=AsyncMock())
            runtime.agent = replacement
            stopped = await client.post("/api/voice/stop", json={})
            assert stopped.status == 200 and (await stopped.json())["ok"]
            replacement.stop_current_reply.assert_awaited_once()
            agent.stop_current_reply.assert_not_awaited()
    asyncio.run(run())


def test_invalid_diagnostic_audio_is_rejected_before_model_call(monkeypatch):
    from app import diagnostics

    async def run():
        diagnose = AsyncMock(return_value={"checks": []})
        monkeypatch.setattr(diagnostics, "diagnose", diagnose)
        app = web.Application(client_max_size=3 * 1024 * 1024)
        app.add_routes(build_voice_routes(SimpleNamespace(agent=object()), with_api_alias=True))
        async with TestClient(TestServer(app)) as client:
            for source in ("not-base64!", 123, "x" * (3 * 1024 * 1024)):
                response = await client.post("/api/voice/diagnostics", json={"audio_wav_base64": source})
                assert response.status in {400, 413}
            diagnose.assert_not_awaited()
    asyncio.run(run())


def test_status_exposes_layers_when_external_connections_are_offline():
    from webui.server import WebUIConsole

    console = WebUIConsole(None, SimpleNamespace(snapshot=lambda: {
        "runtime": {"supervisor_alive": True},
        "oopz": {"connected": False}, "onebot": {"connected": False},
    }))
    console.set_bootstrap_ready(True)
    response = asyncio.run(console._handle_status(None))
    import json
    payload = json.loads(response.body)
    assert payload["health"]["process"]["state"] == "healthy"
    assert payload["health"]["oopz"]["state"] == "degraded"
    assert payload["health"]["onebot"]["state"] == "degraded"
