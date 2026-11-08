"""Readiness must follow local bootstrap, including optional voice failure handling."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("voice_mode", ["missing", "available", "failed"])
def test_main_publishes_ready_after_bootstrap_and_clears_before_shutdown(monkeypatch, voice_mode):
    # Plugin tests may add another checkout's main.py to sys.path. Load the
    # actual entrypoint under test explicitly so collection order is irrelevant.
    spec = importlib.util.spec_from_file_location("ooptra_bootstrap_under_test", Path(__file__).resolve().parents[1] / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    from app import assembly as runtime
    from webui.server import WebUIConsole

    observed = []
    consoles = []
    controller_started = False

    async def observe(stage):
        response = await consoles[0]._handle_status(None)
        status = json.loads(response.text)
        observed.append((stage, status["process"].get("bootstrap_ready"), status["health"]["process_ok"]))

    class Controller:
        def __init__(self, state, **ports):
            pass

        def snapshot(self):
            return {"runtime": {"supervisor_alive": controller_started},
                    "oopz": {"connected": False}, "onebot": {"connected": False}}

        async def start(self):
            nonlocal controller_started
            await observe("controller_start")
            controller_started = True

        async def stop(self):
            await observe("controller_stop")

    class Console(WebUIConsole):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            consoles.append(self)

        async def start(self):
            await observe("http_start")
            self._shutdown()

        async def stop(self):
            await observe("console_stop")

    class Voice:
        enabled = False
        agent_settings = type("Settings", (), {"enabled": False, "backend": "mimo_cascade"})()

        async def start(self):
            await observe("voice_start")
            if voice_mode == "failed":
                raise RuntimeError("Optional voice unavailable")

        async def stop(self):
            await observe("voice_stop")

    monkeypatch.setattr(main, "BridgeController", Controller)
    monkeypatch.setattr(main, "WebUIConsole", Console)
    monkeypatch.setattr(main, "_install_signal_handlers", lambda *args: None)
    monkeypatch.setattr(main, "_install_managed_control", lambda *args: None)
    monkeypatch.setattr(runtime, "get_voice_runtime", lambda: None if voice_mode == "missing" else Voice())

    # Observe the state when bootstrap releases control to the stop wait.
    original_event = asyncio.Event

    class StopEvent(original_event):
        async def wait(self):
            await observe("running")
            return await super().wait()

    monkeypatch.setattr(main.asyncio, "Event", StopEvent)
    asyncio.run(main.run())
    assert ("http_start", False, False) in observed
    assert ("controller_start", False, False) in observed
    if voice_mode != "missing":
        assert ("voice_start", False, False) in observed
        assert ("voice_stop", False, False) in observed
    assert ("running", True, True) in observed
    assert ("controller_stop", False, False) in observed
