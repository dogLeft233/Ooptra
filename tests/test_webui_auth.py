"""Real HTTP authentication: logout revokes the browser session, not plugin access."""
import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from webui.server import WebUIConsole


def console_app(token="shared-password"):
    console = WebUIConsole(SimpleNamespace(), SimpleNamespace(), config={})
    console._token = token
    app = web.Application(middlewares=[console._auth_middleware])
    console._auth.mount(app)
    async def private(request):
        return web.json_response({"ok": True})
    app.router.add_get("/api/private", private)
    return console, app


def test_logout_revokes_copied_cookie_and_requires_password_again():
    async def check():
        _, app = console_app()
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/private")).status == 401
            assert (await client.post("/api/auth/login", json={"password": "wrong"})).status == 401
            login = await client.post("/api/auth/login", json={"password": "shared-password"})
            assert login.status == 200
            session = login.cookies["ooptra_session"]
            assert session.value != "shared-password"
            assert session["httponly"] and session["samesite"] == "Lax"
            assert (await client.get("/api/private")).status == 200
            assert (await client.post("/api/auth/logout", json={})).status == 200
            assert (await client.get("/api/private")).status == 401
            assert (await client.get("/api/private", headers={"Cookie": "ooptra_session=" + session.value})).status == 401
            # Legacy raw-token cookies must never restore a logged-out browser.
            assert (await client.get("/api/private", headers={"Cookie": "oopz_webui_token=shared-password"})).status == 401
            for headers in ({"Authorization": "Bearer shared-password"}, {"X-Ooptra-Token": "shared-password"}):
                assert (await client.get("/api/private", headers=headers)).status == 200
            assert (await client.post("/api/auth/login", json={"password": "shared-password"})).status == 200
    asyncio.run(check())


def test_empty_password_is_setup_required_not_public_access():
    async def check():
        _, app = console_app("")
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/private")).status == 401
            data = await (await client.get("/api/auth/status")).json()
            assert data["configured"] is False and data["setup_allowed"] is True
            assert (await client.post("/api/auth/login", json={"password": ""})).status != 200
    asyncio.run(check())


def test_cross_origin_login_and_cookie_write_are_rejected():
    async def check():
        _, app = console_app()
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/auth/login", json={"password": "shared-password"}, headers={"Origin": "https://other.example"})).status == 403
            assert (await client.post("/api/auth/login", json={"password": "shared-password"})).status == 200
            assert (await client.post("/api/auth/logout", json={}, headers={"Origin": "https://other.example"})).status == 403
            assert (await client.get("/api/private")).status == 200
    asyncio.run(check())


def test_password_rotation_invalidates_sessions_and_plugin_secret(monkeypatch):
    async def check():
        console, app = console_app()
        restarted = []
        async def restart(reason):
            restarted.append(reason)
        console._controller = SimpleNamespace(restart=restart)
        monkeypatch.setattr("webui.server.config_editor.apply_updates", lambda updates: {"changed": {"webui": ["token"]}})
        app.router.add_post("/api/config", console._handle_config_post)
        async with TestClient(TestServer(app)) as client:
            await client.post("/api/auth/login", json={"password": "shared-password"})
            result = await client.post("/api/config", json={"updates": {"webui": {"token": "new-password"}}, "restart_after": True})
            body = await result.json()
            assert body["reauth_required"] is True
            assert body["bridge_restart_requested"] is True and restarted
            assert (await client.get("/api/private")).status == 401
            assert (await client.get("/api/private", headers={"Authorization": "Bearer shared-password"})).status == 401
            assert (await client.get("/api/private", headers={"Authorization": "Bearer new-password"})).status == 200
    asyncio.run(check())


def test_standalone_api_uses_only_shared_webui_password(monkeypatch):
    import config
    from operations import load_voice_agent_settings
    monkeypatch.setattr(config, "WEBUI_CONFIG", {"token": "shared-password"})
    monkeypatch.setattr(config, "VOICE_API_CONFIG", {"token": "obsolete-password"})
    monkeypatch.setenv("VOICE_API_TOKEN", "obsolete-environment-password")
    assert load_voice_agent_settings()[1].token == "shared-password"


def test_settings_have_one_password_and_one_persona_editor():
    from webui.config_editor import schema_payload
    groups = schema_payload()
    assert "token" in groups["webui"]["fields"]
    assert "token" not in groups["voice_api"]["fields"]
    assert "persona" not in groups["voice"]["fields"]


