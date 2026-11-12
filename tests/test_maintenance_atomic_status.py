"""Windows sharing conflicts must not discard maintenance status writes."""

from pathlib import Path

import pytest

from tests.support.maintenance_harness import maintenance_storage as storage


def sharing_error():
    error = PermissionError("temporary sharing conflict")
    error.winerror = 32
    return error


def test_status_write_retries_transient_windows_sharing_conflict(tmp_path, monkeypatch):
    target = tmp_path / "job.json"
    target.write_text('{"phase":"preparing"}', encoding="utf-8")
    replace = storage.os.replace
    calls = []
    sleeps = []

    def conflicting_replace(source, destination):
        calls.append(source)
        if len(calls) <= 2:
            assert target.read_text(encoding="utf-8") == '{"phase":"preparing"}'
            raise sharing_error()
        replace(source, destination)

    monkeypatch.setattr(storage.os, "replace", conflicting_replace)
    monkeypatch.setattr(storage.time, "sleep", sleeps.append)
    storage.atomic_json(target, {"phase": "complete"})
    assert '"complete"' in target.read_text(encoding="utf-8")
    assert len(calls) == 3
    assert len(sleeps) == 2
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("windows_error", [True, False])
def test_status_write_preserves_old_file_on_permanent_failure(tmp_path, monkeypatch, windows_error):
    target = tmp_path / "job.json"
    target.write_text("old-status", encoding="utf-8")
    calls = []
    sleeps = []

    def denied(source, destination):
        calls.append(Path(source))
        raise sharing_error() if windows_error else PermissionError("permanent")

    monkeypatch.setattr(storage.os, "replace", denied)
    monkeypatch.setattr(storage.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError):
        storage.atomic_json(target, {"phase": "complete"})
    assert target.read_text(encoding="utf-8") == "old-status"
    assert len(calls) == (5 if windows_error else 1)
    assert sum(sleeps) <= 0.25
    assert list(tmp_path.iterdir()) == [target]
