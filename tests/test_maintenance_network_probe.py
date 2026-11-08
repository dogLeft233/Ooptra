import asyncio
import subprocess

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tests.support.maintenance_harness import MaintenanceService, mount_maintenance_routes
from tests.support.maintenance_harness import maintenance_network as network


def test_probe_checks_actual_git_ref_without_fetch_or_saving(tmp_path):
    mirror = "https://gh-proxy.com/https://github.com/Eason4869/Ooptra.git"
    calls = []

    def run(root, args, timeout, env):
        calls.append(args)
        assert root != tmp_path
        assert timeout == 15
        assert args == ["git", "ls-remote", "--", mirror, "refs/heads/dev"]
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        return "a" * 40 + "\trefs/heads/dev"

    result = network.test_mirror_connection(mirror, "dev", run, proxy="direct")
    assert result["ok"] and result["elapsed_ms"] >= 0
    assert len(calls) == 1
    assert "官方" in result["detail"]


@pytest.mark.parametrize("output", ["<html>200 OK</html>", "a" * 40 + "\trefs/heads/main", "bogus\trefs/heads/dev"])
def test_probe_rejects_download_only_or_wrong_branch_nodes(output):
    with pytest.raises(ValueError, match="测试失败"):
        network.test_mirror_connection("https://node.example/repo.git", "dev", lambda *args, **kwargs: output, proxy="direct")


@pytest.mark.parametrize("mirror,channel", [("http://node.example/repo.git", "dev"), ("https://user:secret@node.example/repo.git", "dev"), ("https://node.example/repo.git", "arbitrary")])
def test_probe_validates_before_any_network(mirror, channel):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid request must not run Git")
    with pytest.raises(ValueError) as error:
        network.test_mirror_connection(mirror, channel, forbidden, proxy="direct")
    assert "secret" not in str(error.value)


def test_probe_timeout_does_not_expose_git_error():
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("secret", 15)
    with pytest.raises(ValueError, match="15 秒") as error:
        network.test_mirror_connection("https://node.example/repo.git", "dev", run, proxy="direct")
    assert "secret" not in str(error.value)


def test_disabled_accelerator_can_test_manual_proxy_or_direct():
    seen = []

    def run(root, args, timeout, env):
        assert args[3] == network.OFFICIAL_URL
        pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
        seen.append(pairs["http.proxy"])
        return "a" * 40 + "\trefs/heads/main"
    network.test_mirror_connection("", "main", run, proxy="http://127.0.0.1:7890")
    network.test_mirror_connection("", "main", run, proxy="direct")
    assert seen == ["http://127.0.0.1:7890", ""]


def test_mirror_fetch_honors_the_proxy_used_by_probe(tmp_path):
    import time
    seen = []
    mirror = "https://node.example/repo.git"

    def run(root, args, timeout, env=None):
        if args[1] == "fetch":
            if args[-2] == network.OFFICIAL_URL:
                raise ValueError("unreachable")
            pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
            seen.append(pairs["http.proxy"])
            assert pairs["http.proxy"] == "http://127.0.0.1:7890"
            return ""
        return "a" * 40
    network.fetch_verified(tmp_path, "dev", "a" * 40, time.monotonic() + 120, run,
                           settings=network.NetworkSettings("http://127.0.0.1:7890", mirror))
    assert seen == ["http://127.0.0.1:7890"]


def test_probe_route_json_origin_and_does_not_modify_settings(tmp_path, monkeypatch):
    from tests.support.maintenance_harness import maintenance
    seen = []

    def probe(mirror, channel, run):
        seen.append((mirror, channel))
        return {"ok": True, "elapsed_ms": 12, "detail": "Git readable"}
    monkeypatch.setattr(maintenance, "test_mirror_connection", probe)

    async def scenario():
        app = web.Application()
        mount_maintenance_routes(app, MaintenanceService(tmp_path))
        async with TestClient(TestServer(app)) as client:
            path = "/api/maintenance/network/test"
            assert (await client.post(path, json={}, headers={"Origin": "https://bad.example"})).status == 400
            assert (await client.post(path, data="{}")).status == 400
            response = await client.post(path, json={"mirror": "https://node.example/repo.git", "channel": "beta"})
            assert response.status == 200
            assert (await response.json())["elapsed_ms"] == 12
    asyncio.run(scenario())
    assert seen == [("https://node.example/repo.git", "beta")]
    assert not (tmp_path / "config.py").exists()
