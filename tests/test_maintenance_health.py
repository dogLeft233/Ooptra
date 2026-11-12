import pytest

from tests.support.maintenance_harness import maintenance_worker as worker


def test_detached_updated_install_retains_its_verified_source(tmp_path, monkeypatch):
    from tests.support.maintenance_harness import MaintenanceService

    monkeypatch.setattr("tests.support.maintenance_harness.maintenance.__version__", "261007-dev")
    current = "a" * 40
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text(current, encoding="utf-8")
    service = MaintenanceService(tmp_path)
    service.write_job({"action": "update", "phase": "complete", "channel": "main", "target_sha": current})
    assert service.status()["update"]["channel"] == "main"
    (git_dir / "refs/heads").mkdir(parents=True)
    (git_dir / "refs/heads/dev").write_text(current, encoding="utf-8")
    (git_dir / "HEAD").write_text("ref: refs/heads/dev", encoding="utf-8")
    assert service.status()["update"]["channel"] == "dev"
    (git_dir / "HEAD").write_text(current, encoding="utf-8")
    service.write_job({"action": "update", "phase": "complete", "channel": "main", "target_sha": "b" * 40})
    assert service.status()["update"]["channel"] == "dev"


def payload(**process):
    return {"ok": True, "process": {"pid": 123, "update_id": "candidate", "executable": "/venv/bin/python",
                                      "bootstrap_ready": True, **process},
            "bridge": {"runtime": {"supervisor_alive": True},
                       "oopz": {"connected": False}, "onebot": {"connected": False}}}


def test_disconnected_network_is_degraded_but_process_remains_healthy():
    health = worker.runtime_health(payload(), expected_pid=123, update_id="candidate", expected_python="/venv/bin/python")
    assert health["process_ok"] is True
    assert health["state"] == "degraded"
    assert [row["state"] for row in health["checks"]] == ["pass", "degraded", "degraded"]


@pytest.mark.parametrize("ready", [False, None, "true", 1])
def test_matching_identity_cannot_accept_incomplete_local_startup(ready):
    health = worker.runtime_health(payload(bootstrap_ready=ready), expected_pid=123,
                                   update_id="candidate", expected_python="/venv/bin/python")
    assert health["process_ok"] is False
    assert health["state"] == "failed"


@pytest.mark.parametrize("runtime", [{"supervisor_alive": False}, {}, None, {"supervisor_alive": "true"}])
def test_matching_identity_cannot_accept_missing_bridge_supervision(runtime):
    status = payload()
    status["bridge"]["runtime"] = runtime
    assert worker.runtime_health(status, expected_pid=123, update_id="candidate")["process_ok"] is False


def test_missing_readiness_marker_cannot_accept_candidate():
    status = payload()
    del status["process"]["bootstrap_ready"]
    assert worker.runtime_health(status, expected_pid=123, update_id="candidate")["process_ok"] is False


def test_optional_voice_unavailable_does_not_reject_ready_offline_process():
    status = payload()
    status["config"] = {"voice": {"enabled": False, "status": {"error": "unavailable"}}}
    assert worker.runtime_health(status, expected_pid=123, update_id="candidate")["process_ok"] is True


def test_status_exposes_bootstrap_readiness_independently_of_http_availability():
    import asyncio
    import json
    from types import SimpleNamespace

    from webui.server import WebUIConsole

    console = WebUIConsole(None, SimpleNamespace(snapshot=lambda: payload()["bridge"]))
    initial = json.loads(asyncio.run(console._handle_status(None)).text)
    assert initial["process"]["bootstrap_ready"] is False
    assert initial["health"]["process_ok"] is False
    console.set_bootstrap_ready(True)
    ready = json.loads(asyncio.run(console._handle_status(None)).text)
    assert ready["process"]["bootstrap_ready"] is True
    assert ready["health"]["process_ok"] is True
    console.set_bootstrap_ready(False)
    assert json.loads(asyncio.run(console._handle_status(None)).text)["health"]["process_ok"] is False


@pytest.mark.parametrize("override", [{"pid": 456}, {"update_id": "other"}, {"executable": "/other/bin/python"}])
def test_process_identity_mismatch_is_unhealthy(override):
    health = worker.runtime_health(payload(**override), expected_pid=123, update_id="candidate", expected_python="/venv/bin/python")
    assert health["process_ok"] is False
    assert health["state"] == "failed"


def test_two_interpreters_linked_to_same_base_do_not_share_runtime_identity(tmp_path):
    base = tmp_path / "base-python"
    base.write_bytes(b"python")
    intended, other = tmp_path / "intended-python", tmp_path / "other-python"
    try:
        intended.symlink_to(base)
        other.symlink_to(base)
    except OSError:
        pytest.skip("Symbolic links unavailable on this Windows account")
    health = worker.runtime_health(payload(executable=str(other)), expected_pid=123, update_id="candidate", expected_python=str(intended))
    assert health["process_ok"] is False


@pytest.mark.parametrize("malformed", [None, [], {"ok": True, "process": []}])
def test_malformed_health_payload_is_failed_instead_of_raising(malformed):
    assert worker.runtime_health(malformed, expected_pid=123)["process_ok"] is False
