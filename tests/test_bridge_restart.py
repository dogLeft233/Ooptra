"""公开平台网关在真实SDK装配上验证重启；只替换外部SDK连接循环。"""
import asyncio

from cryptography.hazmat.primitives.asymmetric import rsa

from channels import create_platform_gateway
from oopz_sdk import OopzBot


class Configuration:
    def __init__(self, path):
        self.path = path
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    def snapshot(self):
        return ({"device_id": "synthetic", "person_uid": "synthetic", "jwt_token": "synthetic"},
                {"enabled": True, "db_path": str(self.path), "enable_http": False, "enable_ws": False,
                 "enable_ws_reverse": False, "heartbeat_enabled": False}, {})
    def load_private_key(self):
        return self.key
    def login_account(self):
        return "", ""
    async def persist_credentials(self, credentials):
        return []


async def settle_until(predicate):
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(.001)
    raise AssertionError("平台网关未在期限内到达可观察状态")


def test_restart_returns_supervisor_to_blocking_wait(tmp_path):
    async def run():
        started, stopped = [], []
        class SDK(OopzBot):
            async def start(self):
                started.append(self)
                await asyncio.Event().wait()
            async def stop(self):
                stopped.append(self)
                await super().stop()
        controller = create_platform_gateway(configuration=Configuration(tmp_path / "ids.sqlite3"), sdk_factory=SDK)
        await controller.start()
        try:
            await settle_until(lambda: len(started) == 1)
            await controller.restart("first")
            await settle_until(lambda: len(started) == 2)
            await asyncio.sleep(.02)
            assert len(started) == 2 and len(stopped) == 1
            await controller.restart("second")
            await settle_until(lambda: len(started) == 3)
            assert controller.snapshot()["runtime"]["restarts"] == 2
        finally:
            await controller.stop()
        assert len(stopped) == 3
    asyncio.run(run())


def test_restart_wakes_retry_wait_after_external_startup_failure(tmp_path):
    async def run():
        started = []
        class SDK(OopzBot):
            async def start(self):
                started.append(self)
                if len(started) == 1:
                    raise OSError("external connection unavailable")
                await asyncio.Event().wait()
        controller = create_platform_gateway(configuration=Configuration(tmp_path / "ids.sqlite3"), sdk_factory=SDK)
        await controller.start()
        try:
            await settle_until(lambda: bool(controller.snapshot()["runtime"]["last_error"]))
            await controller.restart("new credentials")
            await settle_until(lambda: len(started) == 2)
            assert controller.running
        finally:
            await controller.stop()
    asyncio.run(run())


def test_restart_during_sdk_start_is_processed(tmp_path):
    async def run():
        entered = asyncio.Event()
        started = []
        class SDK(OopzBot):
            async def start(self):
                started.append(self)
                entered.set()
                await asyncio.Event().wait()
        controller = create_platform_gateway(configuration=Configuration(tmp_path / "ids.sqlite3"), sdk_factory=SDK)
        await controller.start()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await controller.restart("startup notification")
            await settle_until(lambda: len(started) == 2)
            assert controller.snapshot()["runtime"]["restarts"] == 1
        finally:
            await controller.stop()
    asyncio.run(run())
