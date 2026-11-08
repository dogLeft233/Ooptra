import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from context import create_memory as MemoryStore
from core.proxy_utils import resolve_proxy_settings
from operations import patch_config_text, restored_endpoint
from webui import config_editor as editor
from webui.voice_routes import build_voice_routes


@pytest.mark.parametrize("token,expected", [("persisted", "persisted"), ("", "bootstrap")])
def test_restored_endpoint_matches_environment_without_execution(tmp_path, monkeypatch, token, expected):
    monkeypatch.setenv("BOT_WEBUI_HOST", "::")
    monkeypatch.setenv("BOT_WEBUI_PORT", "3210")
    monkeypatch.setenv("BOT_WEBUI_TOKEN", "bootstrap")
    path = tmp_path / "config.py"
    path.write_text(f"raise Exception('must not execute')\nWEBUI_CONFIG = {{'host': 'old', 'port': 3090, 'token': {token!r}}}")
    assert restored_endpoint(path) == {"health_host": "::1", "health_port": 3210, "health_token": expected}


def test_proxy_ipv6_and_encoded_credentials_roundtrip():
    settings = resolve_proxy_settings("http://a%40b:p%3A%2F%23%25@[::1]:7890")
    assert settings.server == "http://a%40b:p%3A%2F%23%25@[::1]:7890"
    assert (settings.host, settings.username, settings.password) == ("::1", "a@b", "p:/#%")


def test_proxy_alias_supports_ipv6(monkeypatch):
    import config
    from core.proxy_utils import _build_proxy_aliases

    monkeypatch.setattr(config, 'PROXY_ALIAS_CONFIG', {'host': '::1', 'http_port': 7890}, raising=False)
    assert resolve_proxy_settings(_build_proxy_aliases()['clash']).server == 'http://[::1]:7890'


@pytest.mark.parametrize("source", [
    "OOPZ_CONFIG = {'keep': 1} | {'proxy': ''}",
    "other = {'keep': 1}\nOOPZ_CONFIG = other",
    "OOPZ_CONFIG = dict(proxy='')",
    "OOPZ_CONFIG = {'keep': 1}\nOOPZ_CONFIG = dict(proxy='')",
    "OOPZ_CONFIG = {**{'keep': 1}, 'proxy': ''}",
    "key = 'keep'\nOOPZ_CONFIG = {key: 'keep', 'proxy': ''}",
    "OOPZ_CONFIG = {}\nOOPZ_CONFIG |= {'keep': 1}",
    "OOPZ_CONFIG = {'nested': {**{'keep': 1}}}",
    "if True:\n    OOPZ_CONFIG = {'jwt_token': 'keep'}",
    "OOPZ_CONFIG = {}\nif True:\n    OOPZ_CONFIG = {'jwt_token': 'keep'}",
    "OOPZ_CONFIG, other = {'jwt_token': 'keep'}, 1",
    "from types import SimpleNamespace as OOPZ_CONFIG",
    "import types as OOPZ_CONFIG",
    "OOPZ_CONFIG = {}\nOOPZ_CONFIG.update(jwt_token='keep')",
    "OOPZ_CONFIG = {}\nOOPZ_CONFIG['jwt_token'] = 'keep'",
    "alias = OOPZ_CONFIG = {'jwt_token': 'keep'}",
    "OOPZ_CONFIG = {}\nalias = OOPZ_CONFIG",
    "def OOPZ_CONFIG():\n    return {'jwt_token': 'keep'}",
])
def test_unsafe_config_definition_rejected_before_save(tmp_path, monkeypatch, source):
    path = tmp_path / 'config.py'
    path.write_text(source)
    runtime = SimpleNamespace(OOPZ_CONFIG={'proxy': 'old', 'keep': 1})
    monkeypatch.setattr(editor, 'CONFIG_PATH', str(path))
    monkeypatch.setattr(editor, '_runtime_config', lambda: runtime)
    with pytest.raises(RuntimeError):
        editor.apply_updates({'oopz': {'proxy': 'direct'}})
    assert path.read_text() == source
    assert runtime.OOPZ_CONFIG == {'proxy': 'old', 'keep': 1}


def test_config_references_rejected_without_execution(tmp_path, monkeypatch):
    marker = tmp_path / 'executed'
    path = tmp_path / 'config.py'
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\nif True:\n    OOPZ_CONFIG = {{'jwt_token': 'keep'}}"
    path.write_text(source)
    monkeypatch.setattr(editor, 'CONFIG_PATH', str(path))
    with pytest.raises(RuntimeError):
        editor.apply_updates({'oopz': {'proxy': 'direct'}})
    assert path.read_text() == source
    assert not marker.exists()


def test_unrelated_dynamic_group_does_not_block_safe_target():
    source = "if True:\n    WEBUI_CONFIG = {}\nOOPZ_CONFIG = {'jwt_token': 'keep'}\n"
    namespace = {}
    exec(patch_config_text(source, {'oopz': {'proxy': 'direct'}}, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH), namespace)
    assert namespace['OOPZ_CONFIG'] == {'jwt_token': 'keep', 'proxy': 'direct'}


@pytest.mark.parametrize('source', ["OOPZ_CONFIG: dict = {'keep': 1}\n", "OTHER = {}\n"])
def test_annotated_or_missing_config_group_preserves_fields(source):
    patched = patch_config_text(source, {'oopz': {'proxy': 'direct'}}, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    namespace = {}
    exec(patched, namespace)
    assert namespace['OOPZ_CONFIG']['proxy'] == 'direct'
    if 'keep' in source:
        assert namespace['OOPZ_CONFIG']['keep'] == 1


@pytest.mark.parametrize('body,status', [('broken', 400), ('[]', 400), ('null', 400), ('"text"', 400), ('x' * 100, 413), ('', 200)])
def test_memory_delete_rejects_bad_body_without_erasing_memory(tmp_path, body, status):
    async def run():
        memory = MemoryStore(str(tmp_path / 'memory.db'))
        memory.append('user', 'retain me', user_key='member')
        runtime = SimpleNamespace(agent=SimpleNamespace(memory=memory))
        app = web.Application(client_max_size=64)
        app.add_routes(build_voice_routes(runtime))
        async with TestClient(TestServer(app)) as client:
            response = await client.delete('/memory', data=body)
            assert response.status == status
            assert bool(memory.recent()) == (status != 200)
    asyncio.run(run())


def test_memory_delete_propagates_cancellation(tmp_path):
    class CancelledRequest:
        can_read_body = True

        async def json(self):
            raise asyncio.CancelledError

    async def run():
        memory = MemoryStore(str(tmp_path / 'memory.db'))
        memory.append('user', 'retain me', user_key='member')
        runtime = SimpleNamespace(agent=SimpleNamespace(memory=memory))
        route = next(route for route in build_voice_routes(runtime)
                     if route.method == 'DELETE' and route.path == '/memory')
        with pytest.raises(asyncio.CancelledError):
            await route.handler(CancelledRequest())
        assert memory.recent()[0]['content'] == 'retain me'
    asyncio.run(run())


def test_memory_delete_accepts_empty_chunked_body(tmp_path):
    async def empty_body():
        if False:
            yield b''

    async def run():
        memory = MemoryStore(str(tmp_path / 'memory.db'))
        memory.append('user', 'remove me', user_key='member')
        app = web.Application()
        app.add_routes(build_voice_routes(SimpleNamespace(agent=SimpleNamespace(memory=memory))))
        async with TestClient(TestServer(app)) as client:
            response = await client.delete('/memory', data=empty_body())
            assert response.status == 200
            assert not memory.recent()
    asyncio.run(run())
