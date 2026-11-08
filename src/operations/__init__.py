"""系统维护工厂、文档补丁与可信配置校验；导入不加载用户配置。"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path

from core.paths import project_path
from operations.api import Configuration

from ._internal.configuration.document import patch_config_text, validate_config_text
from ._internal.configuration.service import FileConfiguration

__all__ = ["create_configuration", "patch_config_text", "validate_config_text"]


def create_configuration(
    *, path: str | Path | None = None, runtime_config: Callable[[], object] | None = None,
) -> Configuration:
    """构建配置能力；实际保存时才读取文件和运行配置。"""
    return FileConfiguration(
        Path(path) if path is not None else Path(project_path("config.py")),
        runtime_config if runtime_config is not None else lambda: importlib.import_module("config"),
    )
