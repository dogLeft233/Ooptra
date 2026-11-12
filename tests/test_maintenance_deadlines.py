import asyncio
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tests.support.maintenance_harness import maintenance, maintenance_cleanup, maintenance_network


def test_backup_status_does_not_wait_for_git_or_inventory(tmp_path, monkeypatch):
    service = maintenance.MaintenanceService(tmp_path)
    backup = service.backups.create()

    def blocked(*args, **kwargs):
        raise AssertionError("expensive read entered backup-list response")

    monkeypatch.setattr(maintenance, "run_command", blocked)
    monkeypatch.setattr(maintenance, "inventory", blocked)
    result = service.status()
    assert result["backups"][0]["id"] == backup["id"]
    assert result["update"]["version"]
    assert result["preflight"]["pending"] is True
    assert result["storage"]["pending"] is True
    assert result["storage"]["backups_count"] == 1


def test_git_reads_disable_interactive_credentials(tmp_path, monkeypatch):
    options = {}

    def run(args, **kwargs):
        options.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="safe", stderr="")

    monkeypatch.setattr(maintenance.subprocess, "run", run)
    assert maintenance.run_command(tmp_path, ["git", "rev-parse", "HEAD"]) == "safe"
    assert options["stdin"] == subprocess.DEVNULL
    assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert options["env"]["GCM_INTERACTIVE"] == "Never"


