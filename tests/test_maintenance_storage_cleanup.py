import asyncio
import json
import os
import sys

import pytest

from operations import create_backup_store as BackupStore
from tests.support.maintenance_harness import MaintenanceService


def candidate(root, name, content=b"retired"):
    path = root / name
    path.mkdir(parents=True)
    (path / "asset").write_bytes(content)
    return path


def test_backup_pagination_reports_all_backups_and_storage(tmp_path):
    store = BackupStore(tmp_path)
    for index in range(53):
        store.path(f"{index:032x}").write_bytes(b"123")
    service = MaintenanceService(tmp_path)
    result = service.status(page=6, page_size=10)
    assert len(result["backups"]) == 3
    assert result["backup_page"] == {"page": 6, "page_size": 10, "pages": 6, "total": 53}
    assert result["storage"]["backups_bytes"] == 159
    assert result["storage"]["backups_count"] == 53
    last = service.status(page=999, page_size=10)
    assert last["backup_page"]["page"] == 6
    assert len(last["backups"]) == 3


def test_cleanup_requires_preview_and_preserves_runtime_and_rollback(tmp_path, monkeypatch):
    old = candidate(tmp_path, ".venv-update-" + "a" * 32)
    live = candidate(tmp_path, ".venv-update-" + "b" * 32)
    browser = candidate(tmp_path, "data/maintenance/browsers/" + "b" * 32)
    rollback = candidate(tmp_path, ".venv-update-" + "c" * 32)
    staging = candidate(tmp_path, "data/maintenance/staging/abandoned")
    other = candidate(tmp_path, "data/user-files")
    monkeypatch.setattr(sys, "executable", str(live / "Scripts/python.exe"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browser))
    service = MaintenanceService(tmp_path)
    safety = service.backups.create()["id"]
    service.write_job({"id": "done", "phase": "complete", "old_python": str(rollback / "Scripts/python.exe"), "safety_backup_id": safety})
    with pytest.raises(ValueError):
        asyncio.run(service.apply_cleanup("unpreviewed"))
    preview = service.preview_cleanup()
    paths = {row["path"] for row in preview["items"]}
    assert old.relative_to(tmp_path).as_posix() in paths
    assert staging.relative_to(tmp_path).as_posix() in paths
    assert live.relative_to(tmp_path).as_posix() not in paths
    assert browser.relative_to(tmp_path).as_posix() not in paths
    assert rollback.relative_to(tmp_path).as_posix() not in paths
    assert service.backups.path(safety).relative_to(tmp_path).as_posix() not in paths
    assert old.exists() and staging.exists()  # Preview never deletes.
    result = asyncio.run(service.apply_cleanup(preview["token"]))
    assert result["removed"] == len(preview["items"])
    assert not old.exists() and not staging.exists()
    assert live.exists() and browser.exists() and rollback.exists() and other.exists()
    with pytest.raises(ValueError):
        asyncio.run(service.apply_cleanup(preview["token"]))


@pytest.mark.parametrize("change", ["content", "active", "runtime"])
def test_cleanup_revalidates_preview_before_any_deletion(tmp_path, change):
    retired = candidate(tmp_path, ".venv-update-" + "a" * 32)
    service = MaintenanceService(tmp_path)
    preview = service.preview_cleanup()
    if change == "content":
        (retired / "asset").write_bytes(b"new content")
    elif change == "active":
        service.write_job({"id": "fixture", "phase": "checking", "service_pid": 1, "new_python": str(retired / "Scripts/python.exe")})
    else:
        (tmp_path / "data/maintenance/runtime.json").write_text(json.dumps({"python": str(retired / "Scripts/python.exe")}))
    with pytest.raises(ValueError):
        asyncio.run(service.apply_cleanup(preview["token"]))
    assert retired.exists()


def test_cleanup_preserves_every_backup_referenced_by_latest_job(tmp_path):
    service = MaintenanceService(tmp_path)
    backups = [service.backups.create()["id"] for _ in range(5)]
    for index, backup in enumerate(backups):
        os.utime(service.backups.path(backup), (1700000000 + index, 1700000000 + index))
    service.write_job({"id": "fixture", "phase": "failed", "backup_id": backups[0], "safety_backup_id": backups[1]})
    preview = service.preview_cleanup()
    assert {row["id"] for row in preview["items"] if row["kind"] == "backup"} == {backups[2]}


def test_unreadable_runtime_record_blocks_cleanup_conservatively(tmp_path):
    retired = candidate(tmp_path, ".venv-update-" + "a" * 32)
    service = MaintenanceService(tmp_path)
    (tmp_path / "data/maintenance/runtime.json").write_text("{")
    with pytest.raises(ValueError):
        service.preview_cleanup()
    assert retired.exists()
    assert "error" in asyncio.run(service.query("storage"))["storage"]


def test_expired_cleanup_preview_leaves_every_candidate_intact(tmp_path, monkeypatch):
    from tests.support.maintenance_harness import maintenance
    retired = candidate(tmp_path, ".venv-update-" + "a" * 32)
    service = MaintenanceService(tmp_path)
    preview = service.preview_cleanup()
    monkeypatch.setattr(maintenance.time, "time", lambda: preview["expires_at"] + 1)
    with pytest.raises(ValueError):
        asyncio.run(service.apply_cleanup(preview["token"]))
    assert retired.exists()


def test_storage_query_reuses_scan_but_new_backup_invalidates_it(tmp_path, monkeypatch):
    from tests.support.maintenance_harness import maintenance
    original = maintenance.inventory
    scans = []
    def tracked(*args):
        scans.append(True)
        return original(*args)
    monkeypatch.setattr(maintenance, "inventory", tracked)
    service = MaintenanceService(tmp_path)
    first = asyncio.run(service.query("storage"))["storage"]
    assert service.status()["storage"] == first
    assert asyncio.run(service.query("storage"))["storage"] == first
    assert len(scans) == 1  # A three-second page poll must not walk every venv again.
    asyncio.run(service.create_backup())
    assert asyncio.run(service.query("storage"))["storage"]["backups_bytes"] > first["backups_bytes"]
    assert len(scans) == 2


def test_cleanup_always_scans_live_state_even_with_cached_storage_totals(tmp_path):
    retired = candidate(tmp_path, ".venv-update-" + "a" * 32)
    service = MaintenanceService(tmp_path)
    service.status()
    (tmp_path / "data/maintenance/runtime.json").write_text(json.dumps({"python": str(retired / "Scripts/python.exe")}))
    preview = service.preview_cleanup()
    assert not preview["items"]
    assert retired.exists()
