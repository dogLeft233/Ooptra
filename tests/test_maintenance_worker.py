
import json

import pytest

from tests.support.maintenance_harness import maintenance_worker as worker
from tests.support.maintenance_harness import switch_stopped


class Process:
    pid = 123
    def poll(self):
        return None


@pytest.mark.parametrize("candidate_ok", [True, False])
def test_update_health_failure_restores_previous_code_data_and_python(tmp_path, monkeypatch, candidate_ok):
    (tmp_path / "config.py").write_text("OLD = True")
    calls = []
    monkeypatch.setattr(worker.subprocess, "check_output", lambda *a, **k: b"")
    monkeypatch.setattr(worker, "checkout", lambda root, sha: calls.append(("checkout", sha)))
    def spawn(root, python, marker, browser_path=""):
        calls.append(("spawn", python, marker))
        if python == "candidate":
            (tmp_path / "config.py").write_text("CHANGED = True")
        return Process()
    monkeypatch.setattr(worker, "spawn", spawn)
    monkeypatch.setattr(worker, "stop_owned", lambda process: calls.append(("stop", process.pid)))
    monkeypatch.setattr(worker, "healthy", lambda process, job: candidate_ok if sum(c[0] == "spawn" for c in calls) == 1 else True)
    job = {"id": "test", "action": "update", "old_sha": "old", "target_sha": "new", "old_python": "original",
           "new_python": "candidate", "old_channel": "beta", "channel": "dev"}
    assert switch_stopped(tmp_path, job) is not None
    if candidate_ok:
        assert job["phase"] == "complete"
        assert ("checkout", "old") not in calls
    else:
        assert job["phase"] == "rolled_back"
        assert ("checkout", "old") in calls
        assert ("spawn", "original", "test") in calls
        assert (tmp_path / "config.py").read_text() == "OLD = True"
    runtime = json.loads((tmp_path / "data/maintenance/runtime.json").read_text())
    assert runtime["channel"] == ("dev" if candidate_ok else "beta")


def test_restore_health_failure_keeps_pre_restore_data(tmp_path, monkeypatch):
    store = worker.BackupStore(tmp_path)
    (tmp_path / "config.py").write_text("SAVED = True")
    backup = store.create()
    (tmp_path / "config.py").write_text("CURRENT = True")
    monkeypatch.setattr(worker, "spawn", lambda *a: Process())
    monkeypatch.setattr(worker, "stop_owned", lambda *a: None)
    health = iter([False, True])
    monkeypatch.setattr(worker, "healthy", lambda *a: next(health))
    job = {"id": "test", "action": "restore", "old_python": "original", "backup_id": backup["id"]}
    assert switch_stopped(tmp_path, job) is not None
    assert job["phase"] == "rolled_back"
    assert (tmp_path / "config.py").read_text() == "CURRENT = True"


def test_restore_retains_current_release_channel(tmp_path, monkeypatch):
    (tmp_path / "config.py").write_text("SAVED = True")
    backup = worker.BackupStore(tmp_path).create()
    monkeypatch.setattr(worker, "spawn", lambda *a: Process())
    monkeypatch.setattr(worker, "healthy", lambda *a: True)
    job = {"id": "test", "action": "restore", "old_python": "original", "backup_id": backup["id"],
           "old_channel": "main", "channel": "dev"}
    assert switch_stopped(tmp_path, job) is not None
    runtime = json.loads((tmp_path / "data/maintenance/runtime.json").read_text())
    assert runtime["channel"] == "main"


def test_restore_checks_new_endpoint_and_rollback_checks_original(tmp_path, monkeypatch):
    store = worker.BackupStore(tmp_path)
    (tmp_path / "config.py").write_text("WEBUI_CONFIG = {'port': 3199, 'token': 'saved'}")
    backup = store.create()
    (tmp_path / "config.py").write_text("WEBUI_CONFIG = {'port': 3090, 'token': 'current'}")
    monkeypatch.setattr(worker, "spawn", lambda *a: Process())
    monkeypatch.setattr(worker, "stop_owned", lambda *a: None)
    seen = []
    def check(process, job):
        seen.append((job["health_port"], job["health_token"]))
        return len(seen) == 2
    monkeypatch.setattr(worker, "healthy", check)
    job = {"id": "test", "action": "restore", "old_python": "original", "backup_id": backup["id"],
           "health_port": 3090, "health_token": "current", "restore_endpoint": {"health_port": 3199, "health_token": "saved"}}
    assert switch_stopped(tmp_path, job) is not None
    assert seen == [(3199, "saved"), (3090, "current")]
    assert job["phase"] == "rolled_back"
