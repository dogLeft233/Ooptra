"""维护回归的平台注入适配；测试不导入 operations 的内部模块。"""

import asyncio
import os
import subprocess
import tempfile
import time
from types import SimpleNamespace

import requests

from core.version import __version__
from operations import (
    create_backup_store,
    create_maintenance,
    create_maintenance_platform,
    create_storage_scanner,
    create_supervisor,
    create_update_transport,
    restored_endpoint,
    write_maintenance_record,
)
from operations.models import MaintenancePlatform, NetworkSettings, validate_network_field
from webui.maintenance import mount_maintenance_routes

_default_platform = create_maintenance_platform()
_default_network = create_update_transport()
maintenance_network = SimpleNamespace(
    os=os, requests=requests, time=time,
    OFFICIAL_URL="https://github.com/Eason4869/Ooptra.git",
    NetworkSettings=NetworkSettings, validate_network_field=validate_network_field,
    api_lookup=None,
    load_network_settings=_default_network.load_network_settings,
)


def network_call(method, *args, **kwargs):
    transport = create_update_transport(
        clock=maintenance_network.time,
        api_lookup=maintenance_network.api_lookup,
    )
    return getattr(transport, method)(*args, **kwargs)


for _method in ("lookup_official_sha", "fetch_verified", "test_mirror_connection"):
    setattr(maintenance_network, _method,
            lambda *a, _name=_method, **kw: network_call(_name, *a, **kw))

maintenance_cleanup = SimpleNamespace(time=time)
maintenance_cleanup.inventory = lambda *a, **kw: create_storage_scanner(
    clock=maintenance_cleanup.time).inventory(*a, **kw)

maintenance_storage = SimpleNamespace(os=os, time=time, atomic_json=write_maintenance_record,
                                      BackupStore=create_backup_store, restored_endpoint=restored_endpoint)

_default_supervisor = create_supervisor()
maintenance_worker = SimpleNamespace(subprocess=subprocess, BackupStore=create_backup_store)
for _method in ("spawn", "stop_owned", "healthy", "checkout", "runtime_health"):
    setattr(maintenance_worker, _method, getattr(_default_supervisor, _method))


def supervisor_call(method, *args, **kwargs):
    supervisor = create_supervisor(
        spawn=lambda *a, **kw: maintenance_worker.spawn(*a, **kw),
        stop_owned=lambda *a, **kw: maintenance_worker.stop_owned(*a, **kw),
        healthy=lambda *a, **kw: maintenance_worker.healthy(*a, **kw),
        checkout=lambda *a, **kw: maintenance_worker.checkout(*a, **kw),
    )
    return getattr(supervisor, method)(*args, **kwargs)


for _method in ("switch", "supervise"):
    setattr(maintenance_worker, _method,
            lambda *a, _name=_method, **kw: supervisor_call(_name, *a, **kw))

maintenance = SimpleNamespace(
    subprocess=subprocess, time=time, tempfile=tempfile, asyncio=asyncio,
    run_command=_default_platform.run_command,
    inventory=maintenance_cleanup.inventory,
    fetch_verified=maintenance_network.fetch_verified,
    lookup_official_sha=maintenance_network.lookup_official_sha,
    test_mirror_connection=maintenance_network.test_mirror_connection,
    QUERY_WAIT_SECONDS=12, PREFLIGHT_SECONDS=10, INSTALLED_SECONDS=4, CHECK_SECONDS=60,
    __version__=__version__, mount_maintenance_routes=mount_maintenance_routes,
)


def MaintenanceService(*args, **kwargs):
    platform = MaintenancePlatform(
        run_command=lambda *a, **kw: maintenance.run_command(*a, **kw),
        inventory=lambda *a, **kw: maintenance.inventory(*a, **kw),
        delete_items=_default_platform.delete_items,
        lookup_official_sha=lambda *a, **kw: maintenance.lookup_official_sha(*a, **kw),
        fetch_verified=lambda *a, **kw: maintenance.fetch_verified(*a, **kw),
        runtime_health=_default_platform.runtime_health,
        clock=SimpleNamespace(monotonic=lambda: maintenance.time.monotonic(),
                              time=lambda: maintenance.time.time()),
        version=maintenance.__version__,
        test_mirror_connection=lambda *a, **kw: maintenance.test_mirror_connection(*a, **kw),
    )
    # Tuning may be changed after construction by deadline tests.
    class DynamicTuning:
        def __getattr__(self, key):
            if key in {"query_wait_seconds", "preflight_seconds", "installed_seconds", "check_seconds"}:
                return getattr(maintenance, key.upper())
            return getattr(platform, key)
    return create_maintenance(*args, platform=DynamicTuning(), **kwargs)


maintenance.MaintenanceService = MaintenanceService


def switch_stopped(root, job):
    """隔离部署用已停止身份夹具；确认真实进程的契约另行测试。"""
    from operations.models import StoppedDeployment
    job.update(schema=2, revision=1, owner='application', phase='awaiting_exit',
               service_pid=1, service_start_marker='isolated-process', handoff_token='isolated-handoff')
    if job.get('action') == 'restore':
        job['backup_fingerprint'] = create_backup_store(root).inspect(job['backup_id']).fingerprint
    write_maintenance_record(root / 'data/maintenance/job.json', job)
    stopped = StoppedDeployment(job['id'], 1, 'isolated-process', 'isolated-handoff')
    return maintenance_worker.switch(root, job, stopped)
