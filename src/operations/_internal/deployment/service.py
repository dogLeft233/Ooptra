"""Prepare upgrades while the current bot remains online; the launcher owns restarts."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path

from operations._internal.backups.storage import BackupStore, atomic_json, restored_endpoint
from operations._internal.mutation import ACTIVE_PHASES, deployment_access, mutation_lock
from operations.models import (
    HandoffRef,
    JobRef,
    JobSnapshot,
    MaintenancePlan,
    MaintenancePlatform,
    MaintenanceRequest,
    RevisionConflict,
    SystemSnapshot,
    UpdateTarget,
)

from .jobs import transition

OFFICIAL_REMOTES = {
    "https://github.com/eason4869/ooptra", "https://github.com/eason4869/ooptra.git",
    "git@github.com:eason4869/ooptra.git", "ssh://git@github.com/eason4869/ooptra.git",
}
CHANNELS = {"main": "正式版", "beta": "测试版", "dev": "预览版"}
PUBLIC_JOB_FIELDS = {
    "id", "action", "phase", "detail", "started_at", "updated_at", "target_sha",
    "old_sha", "backup_id", "channel", "progress_stage", "stage_started_at",
    "revision", "closed", "pending", "plan_id",
}
QUERY_WAIT_SECONDS = 12
PREFLIGHT_SECONDS = 10
INSTALLED_SECONDS = 4
CHECK_SECONDS = 60
logger = logging.getLogger(__name__)


def run_command(root: Path, args: list[str], timeout: float = 30, env=None) -> str:
    """Never accept shell commands or arbitrary arguments from the HTTP caller."""
    child_env = dict(os.environ if env is None else env)
    if Path(args[0]).stem.lower() == "git":
        child_env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(args, cwd=root, env=child_env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        # pip/git output may contain credentials; retain only the step and return code.
        raise ValueError(f"{Path(args[0]).name} 执行失败（退出码 {result.returncode}）")
    return result.stdout.strip()


class MaintenanceService:
    def _remaining(self, deadline, stage, maximum=None):
        seconds = deadline - self.platform.clock.monotonic()
        if seconds <= 0:
            raise ValueError(f"{stage}超时，请稍后重试")
        return min(seconds, maximum) if maximum is not None else seconds

    def probe_network(self, mirror, channel, **options):
        return self.platform.test_mirror_connection(mirror, channel, self.platform.run_command, **options)

    def __init__(self, root, *, shutdown=None, managed=None, venv=None, port=3090, host="127.0.0.1", token="", runtime_status=None, bootstrap_ready=None, runtime_control=None, platform: MaintenancePlatform):
        self.platform = platform
        self.root = Path(root).resolve()
        self.backups = BackupStore(self.root)
        self.job_file = self.root / "data/maintenance/job.json"
        self.shutdown = shutdown
        self.managed = os.environ.get("OOPTRA_MANAGED") == "1" if managed is None else managed
        self.venv = sys.prefix != sys.base_prefix if venv is None else venv
        self.port = port
        self.host = "127.0.0.1" if host in {"0.0.0.0", "localhost"} else "::1" if host in {"::", "::1"} else host
        self.token = token
        self._task = None
        self._busy = False
        self._mutation_guard = None
        self.runtime_control = runtime_control
        self._mutation_tasks = set()
        self._check = None
        self._cleanup = None
        self._storage_cache = None
        self._backup_totals = {"backups_count": 0, "backups_bytes": 0}
        self._query_tasks = {}
        self._query_cache = {}
        self._read_generation = 0
        self._check_task = None
        self._check_stage = None
        self.runtime_status = runtime_status
        self.bootstrap_ready = bootstrap_ready
    def read_job(self):
        try:
            value = json.loads(self.job_file.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def write_job(self, value):
        transition(self.root, value, role='application', now=self.platform.clock.time(),
                   locked=self._mutation_guard is not None)
        self._invalidate_reads()

    def set_runtime_control(self, control):
        self.runtime_control = control

    def job(self, reference: JobRef):
        raw = self.read_job()
        if raw is None or raw.get('id') != reference.job_id:
            try:
                raw = json.loads((self.root / 'data/maintenance/jobs' /
                    (hashlib.sha256(reference.job_id.encode()).hexdigest() + '.json')).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return None
        return JobSnapshot(raw['id'], raw.get('revision', 0), raw.get('action', ''),
            raw.get('phase', 'unknown'), raw.get('detail', ''),
            tuple(raw.get('closed', [])), tuple(raw.get('pending', [])))

    def snapshot(self):
        raw = self.read_job()
        return SystemSnapshot(deployment_access(self.root),
            self.job(JobRef(raw['id'])) if raw and isinstance(raw.get('id'), str) else None)

    def _config_revision(self):
        path = self.root / 'config.py'
        return hashlib.sha256(path.read_bytes() if path.exists() else b'').hexdigest()

    async def plan(self, request: MaintenanceRequest) -> MaintenancePlan:
        if not isinstance(request, MaintenanceRequest):
            raise TypeError('需要已解析的维护请求')
        access = deployment_access(self.root)
        if not access.allowed or self._busy:
            raise ValueError(access.reason or '维护任务进行中')
        backup_fingerprint = ''
        if request.action == 'update':
            await self._validate_update(request.target, request.confirm_channel_switch)
        else:
            if not self.managed or not self.runtime_control:
                raise ValueError('恢复需要使用 python launcher.py 启动，确保数据库已安全关闭')
            self.backups.path(request.backup_id)
            def validate():
                with tempfile.TemporaryDirectory(dir=self.backups.directory) as folder:
                    self.backups.validate(request.backup_id, Path(folder))
            await asyncio.to_thread(validate)
            verified = await asyncio.to_thread(self.backups.inspect, request.backup_id)
            backup_fingerprint = verified.fingerprint
        installed, _ = await asyncio.to_thread(self.installed)
        def persist_plan():
            with mutation_lock(self.root, blocking=False):
                if not deployment_access(self.root).allowed:
                    raise ValueError('维护任务进行中')
                plan = MaintenancePlan(uuid.uuid4().hex, request, installed, self._config_revision(),
                    self.platform.clock.time() + 600,
                    ('停止业务资源', '重启应用', '保留身份映射与用户数据'), backup_fingerprint)
                atomic_json(self.root / 'data/maintenance/plans' / (plan.plan_id + '.json'), asdict(plan))
                return plan
        return await self._run_mutation(persist_plan)

    async def execute(self, plan: MaintenancePlan) -> JobRef:
        if not isinstance(plan, MaintenancePlan) or re.fullmatch(r'[a-f0-9]{32}', plan.plan_id) is None:
            raise ValueError('维护计划无效')
        plan_path = self.root / 'data/maintenance/plans' / (plan.plan_id + '.json')
        try:
            saved = json.loads(plan_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError('维护计划不存在或损坏') from exc
        bound_job = saved.pop('execution_id', None)
        if saved != json.loads(json.dumps(asdict(plan))):
            raise ValueError('维护计划与已检查内容不符')
        if bound_job:
            return JobRef(bound_job)
        current_job = self.read_job()
        if current_job and current_job.get('plan_id') == plan.plan_id:
            return JobRef(current_job['id'])
        if (self.platform.clock.time() > plan.expires_at or self._config_revision() != plan.config_revision
                or (await asyncio.to_thread(self.installed))[0] != plan.deployment_revision):
            raise RevisionConflict('维护计划过期或部署/配置已变化，请重新规划')
        if plan.request.action == 'update':
            await self._validate_update(plan.request.target, plan.request.confirm_channel_switch)
        if plan.request.action == 'restore':
            verified = await asyncio.to_thread(self.backups.inspect, plan.request.backup_id)
            if verified.fingerprint != plan.backup_fingerprint:
                raise RevisionConflict('维护计划绑定的备份内容变化，请重新规划')
        self._claim()
        try:
            # Recheck under the shared configuration/maintenance mutation gate.
            if self._config_revision() != plan.config_revision:
                raise RevisionConflict('维护计划的配置版本已变化')
            job = {'id': uuid.uuid4().hex, 'plan_id': plan.plan_id,
                'action': plan.request.action, 'phase': 'preparing',
                'detail': '准备维护资源，当前服务继续运行',
                'started_at': self.platform.clock.time(), 'service_pid': os.getpid(),
                'service_start_marker': os.environ.get('OOPTRA_PROCESS_MARKER', ''),
                'config_revision': plan.config_revision, 'deployment_revision': plan.deployment_revision}
            if plan.request.action == 'update':
                target = plan.request.target
                if target is None:
                    raise ValueError('更新计划缺少目标')
                job.update(channel=target.channel, target_sha=target.commit)
            else:
                job['backup_id'] = plan.request.backup_id
                job['backup_fingerprint'] = plan.backup_fingerprint
            self.write_job(job)
            saved['execution_id'] = job['id']
            atomic_json(plan_path, saved)
            # Persistent preparing now blocks new writes; never hold the OS gate while
            # waiting for resource shutdown (an old save thread may still need to fail).
            self._release_guard()
            self._task = asyncio.create_task(self._prepare(job) if job['action'] == 'update'
                                             else self._prepare_restore(job))
            return JobRef(job['id'])
        except BaseException:
            self._release()
            raise

    def _invalidate_reads(self):
        self._read_generation += 1
        self._storage_cache = None
        self._query_cache.clear()

    def _cached_query(self, kind):
        cached = self._query_cache.get(kind)
        return cached[1] if cached and self.platform.clock.monotonic() - cached[0] < 30 else None

    def _pending_query(self, kind, backups=None):
        if kind == "preflight":
            return {"supported": False, "checks": [], "pending": True,
                    "detail": "预检查尚未完成；备份和版本信息可独立查看"}
        totals = self._backup_totals if backups is None else {"backups_count": len(backups), "backups_bytes": sum(row["size"] for row in backups)}
        return {**totals, "pending": True, "detail": "完整空间统计尚未完成"}

    def _read_storage(self):
        backups = self.backups.list(limit=None)
        self._backup_totals = {"backups_count": len(backups), "backups_bytes": sum(row["size"] for row in backups)}
        return self.storage_status(backups, self.read_job())

    async def query(self, kind):
        """Share a bounded read; an HTTP timeout never launches duplicate workers."""
        cached = self._cached_query(kind)
        if cached is not None:
            return {"ok": True, kind: cached}
        task = self._query_tasks.get(kind)
        if task is None or task.done():
            generation = self._read_generation
            async def sample():
                try:
                    if kind == "preflight":
                        result = await asyncio.to_thread(self.preflight)
                    else:
                        result = await asyncio.to_thread(self._read_storage)
                    result = {**result, "pending": False}
                except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    logger.warning("Maintenance %s read failed (%s)", kind, type(exc).__name__)
                    result = {**self._pending_query(kind), "pending": False,
                              "error": str(exc) if isinstance(exc, ValueError) else "维护读取失败，请检查部署环境"}
                if generation != self._read_generation:
                    return self._pending_query(kind)
                self._query_cache[kind] = (self.platform.clock.monotonic(), result)
                return result
            task = asyncio.create_task(sample())
            self._query_tasks[kind] = task
        try:
            result = await asyncio.wait_for(asyncio.shield(task), self.platform.query_wait_seconds)
        except asyncio.TimeoutError:
            logger.warning("Maintenance %s response deadline exceeded; worker remains shared", kind)
            result = {**self._pending_query(kind), "error": "读取超时；后台仍在完成本次读取，请稍后刷新"}
        return {"ok": True, kind: result}

    def storage_status(self, backups, job):
        # Status runs in a worker thread. Repeated page polling must not walk
        # large venv/browser trees every three seconds; destructive operations
        # always call self.platform.inventory afresh and never consume these cached totals.
        cached = self._storage_cache
        if cached and self.platform.clock.monotonic() - cached[0] < 30:
            return cached[1]
        generation = self._read_generation
        try:
            storage, _ = self.platform.inventory(self.root, backups, job)
        except ValueError as exc:
            storage = {"error": str(exc), "backups_count": len(backups), "backups_bytes": sum(row["size"] for row in backups)}
        storage = {**storage, "sampled_at": self.platform.clock.time()}
        if generation == self._read_generation:
            self._storage_cache = (self.platform.clock.monotonic(), storage)
        return storage

    def preflight(self):
        checks = []
        deadline = self.platform.clock.monotonic() + self.platform.preflight_seconds
        write_directories = [self.root, self.backups.directory.parent]

        def check(name, passed, detail):
            checks.append({"id": name, "passed": bool(passed), "detail": detail})

        check("venv", self.venv, "使用 Python 虚拟环境")
        check("launcher", self.managed, "使用 python launcher.py 启动，才能安全停止、升级和回滚")
        stage = "Git 仓库读取"
        try:
            def git(*args):
                return self.platform.run_command(self.root, ["git", *args], self._remaining(deadline, stage, 2))
            top = git("rev-parse", "--show-toplevel")
            check("git", Path(top).resolve() == self.root, "当前目录是 Git 仓库根目录")
            stage = "Git 更新源读取"
            remote = git("remote", "get-url", "origin")
            check("remote", remote.lower().rstrip("/") in OFFICIAL_REMOTES, "origin 指向 Eason4869/Ooptra 官方仓库")
            stage = "Git 本地改动检查"
            dirty = git("status", "--porcelain")
            check("clean", not dirty, "代码有本地改动或未跟踪文件时，先自行提交或处理；不会强制覆盖")
            for flag in ("--git-dir", "--git-common-dir"):
                stage = "Git 元数据读取"
                directory = Path(git("rev-parse", flag))
                write_directories.append(directory if directory.is_absolute() else self.root / directory)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            logger.warning("Maintenance preflight failed at %s (%s)", stage, type(exc).__name__)
            timed_out = isinstance(exc, subprocess.TimeoutExpired) or self.platform.clock.monotonic() >= deadline
            check("git", False, f"{stage}{'超时' if timed_out else '失败'}；请检查本地 Git 环境后重试")
        try:
            check("disk", shutil.disk_usage(self.root).free >= 512 * 1024 * 1024, "至少 512 MB 可用空间；安装依赖时可能需要更多")
        except OSError:
            check("disk", False, "无法读取部署目录的可用空间")
        try:
            for directory in set(write_directories):
                with tempfile.TemporaryFile(dir=directory):
                    pass
            check("writable", True, "部署目录、维护目录及 Git 元数据允许当前服务账户写入")
        except OSError:
            check("writable", False, "当前服务账户无法写入部署目录、维护目录或 Git 元数据")
        return {"supported": all(c["passed"] for c in checks), "checks": checks}

    def installed(self, *, deadline=None, strict=False):
        """Read the installed channel independently of the last selected target."""
        deadline = min(deadline, self.platform.clock.monotonic() + self.platform.installed_seconds) if deadline is not None else self.platform.clock.monotonic() + self.platform.installed_seconds
        try:
            current_sha = self.platform.run_command(self.root, ["git", "rev-parse", "HEAD"], self._remaining(deadline, "本地版本读取", 2))
            branch = self.platform.run_command(self.root, ["git", "branch", "--show-current"], self._remaining(deadline, "本地渠道读取", 2))
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            if strict:
                logger.warning("Maintenance installed Git read failed (%s)", type(exc).__name__)
                raise ValueError("本地 Git 版本读取超时或失败，请检查部署目录后重试") from exc
            current_sha, branch = "", ""
        return self._installed_channel(current_sha, branch)

    def installed_hint(self):
        """Filesystem-only display hint; update authorization still uses real Git."""
        current_sha, branch = "", ""
        try:
            directory = self.root / ".git"
            if directory.is_file():
                text = directory.read_text(encoding="utf-8").strip()
                if not text.startswith("gitdir: "):
                    raise ValueError
                directory = (self.root / text[8:]).resolve()
            head = (directory / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref: refs/heads/"):
                ref = head[5:]
                branch = ref[len("refs/heads/"):]
                if not re.fullmatch(r"refs/heads/[A-Za-z0-9_./-]+", ref) or ".." in ref:
                    raise ValueError
                common = directory
                if (directory / "commondir").is_file():
                    common = (directory / (directory / "commondir").read_text(encoding="utf-8").strip()).resolve()
                if (common / ref).is_file():
                    current_sha = (common / ref).read_text(encoding="utf-8").strip()
                elif (common / "packed-refs").is_file():
                    with (common / "packed-refs").open(encoding="utf-8") as stream:
                        for row in stream.read(1024 * 1024).splitlines():
                            if row.endswith(" " + ref):
                                current_sha = row.split()[0]
                                break
            else:
                current_sha = head
            if not re.fullmatch(r"[a-f0-9]{40}", current_sha):
                current_sha = ""
        except (OSError, ValueError):
            pass
        return self._installed_channel(current_sha, branch)

    def _installed_channel(self, current_sha, branch):
        if branch in CHANNELS:
            return current_sha, branch
        try:
            saved = json.loads((self.root / "data/maintenance/runtime.json").read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("channel") in CHANNELS:
                return current_sha, saved["channel"]
        except (OSError, ValueError):
            pass
        job = self.read_job()
        if (isinstance(job, dict) and job.get("action") == "update" and job.get("phase") == "complete"
                and job.get("target_sha") == current_sha and job.get("channel") in CHANNELS):
            return current_sha, job["channel"]
        return current_sha, "beta" if self.platform.version.endswith("-beta") else "dev" if self.platform.version.endswith("-dev") else "main"

    def status(self, page=1, page_size=50):
        if isinstance(page, bool) or isinstance(page_size, bool) or not isinstance(page, int) or not isinstance(page_size, int) or page < 1 or not 1 <= page_size <= 50:
            raise ValueError("备份分页参数无效")
        job = self.read_job()
        backups = self.backups.list(limit=None)
        record_error = job is None and self.job_file.exists()
        references = {job.get(key) for key in ("backup_id", "safety_backup_id") if isinstance(job.get(key), str)} if job else set()
        for row in backups:
            reason = "维护记录无法读取，请修复后再删除" if record_error else "更新、恢复或回滚任务引用的备份" if row["id"] in references else ""
            row.update(can_delete=not reason, delete_reason=reason)
        self._backup_totals = {"backups_count": len(backups), "backups_bytes": sum(row["size"] for row in backups)}
        pages = max(1, (len(backups) + page_size - 1) // page_size)
        page = min(page, pages)
        preflight = self._cached_query("preflight") or self._pending_query("preflight")
        current_sha, channel = self.installed_hint()
        update = {"channel": channel, "current_sha": current_sha,
                  "target_sha": None, "available": False, "version": self.platform.version, **(self._check or {}),
                  "supported": preflight["supported"], "current_channel": channel}
        storage = self._cached_query("storage") or self._pending_query("storage", backups)
        bridge = self.runtime_status() if self.runtime_status else {}
        return {
            "ok": True, "preflight": preflight, "backups": backups[(page - 1) * page_size:page * page_size],
            "backup_page": {"page": page, "page_size": page_size, "total": len(backups), "pages": pages},
            "storage": storage, "update": update,
            "health": self.platform.runtime_health({"ok": True, "process": {"pid": os.getpid(), "bootstrap_ready": bool(self.bootstrap_ready and self.bootstrap_ready())}, "bridge": bridge}),
            "job": {k: v for k, v in job.items() if k in PUBLIC_JOB_FIELDS} if isinstance(job, dict) else None,
            "check": self._check, "busy": self._busy,
            "check_stage": self._check_stage if self._check_task and not self._check_task.done() else None,
            "restore_supported": bool(self.managed and self.shutdown),
        }

    def preview_cleanup(self):
        if self._busy or (self.read_job() or {}).get("phase") in ACTIVE_PHASES:
            raise ValueError("维护任务进行中，暂不能清理")
        _, items = self.platform.inventory(self.root, self.backups.list(limit=None), self.read_job())
        self._cleanup = {"token": uuid.uuid4().hex, "items": items, "expires_at": self.platform.clock.time() + 300}
        return {"ok": True, "token": self._cleanup["token"], "expires_at": self._cleanup["expires_at"],
                "items": [{k: v for k, v in item.items() if k != "fingerprint"} for item in items],
                "total_bytes": sum(item["size"] for item in items)}

    async def delete_backup(self, backup_id):
        self._claim()
        try:
            def remove():
                job = self.read_job()
                if job is None and self.job_file.exists():
                    raise ValueError("维护记录无法读取，请修复后再删除")
                if job and any(job.get(key) == backup_id for key in ("backup_id", "safety_backup_id")):
                    raise ValueError("这份备份被更新、恢复或回滚任务引用，暂不能删除")
                self.backups.delete(backup_id)
                return {"deleted_id": backup_id}
            result = await self._run_mutation(remove)
            self._cleanup = None
            return result
        finally:
            self._release()
            self._invalidate_reads()

    async def apply_cleanup(self, token):
        self._claim()
        try:
            preview = self._cleanup
            self._cleanup = None
            if not preview or token != preview["token"] or self.platform.clock.time() > preview["expires_at"]:
                raise ValueError("清理预览已失效，请重新预览并确认")
            def apply():
                _, current = self.platform.inventory(self.root, self.backups.list(limit=None), self.read_job())
                if current != preview["items"]:
                    raise ValueError("清理对象或运行环境已变化，请重新预览")
                self.platform.delete_items(self.root, current)
                return {"ok": True, "removed": len(current), "removed_bytes": sum(item["size"] for item in current)}
            return await self._run_mutation(apply)
        finally:
            self._release()
            self._invalidate_reads()

    async def check_update(self, channel=None):
        if channel is not None and (not isinstance(channel, str) or channel not in CHANNELS):
            raise ValueError("仅支持 main、beta 或 dev 更新源")
        if self._busy:
            raise ValueError("维护任务进行中")
        if self._check_task and not self._check_task.done():
            raise ValueError("更新检查进行中，请等待本次检查完成")
        self._check = None
        self._check_stage = "读取本地安装版本"
        deadline = self.platform.clock.monotonic() + self.platform.check_seconds
        state = {"expired": False}
        self._check_task = asyncio.create_task(self._perform_check(channel, deadline, state))
        # Retrieve errors even when a timed-out HTTP caller has already returned.
        self._check_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        try:
            return await asyncio.wait_for(asyncio.shield(self._check_task), self.platform.check_seconds)
        except asyncio.TimeoutError as exc:
            state["expired"] = True
            logger.warning("Maintenance update check exceeded total response deadline")
            raise ValueError("更新检查总耗时超过 60 秒，请检查本地 Git 和 GitHub 连接后重试") from exc

    async def _perform_check(self, channel, deadline, state):
        installed_supported = (self.root / ".git").exists()
        if installed_supported:
            current, current_channel = await asyncio.to_thread(self.installed, deadline=deadline, strict=True)
        else:
            current, current_channel = await asyncio.to_thread(self.installed_hint)
        channel = current_channel if channel is None else channel
        # Read-only remote lookup also works for unsupported installations.
        try:
            def progress(detail):
                self._check_stage = detail
                logger.info("Maintenance update check: %s", detail)
            sha = await asyncio.to_thread(self.platform.lookup_official_sha, self.root, channel, deadline, self.platform.run_command, progress=progress)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            logger.warning("Maintenance update GitHub lookup failed (%s)", type(exc).__name__)
            if isinstance(exc, ValueError):
                raise
            detail = "超时" if isinstance(exc, subprocess.TimeoutExpired) or self.platform.clock.monotonic() >= deadline else "失败"
            raise ValueError(f"GitHub 更新查询{detail}；请检查服务进程的网络、代理与 Git 配置后重试") from exc
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise ValueError("无法读取更新版本")
        switching = channel != current_channel
        if state["expired"] or self.platform.clock.monotonic() >= deadline:
            raise ValueError("更新检查超时，请重新检查")
        self._check = {"channel": channel, "channel_label": CHANNELS[channel], "target_sha": sha,
                       "current_sha": current, "current_channel": current_channel,
                       "installed_supported": installed_supported,
                       "available": sha != current or switching,
                       "action": "switch" if switching else "update" if sha != current else "current",
                       "requires_confirmation": switching, "checked_at": self.platform.clock.time()}
        return {"ok": True, **self._check}

    def _claim(self):
        if self._busy:
            raise ValueError('维护任务进行中')
        guard = mutation_lock(self.root, blocking=False)
        guard.__enter__()
        try:
            access = deployment_access(self.root)
            if not access.allowed:
                raise ValueError(access.reason)
            self._busy = True
            self._mutation_guard = guard
        except BaseException:
            guard.__exit__(None, None, None)
            raise

    def _release(self):
        self._busy = False
        self._release_guard()

    def _release_guard(self):
        guard, self._mutation_guard = self._mutation_guard, None
        if guard is not None:
            guard.__exit__(None, None, None)

    async def _wait_owned(self, task):
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                break
        try:
            result = task.result()
        except Exception:
            if cancelled is not None:
                logger.exception("维护请求取消后，已启动的平台操作失败")
                raise cancelled from None
            raise
        if cancelled is not None:
            raise cancelled
        return result

    async def _run_mutation(self, operation):
        # Executor work cannot be cancelled once started. Keep ownership and the
        # mutation claim until actual I/O completes, including repeated cancel.
        task = asyncio.create_task(asyncio.to_thread(operation))
        self._mutation_tasks.add(task)
        try:
            return await self._wait_owned(task)
        finally:
            self._mutation_tasks.discard(task)

    async def create_backup(self):
        self._claim()
        try:
            return await self._run_mutation(self.backups.create)
        finally:
            self._release()
            self._invalidate_reads()

    async def start_update(self, channel, target_sha, *, confirm_channel_switch=False):
        plan = await self.plan(MaintenanceRequest('update', UpdateTarget.parse(channel, target_sha),
                                                 confirm_channel_switch=confirm_channel_switch))
        await self.execute(plan)
        job = self.read_job()
        if job is None:
            raise RuntimeError("维护受理后缺少任务记录")
        return {k: v for k, v in job.items() if k in PUBLIC_JOB_FIELDS}

    async def _validate_update(self, target, confirm_channel_switch):
        channel, target_sha = target.channel, target.commit
        # Checking can overlap an awaited Git read; bind all decisions to this
        # immutable result rather than a later check from another browser.
        checked = dict(self._check) if self._check else None
        if not checked or checked["target_sha"] != target_sha or checked["channel"] != channel:
            raise ValueError("请先检查更新，确认要安装的提交")
        current, current_channel = await asyncio.to_thread(self.installed)
        if (current != checked["current_sha"] or current_channel != checked["current_channel"]
                or self.platform.clock.time() - checked["checked_at"] > 600):
            raise ValueError("安装状态已变化或检查已过期，请重新检查更新")
        if checked["requires_confirmation"] and confirm_channel_switch is not True:
            raise ValueError("切换渠道可能降级，请明确确认后安装")
        if not checked["available"]:
            raise ValueError("当前已是该渠道最新版本")
        if not (await asyncio.to_thread(self.preflight))["supported"]:
            raise ValueError("当前部署不支持自动升级，请查看预检查结果")
    async def start_restore(self, backup_id):
        plan = await self.plan(MaintenanceRequest('restore', backup_id=backup_id))
        await self.execute(plan)
        job = self.read_job()
        if job is None:
            raise RuntimeError("维护受理后缺少任务记录")
        return {k: v for k, v in job.items() if k in PUBLIC_JOB_FIELDS}

    def _progress(self, job, stage, detail):
        if job.get("progress_stage") != stage:
            job.update(progress_stage=stage, stage_started_at=self.platform.clock.time())
        job["detail"] = detail
        self.write_job(job)

    def _prepare_environment(self, job):
        def progress(detail):
            logger.info("Maintenance preparation: %s", detail)
            self._progress(job, "download", detail)
        self._progress(job, "download", "正在下载并核对目标提交")
        self.platform.fetch_verified(self.root, job["channel"], job["target_sha"], self.platform.clock.monotonic() + 120,
                       self.platform.run_command, progress=progress)
        fetched = job["target_sha"]
        self._progress(job, "source", "正在解包并校验目标代码")
        stage_root = self.root / "data/maintenance/staging"
        stage_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=stage_root) as folder:
            stage = Path(folder)
            bundle = stage / "source.tar"
            self.platform.run_command(self.root, ["git", "archive", "--format=tar", f"--output={bundle}", fetched])
            source = stage / "source"
            source.mkdir()
            with tarfile.open(bundle) as archive:
                for member in archive.getmembers():
                    dest = source / member.name
                    if not dest.resolve().is_relative_to(source) or not (member.isfile() or member.isdir()):
                        raise ValueError("目标代码归档包含不安全路径")
                if hasattr(tarfile, "data_filter"):
                    archive.extractall(source, filter="data")
                else:
                    archive.extractall(source)
            environment = self.root / f".venv-update-{job['id']}"
            server_source = source / "src/webui/server.py"
            server_text = server_source.read_text(encoding="utf-8") if server_source.is_file() else ""
            if (not (source / "launcher.py").is_file()
                    or any(marker not in server_text for marker in ("OOPTRA_UPDATE_ID", "bootstrap_ready"))):
                raise ValueError("目标版本不支持自动升级健康检查，请按 README 手动安装")
            contract_path = source / 'src/operations/models.py'
            constants = {}
            if contract_path.is_file():
                for node in ast.parse(contract_path.read_text(encoding='utf-8')).body:
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                constants[target.id] = node.value.value
            if constants.get('DEPLOYMENT_CONTRACT_VERSION') != 2 or constants.get('CONTEXT_SCHEMA_MAX', 0) < 1:
                raise ValueError('目标版本不支持维护激活屏障或当前上下文格式，请先导出数据后手动迁移')
            self._progress(job, "environment", "正在创建独立 Python 虚拟环境")
            self.platform.run_command(self.root, [sys.executable, "-m", "venv", str(environment)], 120)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            self._progress(job, "dependencies", "正在安装 Python 依赖，网络较慢时可能需要数分钟")
            self.platform.run_command(source, [str(python), "-m", "pip", "install", "-r", "requirements.txt", "-r", "requirements-optional.txt"], 900)
            self._progress(job, "verify", "正在检查代码与依赖可用性")
            self.platform.run_command(source, [str(python), "-m", "compileall", "-q", "main.py", "src"], 60)
            self.platform.run_command(source, [str(python), "-c", "import aiohttp, pydantic, cryptography, PIL, aiosqlite, playwright, websockets"], 30)
            browsers = self.root / "data/maintenance/browsers" / job["id"]
            job["browser_path"] = str(browsers)
            env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(browsers))
            self._progress(job, "browser", "正在下载浏览器组件，网络较慢时可能需要数分钟")
            self.platform.run_command(source, [str(python), "-m", "playwright", "install", "chromium"], 900, env)
            self._progress(job, "browser_check", "正在验证浏览器组件能否启动")
            self.platform.run_command(source, [str(python), "-c", "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()"], 60, env)
            return str(python)

    async def _prepare(self, job):
        try:
            job["old_sha"] = await asyncio.to_thread(self.platform.run_command, self.root, ["git", "rev-parse", "HEAD"])
            job["new_python"] = await asyncio.to_thread(self._prepare_environment, job)
            if not (await asyncio.to_thread(self.preflight))["supported"]:
                raise ValueError("准备期间部署状态变化，取消切换")
            self._progress(job, "backup", "正在创建更新前安全备份")
            job["backup_id"] = (await asyncio.to_thread(self.backups.create))["id"]
            await self._ready(job)
        except Exception as exc:
            self._fail(job, exc)

    async def _prepare_restore(self, job):
        try:
            self._progress(job, "restore_validate", "正在校验备份完整性与恢复配置")
            def validate():
                with tempfile.TemporaryDirectory(dir=self.backups.directory) as folder:
                    self.backups.validate(job["backup_id"], Path(folder))
                    job["restore_endpoint"] = restored_endpoint(Path(folder) / "config.py")
            await asyncio.to_thread(validate)
            if (await asyncio.to_thread(self.backups.inspect, job['backup_id'])).fingerprint != job['backup_fingerprint']:
                raise ValueError('准备期间备份内容变化，取消恢复')
            self._progress(job, "backup", "正在创建恢复前安全备份")
            job["safety_backup_id"] = (await asyncio.to_thread(self.backups.create))["id"]
            await self._ready(job)
        except Exception as exc:
            self._fail(job, exc)

    async def _ready(self, job):
        if not self.runtime_control:
            raise ValueError('当前启动方式不支持安全重启')
        if self._config_revision() != job.get('config_revision', self._config_revision()):
            raise RevisionConflict('准备期间配置版本变化，已取消切换')
        job.update({'old_channel': (await asyncio.to_thread(self.installed))[1],
            'old_python': sys.executable, 'service_pid': os.getpid(), 'health_port': self.port,
            'health_token': self.token, 'health_host': self.host,
            'old_browser_path': os.environ.get('PLAYWRIGHT_BROWSERS_PATH', ''),
            'phase': 'quiescing', 'detail': '准备完成，正在静默业务资源'})
        self.write_job(job)
        try:
            result = await self.runtime_control.quiesce(job['id'], self.platform.clock.monotonic() + 60)
        except Exception as exc:
            job.update(phase='blocked_degraded', detail='业务静默异常，禁止切换；需显式重启原部署',
                       pending=['runtime'], errors=[type(exc).__name__])
            self.write_job(job)
            self._release()
            return
        job.update(closed=list(result.closed), pending=list(result.pending), errors=list(result.errors))
        if not result.complete:
            job.update(phase='blocked_degraded', detail='部分业务资源未关闭，禁止切换；需显式重启原部署')
            self.write_job(job)
            self._release()
            return
        job.update(phase='quiesced', detail='业务资源已静默，持久化父进程交接')
        self.write_job(job)
        token = uuid.uuid4().hex
        job.update(phase='awaiting_exit', handoff_token=token,
                   detail='交接已持久化，等待启动器确认原进程实际退出')
        self.write_job(job)
        handoff = HandoffRef(job['id'], job['revision'], token)
        self._release()  # The persistent awaiting_exit gate now blocks new mutations.
        try:
            response = await self.runtime_control.request_exit(handoff)
        except Exception as exc:
            job.update(phase='blocked_degraded', detail='退出通知失败，禁止切换；需显式重启原部署',
                       errors=[type(exc).__name__])
            self.write_job(job)
            return
        if not response.accepted:
            job.update(phase='blocked_degraded', detail='退出请求被拒绝，禁止切换；需显式重启原部署')
            self.write_job(job)

    def _fail(self, job, exc):
        if job.get('phase') in {'quiescing', 'quiesced', 'awaiting_exit', 'blocked_degraded'}:
            job.update(phase='blocked_degraded', detail='维护静默/交接失败，业务保持受限；需显式重启原部署')
        else:
            job.update(phase='failed', detail=str(exc) if isinstance(exc, ValueError) else f'维护准备失败（{type(exc).__name__}）；当前服务继续运行')
        self.write_job(job)
        self._release()

    async def close(self):
        for mutation in tuple(self._mutation_tasks):
            await self._wait_owned(mutation)
        if self._task and not self._task.done():
            # Preparation runs in a worker thread; join it before exiting so it cannot
            # race the supervisor's code switch. No shutdown is requested until ready.
            with contextlib.suppress(Exception):
                await self._wait_owned(self._task)
        self._release()
        for task in [*self._query_tasks.values(), self._check_task]:
            if task is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
