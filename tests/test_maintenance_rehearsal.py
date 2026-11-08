"""Real local Git + process + HTTP rehearsal, without Oopz or model credentials."""
import socket
import subprocess
import sys

import pytest

from tests.support.maintenance_harness import maintenance_worker as worker


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


@pytest.mark.parametrize("candidate", ["ready", "crash", "not_ready", "no_supervisor"])
def test_real_process_switch_and_failed_candidate_rollback(tmp_path, monkeypatch, candidate):
    crash = candidate != "ready"
    check_health = worker.healthy
    monkeypatch.setattr(worker, "healthy", lambda process, job, timeout=3: check_health(process, job, timeout=timeout))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    code = '''import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "process": {"pid": os.getpid(), "executable": sys.executable, "update_id": os.environ.get("OOPTRA_UPDATE_ID"), "bootstrap_ready": True}, "bridge": {"runtime": {"supervisor_alive": True}, "oopz": {"connected": False}, "onebot": {"connected": False}}}).encode())
    def log_message(self, *args): pass
def control():
    if sys.stdin.readline().strip() == "OOPTRA_STOP": os._exit(0)
threading.Thread(target=control, daemon=True).start()
HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''.replace("PORT", str(port))
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / ".gitignore").write_text("data/\nconfig.py\n")
    (tmp_path / "main.py").write_text(code)
    (tmp_path / "config.py").write_text("ORIGINAL = True")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "old server")
    old = git(tmp_path, "rev-parse", "HEAD")
    replacement = {
        "ready": code + "\n# replacement\n",
        "crash": "raise SystemExit(3)",
        "not_ready": code.replace('"bootstrap_ready": True', '"bootstrap_ready": False'),
        "no_supervisor": code.replace('"supervisor_alive": True', '"supervisor_alive": False'),
    }[candidate]
    (tmp_path / "main.py").write_text(replacement)
    git(tmp_path, "add", "main.py")
    git(tmp_path, "commit", "-m", "candidate")
    target = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "--detach", old)
    job = {"id": "rehearsal", "action": "update", "old_sha": old, "target_sha": target,
           "old_python": sys.executable, "new_python": sys.executable, "health_port": port}
    process = worker.switch(tmp_path, job)
    try:
        assert process is not None
        assert job["phase"] == ("rolled_back" if crash else "complete")
        assert git(tmp_path, "rev-parse", "HEAD") == (old if crash else target)
        assert worker.healthy(process, job, timeout=2)
        assert (tmp_path / "config.py").read_text() == "ORIGINAL = True"
    finally:
        if process:
            worker.stop_owned(process)