def test_initial_setup_persists_shared_password_and_cannot_overwrite_it(monkeypatch, tmp_path):
    import config
    from operations import load_voice_agent_settings
    from webui import config_editor
    path = tmp_path / "config.py"
    path.write_text('WEBUI_CONFIG = {"token": ""}\n', encoding="utf-8")
    monkeypatch.setattr(config_editor, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "WEBUI_CONFIG", {"token": ""})
    async def check():
        _, app = console_app("")
        async with TestClient(TestServer(app)) as client:
            blocked = await client.post("/api/auth/setup", json={"password": "remote"}, headers={"Host": "public.example"})
            assert blocked.status == 403
            assert (await client.post("/api/auth/setup", json={"password": "first-password"})).status == 200
            assert (await client.get("/api/private")).status == 200
            assert (await client.post("/api/auth/setup", json={"password": "overwrite"})).status == 409
            assert load_voice_agent_settings()[1].token == "first-password"
            assert "first-password" in path.read_text(encoding="utf-8")
    asyncio.run(check())


def test_password_login_attempts_are_limited():
    async def check():
        _, app = console_app()
        async with TestClient(TestServer(app)) as client:
            for _ in range(10):
                assert (await client.post("/api/auth/login", json={"password": "wrong"})).status == 401
            assert (await client.post("/api/auth/login", json={"password": "wrong"})).status == 429
    asyncio.run(check())


def test_password_rotation_rejects_values_that_cannot_be_used_to_login():
    from webui import config_editor
    with pytest.raises(ValueError):
        config_editor._normalize_updates({"webui": {"token": "a" * 1025}})


def test_open_log_stream_stops_sending_after_logout():
    async def check():
        console, app = console_app()
        release = asyncio.Event()
        async def stream(name, *, first_lines):
            yield ": initial\n\n"
            await release.wait()
            yield "data: private log after logout\n\n"
        console._tailer = SimpleNamespace(resolve=lambda name: "fixture", stream=stream)
        app.router.add_get("/api/logs/stream", console._handle_logs_stream)
        async with TestClient(TestServer(app)) as client:
            await client.post("/api/auth/login", json={"password": "shared-password"})
            response = await client.get("/api/logs/stream")
            assert await response.content.readline() == b": initial\n"
            await client.post("/api/auth/logout", json={})
            release.set()
            remaining = await asyncio.wait_for(response.content.read(), 1)
            assert b"private log" not in remaining
    asyncio.run(check())


def test_unrelated_config_save_preserves_shared_runtime_password(monkeypatch, tmp_path):
    import config
    from webui import config_editor
    path = tmp_path / "config.py"
    path.write_text('WEBUI_CONFIG = {"token": "file-default", "log_lines": 300}\nVOICE_AGENT_CONFIG = {"persona": "old"}\n', encoding="utf-8")
    monkeypatch.setattr(config_editor, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "WEBUI_CONFIG", {"token": "runtime-shared", "log_lines": 300})
    monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", {"persona": "old"})
    config_editor.apply_updates({"voice": {"persona": "new"}, "webui": {"log_lines": 400}})
    assert config.WEBUI_CONFIG == {"token": "runtime-shared", "log_lines": 400}


def test_saved_password_is_canonical_and_environment_only_bootstraps_empty_config(monkeypatch):
    import config
    from operations import apply_runtime_overrides, load_voice_agent_settings
    monkeypatch.setenv("BOT_WEBUI_TOKEN", "environment-bootstrap")
    monkeypatch.setattr(config, "WEBUI_CONFIG", {"token": "saved-password"})
    apply_runtime_overrides()
    assert load_voice_agent_settings()[1].token == "saved-password"
    monkeypatch.setattr(config, "WEBUI_CONFIG", {"token": ""})
    apply_runtime_overrides()
    assert load_voice_agent_settings()[1].token == "environment-bootstrap"


def test_empty_password_allows_only_minimal_local_update_health(monkeypatch):
    async def check():
        console, app = console_app("")
        console._controller = SimpleNamespace(snapshot=lambda: {"runtime": {"supervisor_alive": True}, "recent_events": [{"text": "private"}], "oopz": {"connected": False}})
        console.set_bootstrap_ready(True)
        app.router.add_get("/api/status", console._handle_status)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/status")
            assert response.status == 200
            health = await response.json()
            assert health["process"]["bootstrap_ready"] is True
            assert health["bridge"]["runtime"]["supervisor_alive"] is True
            assert "config" not in health and "recent_events" not in health["bridge"]
            assert (await client.get("/api/status", headers={"Host": "remote.example"})).status == 401
    asyncio.run(check())


