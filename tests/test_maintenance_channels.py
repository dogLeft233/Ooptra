import asyncio

import pytest
from test_maintenance_update import git

from operations import write_maintenance_record as atomic_json
from tests.support.maintenance_harness import maintenance


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / ".gitignore").write_text("data/\n")
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-m", "initial")
    git(tmp_path, "checkout", "-b", "beta")
    git(tmp_path, "remote", "add", "origin", "https://github.com/Eason4869/Ooptra.git")
    return tmp_path


def remote(monkeypatch, sha):
    original = maintenance.run_command

    def run(root, args, timeout=30, env=None):
        if args[:2] == ["git", "ls-remote"]:
            return sha + "\t" + args[-1]
        return original(root, args, timeout, env)

    monkeypatch.setattr(maintenance, "run_command", run)
    from tests.support.maintenance_harness import maintenance_network
    def unavailable_api(*args):
        raise ValueError("offline test fixture")
    monkeypatch.setattr(maintenance_network, "api_lookup", unavailable_api)


def test_beta_is_a_distinct_installed_channel(repo):
    status = maintenance.MaintenanceService(repo).status()
    assert status["update"]["current_channel"] == "beta"
    assert status["update"]["channel"] == "beta"


def test_detached_beta_keeps_channel_after_restore_job_replaces_update(repo):
    git(repo, "checkout", "--detach")
    atomic_json(repo / "data/maintenance/runtime.json", {"channel": "beta"})
    service = maintenance.MaintenanceService(repo)
    service.write_job({"id": "restore", "action": "restore", "phase": "complete"})
    assert service.status()["update"]["current_channel"] == "beta"


def test_check_defaults_to_installed_beta_and_reports_no_change(repo, monkeypatch):
    remote(monkeypatch, git(repo, "rev-parse", "HEAD"))
    result = asyncio.run(maintenance.MaintenanceService(repo).check_update())
    assert result["channel"] == "beta"
    assert result["action"] == "current"
    assert not result["available"]


def test_cross_channel_check_preserves_current_channel_and_requires_confirmation(repo, monkeypatch):
    remote(monkeypatch, "a" * 40)
    service = maintenance.MaintenanceService(repo)
    result = asyncio.run(service.check_update("dev"))
    assert result["action"] == "switch"
    assert result["requires_confirmation"] is True
    assert service.status()["update"]["current_channel"] == "beta"
    with pytest.raises(ValueError, match="确认"):
        asyncio.run(service.start_update("dev", "a" * 40))
    assert not service.read_job()


def test_confirmed_cross_channel_install_creates_bound_job(repo, monkeypatch):
    remote(monkeypatch, "a" * 40)
    service = maintenance.MaintenanceService(repo, managed=True, venv=True)

    async def run():
        await service.check_update("dev")
        result = await service.start_update("dev", "a" * 40, confirm_channel_switch=True)
        assert result["channel"] == "dev"
        await service.close()

    async def prepare(job):
        service.write_job({**job, "phase": "complete"})

    monkeypatch.setattr(service, "_prepare", prepare)
    asyncio.run(run())


def test_head_changed_since_check_requires_recheck(repo, monkeypatch):
    remote(monkeypatch, "a" * 40)
    service = maintenance.MaintenanceService(repo, managed=True, venv=True)
    asyncio.run(service.check_update("beta"))
    (repo / "file").write_text("change")
    git(repo, "add", "file")
    git(repo, "commit", "-m", "changed")
    with pytest.raises(ValueError, match="重新检查"):
        asyncio.run(service.start_update("beta", "a" * 40))
    assert not service.read_job()


def test_no_update_never_starts_deployment(repo, monkeypatch):
    sha = git(repo, "rev-parse", "HEAD")
    remote(monkeypatch, sha)
    service = maintenance.MaintenanceService(repo, managed=True, venv=True)
    asyncio.run(service.check_update("beta"))
    with pytest.raises(ValueError, match="最新"):
        asyncio.run(service.start_update("beta", sha))
    assert not service.read_job()


def test_failed_recheck_invalidates_previous_target(repo, monkeypatch):
    remote(monkeypatch, "a" * 40)
    service = maintenance.MaintenanceService(repo)
    asyncio.run(service.check_update("beta"))
    remote(monkeypatch, "invalid")
    with pytest.raises(ValueError):
        asyncio.run(service.check_update("dev"))
    assert service.status()["check"] is None


def test_preflight_detects_git_metadata_write_denied(repo, monkeypatch):
    service = maintenance.MaintenanceService(repo, managed=True, venv=True)
    original = maintenance.tempfile.TemporaryFile

    def temporary(*args, **kwargs):
        if ".git" in str(kwargs.get("dir", "")):
            raise PermissionError("denied")
        return original(*args, **kwargs)

    monkeypatch.setattr(maintenance.tempfile, "TemporaryFile", temporary)
    result = service.preflight()
    assert not result["supported"]
    assert any(row["id"] == "writable" and not row["passed"] for row in result["checks"])


@pytest.mark.parametrize("replacement", [None, "same_channel"])
def test_concurrent_recheck_cannot_bypass_channel_confirmation(repo, monkeypatch, replacement):
    remote(monkeypatch, "a" * 40)
    service = maintenance.MaintenanceService(repo, managed=True, venv=True)
    original = maintenance.asyncio.to_thread

    async def run():
        await service.check_update("dev")
        checked = dict(service._check)
        paused, resume = asyncio.Event(), asyncio.Event()

        async def to_thread(function, *args, **kwargs):
            if function == service.installed:
                paused.set()
                await resume.wait()
            return await original(function, *args, **kwargs)

        monkeypatch.setattr(maintenance.asyncio, "to_thread", to_thread)
        task = asyncio.create_task(service.start_update("dev", "a" * 40))
        await paused.wait()
        service._check = None if replacement is None else {**checked, "channel": "beta", "requires_confirmation": False}
        resume.set()
        with pytest.raises(ValueError, match="确认"):
            await task
        assert service.read_job() is None

    asyncio.run(run())
