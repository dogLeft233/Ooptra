import asyncio
import json
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tests.support.maintenance_harness import MaintenanceService, mount_maintenance_routes
from webui.server import WebUIConsole


def test_manual_delete_allows_newest_and_invalidates_cleanup(tmp_path):
    service = MaintenanceService(tmp_path)
    backup = service.backups.create()["id"]
    preview = service.preview_cleanup()
    service.status()
    assert service.status()["backups"][0]["can_delete"]
    result = asyncio.run(service.delete_backup(backup))
    assert result["deleted_id"] == backup
    assert not service.backups.path(backup).exists()
    assert not service._busy
    assert service.status()["storage"]["backups_count"] == 0
    with pytest.raises(ValueError, match="失效"):
        asyncio.run(service.apply_cleanup(preview["token"]))


@pytest.mark.parametrize("field", ["backup_id", "safety_backup_id"])
@pytest.mark.parametrize("phase", ["complete", "failed", "rolled_back"])
def test_delete_protects_update_restore_and_rollback_references(tmp_path, field, phase):
    service = MaintenanceService(tmp_path)
    backup = service.backups.create()["id"]
    service.write_job({"phase": phase, field: backup})
    row = service.status()["backups"][0]
    assert not row["can_delete"] and row["delete_reason"]
    with pytest.raises(ValueError, match="引用"):
        asyncio.run(service.delete_backup(backup))
    assert service.backups.path(backup).is_file()
    assert not service._busy


@pytest.mark.parametrize("record", ["{", "[]", "null"])
def test_unreadable_job_blocks_delete(tmp_path, record):
    service = MaintenanceService(tmp_path)
    backup = service.backups.create()["id"]
    service.job_file.write_text(record, encoding="utf-8")
    assert not service.status()["backups"][0]["can_delete"]
    with pytest.raises(ValueError, match="记录"):
        asyncio.run(service.delete_backup(backup))
    assert service.backups.path(backup).exists()
    assert not service._busy


def test_busy_invalid_missing_and_directory_backups_are_not_deleted(tmp_path):
    service = MaintenanceService(tmp_path)
    backup = service.backups.create()["id"]
    service._busy = True
    with pytest.raises(ValueError, match="进行中"):
        asyncio.run(service.delete_backup(backup))
    service._busy = False
    service.write_job({"phase": "checking", "service_pid": 1})
    with pytest.raises(ValueError, match="进行中"):
        asyncio.run(service.delete_backup(backup))
    service.write_job({"phase": "complete"})
    for invalid in ["../config.py", "A" * 32, "", "b" * 32]:
        with pytest.raises(ValueError):
            asyncio.run(service.delete_backup(invalid))
        assert not service._busy
    directory = service.backups.path("c" * 32)
    directory.mkdir()
    with pytest.raises(ValueError):
        asyncio.run(service.delete_backup("c" * 32))
    assert directory.is_dir() and service.backups.path(backup).exists()


def test_delete_last_page_clamps_and_preserves_other_files(tmp_path):
    service = MaintenanceService(tmp_path)
    unrelated = tmp_path / "config.py"
    unrelated.write_text("KEEP = True", encoding="utf-8")
    for index in range(11):
        service.backups.path(f"{index:032x}").write_bytes(b"fixture")
    old_page = service.status(page=2, page_size=10)
    asyncio.run(service.delete_backup(old_page["backups"][0]["id"]))
    page = service.status(page=2, page_size=10)
    assert page["backup_page"] == {"page": 1, "page_size": 10, "total": 10, "pages": 1}
    assert unrelated.read_text(encoding="utf-8") == "KEEP = True"


def test_delete_routes_require_auth_same_origin_and_json(tmp_path):
    async def run():
        console = WebUIConsole(None, None)
        console._token = "test-token"
        service = MaintenanceService(tmp_path)
        backup = service.backups.create()["id"]
        app = web.Application(middlewares=[console._auth_middleware])
        mount_maintenance_routes(app, service)
        path = "/api/maintenance/backups/" + backup
        auth = {"Authorization": "Bearer test-token"}
        async with TestClient(TestServer(app)) as client:
            assert (await client.delete(path, json={})).status == 401
            assert (await client.delete(path, json={}, headers={**auth, "Origin": "https://other.example"})).status == 400
            assert (await client.delete(path, data="{}", headers=auth)).status == 400
            assert service.backups.path(backup).exists()
            response = await client.delete(path, json={}, headers=auth)
            assert response.status == 200
            assert (await response.json())["deleted_id"] == backup
            assert (await client.delete(path, json={}, headers=auth)).status == 400
            assert not service.backups.path(backup).exists()
    asyncio.run(run())


def test_delete_rechecks_job_after_display(tmp_path):
    service = MaintenanceService(tmp_path)
    backup = service.backups.create()["id"]
    assert service.status()["backups"][0]["can_delete"]
    service.job_file.write_text(json.dumps({"phase": "complete", "backup_id": backup}), encoding="utf-8")
    with pytest.raises(ValueError, match="引用"):
        asyncio.run(service.delete_backup(backup))
    assert service.backups.path(backup).exists()


def test_delete_rejects_symlink_backup_and_replaced_parent(tmp_path):
    service = MaintenanceService(tmp_path)
    target = tmp_path / "keep.zip"
    target.write_bytes(b"keep")
    link = service.backups.directory / ("a" * 32 + ".zip")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlink privilege unavailable")
    with pytest.raises(ValueError):
        asyncio.run(service.delete_backup("a" * 32))
    assert target.read_bytes() == b"keep"
    link.unlink()
    original = service.backups.directory
    moved = tmp_path / "saved-backups"
    original.rename(moved)
    (moved / ("b" * 32 + ".zip")).write_bytes(b"keep")
    original.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="链接"):
        asyncio.run(service.delete_backup("b" * 32))
    assert (moved / ("b" * 32 + ".zip")).read_bytes() == b"keep"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_delete_rejects_windows_junction_without_admin(tmp_path):
    import _winapi

    service = MaintenanceService(tmp_path)
    original = service.backups.directory
    moved = tmp_path / "saved-backups"
    original.rename(moved)
    target = moved / ("d" * 32 + ".zip")
    target.write_bytes(b"keep")
    _winapi.CreateJunction(str(moved), str(original))
    with pytest.raises(ValueError, match="联接"):
        asyncio.run(service.delete_backup("d" * 32))
    assert target.read_bytes() == b"keep"