def test_password_rotation_reaches_standalone_api_even_if_voice_reload_fails(monkeypatch):
    from app.http import VoiceApiServer
    from operations.models import VoiceApiSettings
    async def check():
        console, app = console_app()
        async def failing_reload():
            raise RuntimeError("voice reload unavailable")
        runtime = SimpleNamespace(api_settings=VoiceApiSettings(token="shared-password"), reload_settings=failing_reload)
        console._voice_runtime = runtime
        console._maintenance = SimpleNamespace(token="shared-password")
        api = VoiceApiServer(runtime, runtime.api_settings)
        async def endpoint(request):
            return web.json_response({"ok": True})
        standalone = web.Application()
        standalone.router.add_get("/voice/status", api._wrap(endpoint))
        monkeypatch.setattr("webui.server.config_editor.apply_updates", lambda updates: {"changed": {"webui": ["token"]}})
        app.router.add_post("/api/config", console._handle_config_post)
        async with TestClient(TestServer(app)) as client, TestClient(TestServer(standalone)) as plugin:
            await client.post("/api/auth/login", json={"password": "shared-password"})
            response = await client.post("/api/config", json={"updates": {"webui": {"token": "new-password"}}})
            assert response.status == 200
            assert console._maintenance.token == "new-password"
            assert (await plugin.get("/voice/status", headers={"Authorization": "Bearer shared-password"})).status == 401
            assert (await plugin.get("/voice/status", headers={"Authorization": "Bearer new-password"})).status == 200
    asyncio.run(check())


@pytest.mark.parametrize("initial_setup", [False, True])
def test_cancelled_configuration_request_finishes_saved_password_rotation(tmp_path, monkeypatch, initial_setup):
    import contextlib
    import threading

    from operations.models import VoiceApiSettings
    from webui import config_editor

    entered, release = threading.Event(), threading.Event()
    initial = "" if initial_setup else "shared-password"
    path = tmp_path / "config.py"
    path.write_text(f'WEBUI_CONFIG = {{"token": {initial!r}}}\n', encoding="utf-8")
    config = SimpleNamespace(WEBUI_CONFIG={"token": initial})

    def running_config():
        entered.set()
        assert release.wait(3)
        return config

    monkeypatch.setattr(config_editor, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config_editor, "_runtime_config", running_config)

    async def check():
        console, app = console_app(initial)
        reloaded = asyncio.Event()
        handlers = []

        async def reload_settings():
            reloaded.set()
            return {}

        console._voice_runtime = SimpleNamespace(api_settings=VoiceApiSettings(token=initial),
                                                 reload_settings=reload_settings)
        console._maintenance = SimpleNamespace(token=initial)

        @web.middleware
        async def capture(request, handler):
            if request.method == "POST":
                handlers.append(asyncio.current_task())
            return await handler(request)

        app.middlewares.append(capture)
        app.router.add_post("/api/config", console._handle_config_post)
        async with TestClient(TestServer(app)) as client:
            endpoint = "/api/auth/setup" if initial_setup else "/api/config"
            payload = ({"password": "new-password"} if initial_setup else
                       {"updates": {"webui": {"token": "new-password"}}})
            request = asyncio.ensure_future(client.post(
                endpoint, json=payload,
                headers={} if initial_setup else {"Authorization": "Bearer shared-password"}))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                handlers[0].cancel()
                await asyncio.sleep(0)
                handlers[0].cancel()
                release.set()
                await asyncio.wait_for(reloaded.wait(), 1)
                assert (await client.get("/api/private", headers={"Authorization": "Bearer new-password"})).status == 200
                assert (await client.get("/api/private", headers={"Authorization": "Bearer shared-password"})).status == 401
                assert console._voice_runtime.api_settings.token == "new-password"
                assert console._maintenance.token == "new-password"
                assert config.WEBUI_CONFIG["token"] == "new-password"
                assert "new-password" in path.read_text(encoding="utf-8")
            finally:
                release.set()
                request.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await request
    asyncio.run(check())


def test_console_stop_waits_for_accepted_configuration_application(tmp_path, monkeypatch):
    import contextlib

    from operations.models import VoiceApiSettings
    from webui import config_editor

    path = tmp_path / "config.py"
    path.write_text('WEBUI_CONFIG = {"token": "shared-password"}\n', encoding="utf-8")
    monkeypatch.setattr(config_editor, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config_editor, "_runtime_config", lambda: SimpleNamespace(WEBUI_CONFIG={}))

    async def check():
        console, app = console_app()
        entered, release = asyncio.Event(), asyncio.Event()

        async def reload_settings():
            entered.set()
            await release.wait()
            return {}

        console._voice_runtime = SimpleNamespace(api_settings=VoiceApiSettings(token="shared-password"),
                                                 reload_settings=reload_settings)
        app.router.add_post("/api/config", console._handle_config_post)
        async with TestClient(TestServer(app)) as client:
            request = asyncio.ensure_future(client.post(
                "/api/config", json={"updates": {"webui": {"token": "new-password"}}},
                headers={"Authorization": "Bearer shared-password"}))
            stop = None
            try:
                await asyncio.wait_for(entered.wait(), 1)
                stop = asyncio.create_task(console.stop())
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not stop.done()
                stop.cancel()
                await asyncio.sleep(0)
                assert not stop.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await stop
                assert (await client.get("/api/private", headers={"Authorization": "Bearer new-password"})).status == 200
            finally:
                release.set()
                if stop is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await stop
                request.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await request
    asyncio.run(check())
