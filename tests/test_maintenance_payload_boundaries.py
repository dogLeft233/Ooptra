"""Malformed persisted JSON must not prevent the owned service from starting."""

import json
import sys

import pytest

from tests.support.maintenance_harness import maintenance_worker as worker


class ExitedProcess:
    pid = 123

    def wait(self):
        return 7


@pytest.mark.parametrize("payload", [None, [], "invalid", 42, {"phase": []}])
def test_supervise_ignores_non_job_json_at_startup(tmp_path, monkeypatch, payload):
    job_file = tmp_path / "data/maintenance/job.json"
    job_file.parent.mkdir(parents=True)
    job_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(worker, "spawn", lambda *args, **kwargs: ExitedProcess())

    assert worker.supervise(tmp_path) == 7


@pytest.mark.parametrize("payload", [None, [], "invalid", 42, {}, {"python": None}, {"python": 42}])
def test_supervise_uses_current_interpreter_for_invalid_runtime_record(tmp_path, monkeypatch, payload):
    runtime_file = tmp_path / "data/maintenance/runtime.json"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text(json.dumps(payload), encoding="utf-8")
    launched = []

    def spawn(root, python, *, browser_path):
        launched.append((python, browser_path))
        return ExitedProcess()

    monkeypatch.setattr(worker, "spawn", spawn)

    assert worker.supervise(tmp_path) == 7
    assert launched == [(sys.executable, "")]


@pytest.mark.parametrize("payload", [None, [], "invalid", 42])
def test_supervise_returns_exit_code_when_final_job_is_not_an_object(tmp_path, monkeypatch, payload):
    job_file = tmp_path / "data/maintenance/job.json"
    job_file.parent.mkdir(parents=True)

    class Process(ExitedProcess):
        def wait(self):
            job_file.write_text(json.dumps(payload), encoding="utf-8")
            return 7

    monkeypatch.setattr(worker, "spawn", lambda *args, **kwargs: Process())

    assert worker.supervise(tmp_path) == 7


@pytest.mark.parametrize("browser_path", [None, [], 42])
def test_supervise_ignores_invalid_saved_browser_path(tmp_path, monkeypatch, browser_path):
    runtime_file = tmp_path / "data/maintenance/runtime.json"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text(json.dumps({"python": sys.executable, "browser_path": browser_path}), encoding="utf-8")
    launched = []

    def spawn(root, python, *, browser_path):
        launched.append((python, browser_path))
        return ExitedProcess()

    monkeypatch.setattr(worker, "spawn", spawn)

    assert worker.supervise(tmp_path) == 7
    assert launched == [(sys.executable, "")]
