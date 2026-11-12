"""系统维护工厂、文档补丁与可信配置校验；导入不加载用户配置。"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from pathlib import Path

from core.paths import project_path
from operations.api import (
    BackupArchive,
    Configuration,
    ConfigurationGroupMerger,
    Maintenance,
    StorageScanner,
    Supervisor,
    UpdateTransport,
)
from operations.models import MaintenancePlatform, VoiceAgentSettings, VoiceApiSettings

from ._internal.configuration.document import patch_config_text, validate_config_text
from ._internal.configuration.service import FileConfiguration

__all__ = ["create_configuration", "patch_config_text", "validate_config_text"]


def create_configuration(
    *, path: str | Path | None = None, runtime_config: Callable[[], object] | None = None,
    group_mergers: Mapping[str, ConfigurationGroupMerger] | None = None,
) -> Configuration:
    """构建配置能力；实际保存时才读取文件和运行配置。"""
    return FileConfiguration(
        Path(path) if path is not None else Path(project_path("config.py")),
        runtime_config if runtime_config is not None else lambda: importlib.import_module("config"),
        group_mergers=group_mergers,
    )


def create_backup_store(root) -> BackupArchive:
    """构建真实备份归档能力；不恢复、不复制用户数据。"""
    from ._internal.backups.storage import BackupStore
    return BackupStore(root)


def create_storage_scanner(*, clock=None) -> StorageScanner:
    """构建有界存储盘点/清理能力。"""
    from ._internal.backups.cleanup import StorageScanner
    return StorageScanner(clock=clock)


def create_update_transport(*, command_runner=None, clock=None, api_lookup=None) -> UpdateTransport:
    """构建更新传输；构建时不加载用户配置或发起网络请求。"""
    from ._internal.deployment.network import UpdateTransport
    from ._internal.deployment.service import run_command
    return UpdateTransport(command_runner=command_runner or run_command, clock=clock, api_lookup=api_lookup)


def create_supervisor(*, spawn=None, stop_owned=None, healthy=None, checkout=None) -> Supervisor:
    """构建受管父进程能力；只在显式调用后启动/停止进程。"""
    from ._internal.deployment.supervisor import ProcessSupervisor
    return ProcessSupervisor(spawn=spawn, stop_owned=stop_owned, healthy=healthy, checkout=checkout)


def evaluate_runtime_health(status, *, expected_pid=None, update_id=None, expected_python=None):
    """本地启动和进程身份判定；外部平台离线仅为降级。"""
    return create_supervisor().runtime_health(status, expected_pid=expected_pid, update_id=update_id,
                                               expected_python=expected_python)


def create_maintenance_platform() -> MaintenancePlatform:
    """为维护应用装配平台端口；不拥有任务事实或 HTTP 状态。"""
    import time

    from core.version import __version__

    from ._internal.deployment.service import run_command
    scanner = create_storage_scanner()
    transport = create_update_transport()
    return MaintenancePlatform(run_command, scanner.inventory, scanner.delete_items,
                               transport.lookup_official_sha, transport.fetch_verified,
                               evaluate_runtime_health, time, __version__, transport.test_mirror_connection)


def create_maintenance(root, *, shutdown=None, managed=None, venv=None, port=3090,
                       host="127.0.0.1", token="", runtime_status=None, bootstrap_ready=None,
                       platform: MaintenancePlatform | None = None) -> Maintenance:
    """创建维护能力；应用注入停止和运行状态端口，WebUI 只调用此门面。"""
    from ._internal.deployment.service import MaintenanceService
    return MaintenanceService(root, shutdown=shutdown, managed=managed, venv=venv, port=port,
                              host=host, token=token, runtime_status=runtime_status,
                              bootstrap_ready=bootstrap_ready,
                              platform=platform or create_maintenance_platform())


def write_maintenance_record(path, value):
    """原子替换兼容任务记录；不提供跨进程锁或 CAS。"""
    from ._internal.backups.storage import atomic_json
    return atomic_json(path, value)


def restored_endpoint(path):
    """只读取备份中的字面量管理端点，绝不执行备份配置。"""
    from ._internal.backups.storage import restored_endpoint as read_endpoint
    return read_endpoint(path)


__all__ += [
    "create_backup_store",
    "create_maintenance",
    "create_maintenance_platform",
    "create_storage_scanner",
    "create_supervisor",
    "create_update_transport",
    "evaluate_runtime_health",
    "restored_endpoint",
    "write_maintenance_record",
]


def load_voice_agent_settings(
    *, runtime_config=None, environ=None, local_environment=None, local_environment_path=None,
) -> tuple[VoiceAgentSettings, VoiceApiSettings]:
    """显式解析部署配置与环境，返回语音应用/API配置；导入不加载凭据。"""
    from ._internal.configuration.settings import load_voice_agent_settings as load_settings
    return load_settings(runtime_config=runtime_config, environ=environ,
                         local_environment=local_environment,
                         local_environment_path=local_environment_path)


def apply_settings(target, fresh) -> list[str]:
    """就地应用已加载配置并返回变化字段，维持运行对象身份。"""
    from ._internal.configuration.settings import apply_settings as update_settings
    return update_settings(target, fresh)


def resolve_agent_proxy_url(proxy: str) -> str | None:
    """将配置代理选择转换为模型客户端使用的URL；不发起网络请求。"""
    from ._internal.configuration.settings import resolve_agent_proxy_url as resolve_proxy
    return resolve_proxy(proxy)


__all__ += ["apply_settings", "load_voice_agent_settings", "resolve_agent_proxy_url"]
