import asyncio
import json
import subprocess

import pytest


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def test_preflight_rejects_dirty_repo_and_wrong_remote(tmp_path):
    from operations import create_maintenance as MaintenanceService

    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "main.py").write_text("pass")
    git(tmp_path, "add", "main.py")
    git(tmp_path, "commit", "-m", "initial")
    git(tmp_path, "remote", "add", "origin", "https://github.com/other/project.git")
    service = MaintenanceService(tmp_path, managed=True, venv=True)
    assert not service.preflight()["supported"]
    git(tmp_path, "remote", "set-url", "origin", "https://github.com/Eason4869/Ooptra.git")
    assert service.preflight()["supported"]
    (tmp_path / "main.py").write_text("changed")
    assert not service.preflight()["supported"]


def test_unsupported_install_never_starts_shutdown(tmp_path):
    from operations import create_maintenance as MaintenanceService

    async def run():
        service = MaintenanceService(tmp_path, managed=False, venv=False)
        with pytest.raises(ValueError):
            await service.start_update("dev", "a" * 40)
        assert service.status()["job"] is None
    asyncio.run(run())


def test_public_status_never_returns_health_token(tmp_path):
    from operations import create_maintenance as MaintenanceService

    service = MaintenanceService(tmp_path)
    service.write_job({"id": "test", "phase": "preparing", "health_token": "secret", "target_sha": "a" * 40})
    assert "secret" not in json.dumps(service.status())


@pytest.mark.parametrize("phase", ["awaiting_restart", "switching", "checking", "rolling_back"])
def test_stale_jobs_remain_restricted_until_explicit_recovery(tmp_path, phase):
    from operations import create_maintenance as MaintenanceService

    initial = MaintenanceService(tmp_path)
    initial.write_job({"id": "old", "phase": phase, "service_pid": -1})
    recovered = MaintenanceService(tmp_path)
    assert recovered.status()["job"]["phase"] == phase
    from operations import deployment_access
    assert not deployment_access(tmp_path).allowed
    with pytest.raises(ValueError, match="维护"):
        recovered._claim()


def test_restored_webui_endpoint_is_extracted_without_executing_config(tmp_path):
    from operations import restored_endpoint

    path = tmp_path / "config.py"
    path.write_text("raise Exception('never execute')\nWEBUI_CONFIG = {'host': ' 0.0.0.0 ', 'port': 3199, 'token': ' restored '}")
    assert restored_endpoint(path) == {"health_host": "127.0.0.1", "health_port": 3199, "health_token": "restored"}
