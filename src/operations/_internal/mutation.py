"""部署变更文件锁与持久化激活屏障；不持锁等待业务资源关闭。"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from operations.models import DeploymentAccess

ACTIVE_PHASES = frozenset({
    'preparing', 'awaiting_restart', 'quiescing', 'quiesced', 'awaiting_exit',
    'switching', 'checking', 'rolling_back', 'blocked_degraded', 'interrupted',
})


def deployment_access(root) -> DeploymentAccess:
    path = Path(root) / 'data/maintenance/job.json'
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return DeploymentAccess(True)
    except (OSError, ValueError):
        return DeploymentAccess(False, '维护记录损坏或不可读，需先核对恢复', '')
    if (not isinstance(raw, dict) or not isinstance(raw.get('phase'), str)
            or not isinstance(raw.get('id'), str)):
        return DeploymentAccess(False, '维护记录结构无效，需先核对恢复', '')
    phase = raw['phase']
    if phase in ACTIVE_PHASES:
        return DeploymentAccess(False, '维护任务进行中或需要恢复，暂不接受业务变更', raw['id'])
    if phase not in {'complete', 'rolled_back', 'failed'}:
        return DeploymentAccess(False, '维护阶段未知，需先核对恢复', raw['id'])
    return DeploymentAccess(True, '', raw['id'])


@contextmanager
def mutation_lock(root: Path, *, blocking=True):
    """同一部署跨进程串行化配置提交、任务受理和阶段 CAS。

    OS 自动在进程退出时释放锁；不以 PID 文件猜测锁是否失效。
    """
    folder = Path(root) / 'data/maintenance'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / 'mutation.lock'
    if folder.is_symlink() or target.is_symlink():
        raise ValueError('维护锁路径不能是链接')
    with target.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if target.stat().st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError('维护或配置变更进行中') from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise ValueError('维护或配置变更进行中') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


# A scoped capability is propagated by asyncio.to_thread to the file transaction.
# Revocation is shared, so a child created in the scope cannot retain it after exit.


@dataclass
class _MutationLease:
    root: Path
    active: bool = True


_LEASE: ContextVar[_MutationLease | None] = ContextVar('operations_mutation_lease', default=None)


def owns_mutation(root) -> bool:
    lease = _LEASE.get()
    return bool(lease is not None and lease.active and lease.root == Path(root).resolve())


async def configuration_transaction(root, operation):
    """在同一个跨进程许可中保存并应用配置；取消等待者仍观察已受理工作。"""
    import asyncio

    async def execute():
        guard = mutation_lock(Path(root).resolve())
        await asyncio.to_thread(guard.__enter__)
        lease = _MutationLease(Path(root).resolve())
        context_token = _LEASE.set(lease)
        try:
            access = deployment_access(root)
            if not access.allowed:
                raise ValueError(access.reason)
            return await operation()
        finally:
            lease.active = False
            _LEASE.reset(context_token)
            # No await between revocation and release: cancellation cannot leak a file lock.
            guard.__exit__(None, None, None)

    task = asyncio.create_task(execute(), name='operations-config-transaction')
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            break
    if cancelled is not None:
        try:
            task.result()
        except Exception:
            import logging
            logging.getLogger(__name__).exception('配置请求取消后事务失败')
        raise cancelled
    return task.result()
