"""Stdlib supervisor: only restart processes that this launcher created."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from operations._internal.backups.storage import BackupStore, atomic_json
from operations._internal.mutation import ACTIVE_PHASES
from operations.models import StoppedDeployment, ValidatedRestore

from .jobs import transition


class OwnedProcess(subprocess.Popen[bytes]):
    """监督器创建的真实句柄保留本次启动标记，PID复用不能重用身份。"""
    ooptra_start_marker: str


class ProcessSupervisor:
    def __init__(self, *, spawn=None, stop_owned=None, healthy=None, checkout=None):
        if spawn is not None:
            self.spawn = spawn
        if stop_owned is not None:
            self.stop_owned = stop_owned
        if healthy is not None:
            self.healthy = healthy
        if checkout is not None:
            self.checkout = checkout

    def spawn(self, root, python, marker="", browser_path=""):
        start_marker = uuid.uuid4().hex
        env = dict(os.environ, OOPTRA_MANAGED="1", OOPTRA_UPDATE_ID=marker,
                   OOPTRA_PROCESS_MARKER=start_marker)
        if browser_path:
            env["PLAYWRIGHT_BROWSERS_PATH"] = browser_path
        executable = python
        if os.name == "nt":
            # Invoke the base interpreter with Python's venv-launcher environment.
            # This keeps venv dependencies while avoiding the Windows redirector PID.
            config = Path(python).parent.parent / "pyvenv.cfg"
            if config.is_file():
                values = dict(line.split("=", 1) for line in config.read_text(encoding="utf-8").splitlines() if "=" in line)
                base = Path(values.get("home ", values.get("home", "")).strip()) / "python.exe"
                if not base.is_file():
                    raise RuntimeError("虚拟环境的基础 Python 已不可用")
                env["__PYVENV_LAUNCHER__"] = python
                executable = str(base)
        process = OwnedProcess([executable, str(root / "main.py")], cwd=root, env=env, stdin=subprocess.PIPE)
        process.ooptra_start_marker = start_marker
        return process


    def stop_owned(self, process):
        if process.poll() is None:
            if getattr(process, "stdin", None):
                try:
                    process.stdin.write(b"OOPTRA_STOP\n")
                    process.stdin.flush()
                    process.wait(timeout=8)
                    return
                except (OSError, subprocess.TimeoutExpired):
                    pass
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


    def runtime_health(self, status, *, expected_pid=None, update_id=None, expected_python=None, expected_marker=None):
        """Local startup and supervision gate a switch; remote links only describe degradation."""
        status = status if isinstance(status, dict) else {}
        actual = status.get("process")
        actual = actual if isinstance(actual, dict) else {}
        process_ok = bool(status.get("ok") and actual.get("pid"))
        if expected_pid is not None:
            process_ok = process_ok and actual.get("pid") == expected_pid
        if update_id is not None:
            process_ok = process_ok and actual.get("update_id") == update_id
        if expected_python is not None:
            executable = actual.get("executable")
            # A venv's Python can link to the same base as another environment.
            # Preserve the invoked path instead of resolving those links away.
            process_ok = process_ok and isinstance(executable, str) and bool(executable) and os.path.normcase(os.path.abspath(executable)) == os.path.normcase(os.path.abspath(expected_python))
        if expected_marker is not None:
            process_ok = process_ok and actual.get('start_marker') == expected_marker
        bridge = status.get("bridge")
        bridge = bridge if isinstance(bridge, dict) else {}
        runtime = bridge.get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        waiting_commit = bool(update_id and actual.get('update_id') == update_id
            and actual.get('candidate_waiting_commit') is True)
        local_ready = actual.get('bootstrap_ready') is True and (
            runtime.get('supervisor_alive') is True or waiting_commit)
        detail = "服务已响应，本地启动和桥接监督已就绪" if process_ok and local_ready else (
            "本地启动尚未完成或桥接监督未运行" if process_ok else "服务未响应或进程身份不匹配")
        process_ok = process_ok and local_ready
        checks = [{"id": "process", "title": "服务进程", "state": "pass" if process_ok else "fail", "detail": detail}]
        for key, title in [("oopz", "Oopz 连接"), ("onebot", "OneBot 连接")]:
            link = bridge.get(key)
            link = link if isinstance(link, dict) else {}
            connected = bool(link.get("connected"))
            checks.append({"id": key, "title": title, "state": "pass" if connected else "degraded",
                           "detail": "已连接" if connected else "连接暂不可用；服务继续运行，不因此回滚"})
        layers = {row["id"]: {"state": "healthy" if row["state"] == "pass" else "failed" if row["state"] == "fail" else "degraded",
                               "detail": row["detail"]} for row in checks}
        return {"process_ok": bool(process_ok), "state": "failed" if not process_ok else "degraded" if any(row["state"] == "degraded" for row in checks) else "healthy", "checks": checks, **layers}


    def healthy(self, process, job, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process.poll() is None:
            try:
                host = job.get("health_host", "127.0.0.1")
                host = f"[{host}]" if ":" in host else host
                request = Request(f"http://{host}:{int(job['health_port'])}/api/status")
                if job.get("health_token"):
                    request.add_header("Authorization", "Bearer " + job["health_token"])
                with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
                    status = json.load(response)
                health = self.runtime_health(status, expected_pid=process.pid, update_id=job["id"], expected_python=job.get("health_python"),
                                              expected_marker=getattr(process, "ooptra_start_marker", None))
                if health["process_ok"]:
                    return True
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.5)
        return False


    def checkout(self, root, sha):
        # No force/reset/clean: local changes are always preserved.
        result = subprocess.run(["git", "checkout", "--detach", sha], cwd=root, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError("无法切换代码")


    def confirm_stopped(self, process, job: dict) -> StoppedDeployment:
        if (process.poll() is None or process.pid != job.get('service_pid')
                or not isinstance(getattr(process, 'ooptra_start_marker', None), str)
                or not process.ooptra_start_marker
                or process.ooptra_start_marker != job.get('service_start_marker')
                or job.get('phase') != 'awaiting_exit' or not job.get('handoff_token')):
            raise ValueError('原受管进程尚未退出或身份/交接不匹配，拒绝切换')
        return StoppedDeployment(job['id'], process.pid, process.ooptra_start_marker,
                                 job['handoff_token'])

    def switch(self, root: Path, job: dict, stopped: StoppedDeployment | None = None):
        if (not isinstance(stopped, StoppedDeployment) or stopped.job_id != job.get('id')
                or stopped.pid != job.get('service_pid')
                or stopped.start_marker != job.get('service_start_marker')
                or stopped.handoff_token != job.get('handoff_token')
                or job.get('phase') != 'awaiting_exit' or job.get('schema') != 2):
            raise ValueError('维护切换需要确认原进程已退出的交接凭据')
        store = BackupStore(root)

        def phase(name, detail):
            job.update(phase=name, detail=detail, updated_at=time.time())
            transition(root, job, role="supervisor", now=time.time())

        replacement = None
        switched = False
        safety = None
        safety_reference = None
        original_endpoint = {k: job[k] for k in ("health_host", "health_port", "health_token") if k in job}
        try:
            phase("switching", "服务已停止，创建最终数据快照并切换")
            # Capture writes made while dependencies were being prepared, after DB close.
            safety = store.create()["id"]
            safety_reference = store.inspect(safety)
            job["safety_backup_id"] = safety
            if job["action"] == "restore":
                store.restore(ValidatedRestore(job["backup_id"], job["backup_fingerprint"]), stopped)
                if job.get("restore_endpoint"):
                    job.update(job["restore_endpoint"])
                python = job["old_python"]
                browsers = job.get("old_browser_path", "")
            else:
                dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=root, timeout=20)
                if dirty.strip():
                    raise RuntimeError("切换前发现代码改动，已取消")
                self.checkout(root, job["target_sha"])
                switched = True
                python = job["new_python"]
                browsers = job.get("browser_path", "")
            replacement = self.spawn(root, python, job["id"], browsers)
            job["health_python"] = python
            phase("checking", "已启动新进程，等待 WebUI 健康检查")
            if not self.healthy(replacement, job):
                raise RuntimeError("新进程健康检查未通过")
            # Persist the interpreter and source only after the candidate became ready.
            channel = job.get("channel", "") if job["action"] == "update" else job.get("old_channel", "")
            atomic_json(root / "data/maintenance/runtime.json", {"python": python, "browser_path": browsers, "channel": channel})
            phase("complete", "更新完成" if job["action"] == "update" else "备份恢复完成")
            return replacement
        except Exception:
            if replacement:
                self.stop_owned(replacement)
            phase("rolling_back", "切换或健康检查失败，正在恢复原运行环境")
            try:
                if switched:
                    self.checkout(root, job["old_sha"])
                if safety_reference is not None:
                    store.restore(safety_reference, stopped)
                job.update(original_endpoint)
                replacement = self.spawn(root, job["old_python"], job["id"], job.get("old_browser_path", ""))
                job["health_python"] = job["old_python"]
                if not self.healthy(replacement, job):
                    self.stop_owned(replacement)
                    phase("failed", "恢复后仍未通过健康检查；请在部署机器查看日志并手动启动")
                    return None
                atomic_json(root / "data/maintenance/runtime.json", {"python": job["old_python"], "browser_path": job.get("old_browser_path", ""),
                                                                    "channel": job.get("old_channel", "")})
                phase("rolled_back", "操作失败，已恢复原版本和数据")
                return replacement
            except Exception:
                phase("failed", "自动回滚失败；请使用恢复前备份和原虚拟环境手动恢复")
                return None


    def supervise(self, root):
        job_file = root / "data/maintenance/job.json"
        try:
            abandoned = json.loads(job_file.read_text(encoding="utf-8"))
            if (isinstance(abandoned, dict) and isinstance(abandoned.get('phase'), str)
                    and abandoned.get('phase') in ACTIVE_PHASES):
                abandoned.update(phase='interrupted', detail='启动器维护任务被中断；业务保持受限，请核对版本与数据并显式恢复', updated_at=time.time())
                atomic_json(job_file, abandoned)
        except (OSError, ValueError):
            pass
        python = sys.executable
        browsers = ""
        try:
            runtime = json.loads((root / "data/maintenance/runtime.json").read_text(encoding="utf-8"))
            saved = runtime.get("python") if isinstance(runtime, dict) else None
            if isinstance(saved, str) and Path(saved).is_file():
                python = saved
                saved_browsers = runtime.get("browser_path", "")
                browsers = saved_browsers if isinstance(saved_browsers, str) else ""
        except (OSError, ValueError, KeyError):
            pass
        process = self.spawn(root, python, browser_path=browsers)
        try:
            while True:
                code = process.wait()
                try:
                    job = json.loads(job_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return code
                if not isinstance(job, dict) or job.get('service_pid') != process.pid:
                    return code
                try:
                    stopped = self.confirm_stopped(process, job)
                except ValueError:
                    return code or 1
                process = self.switch(root, job, stopped)
                if process is None:
                    return 1
        except KeyboardInterrupt:
            self.stop_owned(process)
            return 0
