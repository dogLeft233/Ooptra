"""停止管理面保存任务后才释放业务资源，避免停机期间热重载重开。"""
import asyncio

import main
from app import assembly as runtime


def test_shutdown_drains_console_before_stopping_runtime_and_bridge(monkeypatch):
    stopped = []
    class Controller:
        def __init__(self, state, **kwargs):
            pass
        async def start(self):
            pass
        async def stop(self):
            assert stopped == ["console", "voice"]
            stopped.append("bridge")
    class Voice:
        enabled = False
        async def start(self):
            pass
        async def stop(self):
            assert stopped == ["console"]
            stopped.append("voice")
    class Console:
        bind_address = None
        def __init__(self, *args, shutdown, **kwargs):
            self.shutdown = shutdown
        async def start(self):
            self.shutdown()
        def set_bootstrap_ready(self, value):
            pass
        def set_runtime_control(self, value):
            pass
        async def stop(self):
            stopped.append("console")
    monkeypatch.setattr(main, "BridgeController", Controller)
    monkeypatch.setattr(main, "WebUIConsole", Console)
    monkeypatch.setattr(main, "_install_signal_handlers", lambda event: None)
    monkeypatch.setattr(main, "_install_managed_control", lambda event: None)
    monkeypatch.setattr(runtime, "get_voice_runtime", lambda: Voice())
    asyncio.run(main.run())
    assert stopped == ["console", "voice", "bridge"]
