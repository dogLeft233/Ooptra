"""真实OS变更锁、配置事务和计划落盘的事件循环/取消边界。"""
import os
import subprocess
import sys
from pathlib import Path


def test_plan_during_runtime_config_application_never_blocks_event_loop(tmp_path):
    script = r"""
import asyncio
import sys
from pathlib import Path
from operations import configuration_transaction, create_maintenance
from operations.models import MaintenanceRequest

async def run():
    root = Path(sys.argv[1])
    (root / 'config.py').write_text('VALUE = 1\n')
    service = create_maintenance(root, managed=True, runtime_control=object())
    backup = await service.create_backup()
    saved, finish = asyncio.Event(), asyncio.Event()
    async def apply():
        saved.set()
        await finish.wait()
    transaction = asyncio.create_task(configuration_transaction(root, apply))
    await saved.wait()
    try:
        try:
            await asyncio.wait_for(service.plan(MaintenanceRequest('restore', backup_id=backup['id'])), 1)
        except ValueError as exc:
            assert '进行中' in str(exc)
        else:
            raise AssertionError('busy configuration gate must reject plan')
    finally:
        finish.set()
        await transaction
        await service.close()
asyncio.run(run())
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
                   "PYTHONUTF8": "1"}
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            capture_output=True, text=True, encoding="utf-8", env=environment, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr


def test_cancelled_plan_waits_for_actual_persistence_before_returning(tmp_path, monkeypatch):
    import asyncio
    import threading

    import pytest

    from operations import create_maintenance
    from operations._internal.deployment import service as implementation
    from operations.models import MaintenanceRequest

    async def run():
        (tmp_path / "config.py").write_text("VALUE = 1\n")
        service = create_maintenance(tmp_path, managed=True, runtime_control=object())
        backup = await service.create_backup()
        entered, release = threading.Event(), threading.Event()
        original = implementation.atomic_json
        def write(path, value):
            if path.parent.name == "plans":
                entered.set()
                assert release.wait(3)
            return original(path, value)
        monkeypatch.setattr(implementation, "atomic_json", write)
        pending = asyncio.create_task(service.plan(MaintenanceRequest("restore", backup_id=backup["id"])))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await service.close()
        assert len(list((tmp_path / "data/maintenance/plans").glob("*.json"))) == 1
    asyncio.run(run())
