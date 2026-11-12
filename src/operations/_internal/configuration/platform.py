"""平台装配只读配置与原子凭据持久化；不接触SDK对象。"""
import copy
import logging
import os

from operations._internal.configuration.credentials import CredentialStore

logger = logging.getLogger(__name__)


class PlatformConfigurationStore:
    def __init__(self, *, config_path=None, private_key_path=None, runtime_config=None):
        self._store = CredentialStore(config_path=config_path, private_key_path=private_key_path,
                                      runtime_config=runtime_config)
        self._runtime_config = runtime_config

    def snapshot(self):
        import config
        config = self._runtime_config() if self._runtime_config is not None else config
        oopz = copy.deepcopy(getattr(config, "OOPZ_CONFIG", {}))
        oopz["voice_browser_executable_path"] = os.environ.get("BOT_CHROMIUM_EXECUTABLE_PATH") or os.environ.get("CHROME_BIN") or ""
        onebot = copy.deepcopy(getattr(config, "ONEBOT_V11_CONFIG", {}))
        enabled = os.environ.get("ONEBOT_V11_ENABLED")
        if enabled is not None:
            onebot["enabled"] = enabled.strip().lower() in {"1", "true", "yes", "on", "y"}
        return (oopz, onebot, copy.deepcopy(getattr(config, "DEFAULT_HEADERS", {})))

    def load_private_key(self):
        try:
            return self._store.load_private_key()
        except Exception:
            logger.warning("event=configuration.private_key.unavailable")
            return None

    def login_account(self):
        oopz, _, _ = self.snapshot()
        return (str(os.environ.get("OOPZ_PHONE") or oopz.get("login_phone") or oopz.get("phone") or "").strip(),
                str(os.environ.get("OOPZ_PASSWORD") or oopz.get("login_password") or oopz.get("password") or ""))

    async def persist_credentials(self, credentials):
        return await self._store.persist(credentials)
