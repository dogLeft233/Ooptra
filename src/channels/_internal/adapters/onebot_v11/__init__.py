"""项目对内置 Oopz-SDK OneBot v11 的配置、迁移与能力补丁。"""

from channels._internal.adapters.onebot_v11.config import (
    OneBotV11ServerConfig,
    get_onebot_v11_config,
)
from channels._internal.adapters.onebot_v11.sdk_integration import (
    OneBotV11Supplement,
    find_sdk_onebot_v11,
)
from channels._internal.adapters.onebot_v11.sdk_migration import migrate_onebot_v11_database

__all__ = [
    "OneBotV11ServerConfig",
    "OneBotV11Supplement",
    "find_sdk_onebot_v11",
    "get_onebot_v11_config",
    "migrate_onebot_v11_database",
]
