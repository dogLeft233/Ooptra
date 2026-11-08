"""维护公开边界：有效更新目标，以及真实归档可校验、可恢复。"""

import pytest

from operations import create_backup_store, create_maintenance
from operations.models import StoppedDeployment, UpdateTarget


def test_update_target_retains_confirmed_channel_and_commit():
    target = UpdateTarget.parse("beta", "a" * 40)
    assert target.channel == "beta"
    assert target.commit == "a" * 40
    with pytest.raises(ValueError, match="更新参数无效"):
        UpdateTarget.parse("unknown", "a" * 40)
    with pytest.raises(ValueError, match="更新参数无效"):
        UpdateTarget.parse("main", 123)


def test_public_archive_restores_real_data_and_configuration(tmp_path):
    config = tmp_path / "config.py"
    config.write_text("WEBUI_CONFIG = {'enabled': True}\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    memory = data / "conversation.jsonl"
    memory.write_text('original history\n', encoding="utf-8")
    archive = create_backup_store(tmp_path)
    receipt = archive.create()
    stage = tmp_path / "validated"
    stage.mkdir()
    assert "config.py" in archive.validate(receipt["id"], stage)
    memory.write_text("new history\n", encoding="utf-8")
    config.write_text("modified = True\n", encoding="utf-8")
    archive.restore(archive.inspect(receipt["id"]), StoppedDeployment("isolated", 1, "test", "stopped"))
    assert memory.read_text(encoding="utf-8") == "original history\n"
    assert config.read_text(encoding="utf-8") == "WEBUI_CONFIG = {'enabled': True}\n"


def test_maintenance_factory_owns_backup_results(tmp_path):
    (tmp_path / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
    service = create_maintenance(tmp_path, managed=False)
    import asyncio
    receipt = asyncio.run(service.create_backup())
    assert service.status()["backups"][0]["id"] == receipt["id"]


def test_cancelled_backup_keeps_mutation_exclusive_until_io_finishes(tmp_path, monkeypatch):
    import asyncio
    import threading
    import zipfile

    entered, release = threading.Event(), threading.Event()
    original = zipfile.ZipFile.writestr

    def blocked_write(archive, name, content, *args, **kwargs):
        if name == "config.py":
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test archive I/O was not released")
        return original(archive, name, content, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", blocked_write)
    (tmp_path / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
    service = create_maintenance(tmp_path, managed=False)

    async def run():
        task = asyncio.create_task(service.create_backup())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            with pytest.raises(ValueError, match="维护任务进行中"):
                await service.create_backup()
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(service.status()["backups"]) == 1
        await service.close()

    asyncio.run(run())
