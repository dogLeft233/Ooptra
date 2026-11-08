import json
import sqlite3
import zipfile

import pytest


def test_backup_consistent_sqlite_restore_and_excludes_maintenance(tmp_path):
    from operations import create_backup_store as BackupStore

    (tmp_path / "data/maintenance").mkdir(parents=True)
    (tmp_path / "data/maintenance/private-job.json").write_text("secret")
    (tmp_path / "config.py").write_text("VALUE = 1\n")
    db = sqlite3.connect(tmp_path / "data/test.sqlite3")
    db.execute("create table entries(value)")
    db.execute("insert into entries values(1)")
    db.commit()
    store = BackupStore(tmp_path)
    result = store.create()
    with zipfile.ZipFile(store.path(result["id"])) as archive:
        assert "data/maintenance/private-job.json" not in archive.namelist()
    (tmp_path / "config.py").write_text("VALUE = 2\n")
    db.close()
    store.restore(result["id"])
    assert (tmp_path / "config.py").read_text() == "VALUE = 1\n"
    with sqlite3.connect(tmp_path / "data/test.sqlite3") as restored:
        assert restored.execute("select value from entries").fetchall() == [(1,)]


@pytest.mark.parametrize("name", ["../escape", "data/../../escape", "data/maintenance/job.json", "src/main.py"])
def test_restore_rejects_paths_outside_backup_scope(tmp_path, name):
    from operations import create_backup_store as BackupStore

    store = BackupStore(tmp_path)
    bad_id = "a" * 32
    with zipfile.ZipFile(store.path(bad_id), "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": 1, "files": [{"name": name, "size": 1, "sha256": ""}]}))
        archive.writestr(name, "x")
    with pytest.raises(ValueError):
        store.restore(bad_id)
    assert not (tmp_path.parent / "escape").exists()


def test_restore_rejects_changed_content_before_overwriting_config(tmp_path):
    from operations import create_backup_store as BackupStore

    (tmp_path / "config.py").write_text("VALUE = 1")
    store = BackupStore(tmp_path)
    backup = store.create()
    with zipfile.ZipFile(store.path(backup["id"])) as archive:
        manifest = archive.read("manifest.json")
    with zipfile.ZipFile(store.path(backup["id"]), "w") as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("config.py", "VALUE = 2")
    with pytest.raises(ValueError):
        store.restore(backup["id"])
    assert (tmp_path / "config.py").read_text() == "VALUE = 1"