def test_update_check_uses_one_budget_for_local_and_remote_reads(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    service = maintenance.MaintenanceService(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(maintenance, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(maintenance_network, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    budgets = []

    def run(root, args, timeout=30, env=None):
        budgets.append((args[1], timeout))
        clock[0] += 2
        if args[1] == "rev-parse":
            return "a" * 40
        if args[1] == "branch":
            return "beta"
        return "b" * 40 + "\trefs/heads/beta"

    monkeypatch.setattr(maintenance, "run_command", run)
    result = asyncio.run(service.check_update("beta"))
    assert result["available"] is True
    assert budgets[-1][0] == "ls-remote"
    assert budgets[-1][1] <= 56
    assert all(timeout <= 4 for stage, timeout in budgets[:-1])


def test_preflight_has_total_deadline_and_reports_git_stage(tmp_path, monkeypatch):
    service = maintenance.MaintenanceService(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(maintenance, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))

    def run(root, args, timeout=30, env=None):
        clock[0] += 11
        return str(tmp_path)

    monkeypatch.setattr(maintenance, "run_command", run)
    result = service.preflight()
    assert result["supported"] is False
    assert any("超时" in item["detail"] for item in result["checks"])


def test_supplementary_query_times_out_without_spawning_duplicate_scan(tmp_path, monkeypatch):
    async def run():
        service = maintenance.MaintenanceService(tmp_path)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocked():
            calls.append(True)
            entered.set()
            release.wait(2)
            return {"supported": False, "checks": []}

        monkeypatch.setattr(service, "preflight", blocked)
        monkeypatch.setattr(maintenance, "QUERY_WAIT_SECONDS", 0.03, raising=False)
        app = web.Application()
        maintenance.mount_maintenance_routes(app, service)
        try:
            async with TestClient(TestServer(app)) as client:
                first = await client.get("/api/maintenance/preflight")
                assert first.status == 200
                assert (await first.json())["preflight"]["pending"] is True
                second = await client.get("/api/maintenance/preflight")
                assert (await second.json())["preflight"]["pending"] is True
                assert calls == [True]
                status = await (await client.get("/api/maintenance")).json()
                assert status["update"]["version"]
        finally:
            release.set()
            await service.close()

    asyncio.run(run())


def test_storage_deadline_never_returns_partial_cleanup_candidates(tmp_path, monkeypatch):
    folder = tmp_path / (".venv-update-" + "a" * 32)
    folder.mkdir()
    (folder / "file").write_bytes(b"keep")
    clock = [0]

    def now():
        clock[0] += 1
        return clock[0]

    monkeypatch.setattr(maintenance_cleanup, "time", SimpleNamespace(monotonic=now), raising=False)
    with pytest.raises(ValueError, match=r"超时|限制"):
        maintenance_cleanup.inventory(tmp_path, [], None, timeout=0.5)
    assert (folder / "file").exists()


def test_inflight_query_does_not_cache_results_after_job_changes(tmp_path, monkeypatch):
    async def run():
        service = maintenance.MaintenanceService(tmp_path)
        entered, release = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            release.wait(2)
            return {"supported": True, "checks": []}

        monkeypatch.setattr(service, "preflight", blocked)
        task = asyncio.create_task(service.query("preflight"))
        try:
            await asyncio.to_thread(entered.wait, 1)
            service.write_job({"id": "fixture", "phase": "preparing"})
        finally:
            release.set()
        result = await task
        assert result["preflight"]["pending"] is True
        assert service.status()["update"]["supported"] is False

    asyncio.run(run())


def test_update_response_deadline_does_not_publish_late_result(tmp_path, monkeypatch):
    async def run():
        service = maintenance.MaintenanceService(tmp_path)
        monkeypatch.setattr(maintenance, "CHECK_SECONDS", 0.02)
        monkeypatch.setattr(service, "installed", lambda **kwargs: ("a" * 40, "beta"))

        def slow_remote(root, args, timeout=30, env=None):
            time.sleep(0.05)
            return "b" * 40 + "\trefs/heads/beta"

        monkeypatch.setattr(maintenance, "run_command", slow_remote)
        with pytest.raises(ValueError, match=r"超.*秒"):
            await service.check_update("beta")
        with pytest.raises(ValueError, match="进行中"):
            await service.check_update("beta")
        await service.close()
        assert service.status()["check"] is None

    asyncio.run(run())


def test_non_git_install_can_check_official_update_without_enabling_install(tmp_path, monkeypatch):
    service = maintenance.MaintenanceService(tmp_path)

    def remote(root, args, timeout=30, env=None):
        if args[:2] != ["git", "ls-remote"]:
            raise ValueError("not a Git repository")
        return "a" * 40 + "\t" + args[-1]

    monkeypatch.setattr(maintenance, "run_command", remote)
    result = asyncio.run(service.check_update("beta"))
    assert result["target_sha"] == "a" * 40
    assert result["current_sha"] == ""
    assert result["installed_supported"] is False
    assert service.status()["update"]["supported"] is False


def test_storage_deadline_stops_enumerating_environment_candidates(tmp_path, monkeypatch):
    clock, enumerated = [0], []
    original = Path.glob

    def glob(path, pattern):
        if path != tmp_path or pattern != ".venv-update-*":
            yield from original(path, pattern)
            return
        for index in range(20):
            clock[0] += 2
            enumerated.append(index)
            yield path / (".venv-update-" + f"{index:032x}")

    monkeypatch.setattr(Path, "glob", glob)
    monkeypatch.setattr(maintenance_cleanup, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(ValueError, match="超时"):
        maintenance_cleanup.inventory(tmp_path, [], None, timeout=1)
    assert len(enumerated) <= 1


def test_storage_query_keeps_event_loop_and_timeout_alive_during_backup_read(tmp_path, monkeypatch):
    async def run():
        service = maintenance.MaintenanceService(tmp_path)
        release = threading.Event()
        monkeypatch.setattr(maintenance, "QUERY_WAIT_SECONDS", 0.03)

        def blocked_list(*args, **kwargs):
            release.wait(1)
            return []

        monkeypatch.setattr(service.backups, "list", blocked_list)
        started = time.monotonic()
        task = asyncio.create_task(service.query("storage"))
        try:
            await asyncio.sleep(0.01)
            assert time.monotonic() - started < 0.2
            result = await task
            assert result["storage"]["pending"] is True
            assert time.monotonic() - started < 0.2
        finally:
            release.set()
            await task
            await service.close()

    asyncio.run(run())
