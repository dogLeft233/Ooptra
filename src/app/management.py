"""维护管理端口：显式静默业务资源，同时保留控制台查询与交接通道。"""

import asyncio
import time
from collections.abc import Callable

from core.paths import PROJECT_ROOT
from operations import validate_handoff
from operations.models import ExitResult, QuiesceResult, RuntimeApplyReport, RuntimeHealth


class RuntimeManagement:
    def __init__(self, voice, controller, request_stop: Callable[[], None], *, root=PROJECT_ROOT) -> None:
        self._voice, self._controller = voice, controller
        self._request_stop = request_stop
        self._root = root
        self._blocked = False
        self._quiesced = False
        self._job_id = ""
        self._closed: set[str] = set()
        self._quiesce_lock = asyncio.Lock()

    @property
    def business_allowed(self) -> bool:
        return not self._blocked

    async def apply_config(self, revision, change) -> RuntimeApplyReport:
        if self._blocked:
            return RuntimeApplyReport(failed=("maintenance_blocked",))
        if self._voice is None:
            return RuntimeApplyReport(applied=("configuration",))
        result = await self._voice.reload_settings()
        failed = tuple(result.get('failed_components', ('voice',) if result.get('hot_reload_failed') else ()))
        applied = tuple(result.get('applied_components', () if failed else tuple(result.get('changed', ()))))
        return RuntimeApplyReport(applied=applied,
                                  failed=failed,
                                  restart_required=tuple(result.get('restart_keys', ())))

    async def quiesce(self, job_id: str, deadline: float) -> QuiesceResult:
        self._blocked = True
        async with self._quiesce_lock:
            if self._job_id and self._job_id != job_id:
                return QuiesceResult(tuple(sorted(self._closed)), ("maintenance_job",), ("job_conflict",))
            self._job_id = job_id
            errors = []
            resources = [("voice", self._voice), ("channels", self._controller)]
            for name, resource in resources:
                if resource is None or name in self._closed:
                    continue
                try:
                    await asyncio.wait_for(resource.stop(), max(0.001, deadline - time.monotonic()))
                except Exception:
                    errors.append(name)
                else:
                    self._closed.add(name)
            pending = tuple(name for name, resource in resources
                            if resource is not None and name not in self._closed)
            self._quiesced = not pending and not errors
            return QuiesceResult(tuple(sorted(self._closed)), pending, tuple(errors))

    async def request_exit(self, handoff) -> ExitResult:
        if (not self._quiesced or handoff.job_id != self._job_id
                or not validate_handoff(self._root, handoff)):
            return ExitResult(False, "not_quiesced")
        self._request_stop()
        return ExitResult(True)

    def health(self) -> RuntimeHealth:
        return RuntimeHealth(ready=not self._blocked, degraded=("quiesced",) if self._blocked else ())
