"""通过系统维护公开入口验证持久化配置，不依赖 WebUI 或真实配置。"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from operations import create_configuration
from operations.models import OutputVolume


def test_volume_save_preserves_user_document_and_unrelated_runtime_overrides(tmp_path):
    path = tmp_path / "config.py"
    path.write_text(
        '# 保留注释\nCUSTOM = "保留"\n'
        'VOICE_AGENT_CONFIG = {"output_volume": 50, "persona": "disk persona"}\n',
        encoding="utf-8",
    )
    runtime = SimpleNamespace(VOICE_AGENT_CONFIG={"output_volume": 50, "persona": "env persona"})
    configuration = create_configuration(path=path, runtime_config=lambda: runtime)

    result = asyncio.run(configuration.save_output_volume(OutputVolume.parse("37")))

    assert result.volume == 37
    assert runtime.VOICE_AGENT_CONFIG == {"output_volume": 37, "persona": "env persona"}
    saved = path.read_text(encoding="utf-8")
    assert '# 保留注释' in saved and 'CUSTOM = "保留"' in saved
    namespace = {}
    exec(compile(saved, str(path), "exec"), namespace)
    assert namespace["VOICE_AGENT_CONFIG"] == {"output_volume": 37, "persona": "disk persona"}


def test_invalid_volume_never_changes_file_or_runtime(tmp_path):
    path = tmp_path / "config.py"
    original = 'VOICE_AGENT_CONFIG = {"output_volume": 50}\n'
    path.write_text(original, encoding="utf-8")
    runtime = SimpleNamespace(VOICE_AGENT_CONFIG={"output_volume": 50})
    configuration = create_configuration(path=path, runtime_config=lambda: runtime)

    with pytest.raises(ValueError):
        asyncio.run(configuration.save_output_volume(OutputVolume.parse(101)))

    assert path.read_text(encoding="utf-8") == original
    assert runtime.VOICE_AGENT_CONFIG == {"output_volume": 50}


def test_failed_file_replace_does_not_change_running_volume(tmp_path, monkeypatch):
    import os

    path = tmp_path / "config.py"
    original = 'VOICE_AGENT_CONFIG = {"output_volume": 50}\n'
    path.write_text(original, encoding="utf-8")
    runtime = SimpleNamespace(VOICE_AGENT_CONFIG={"output_volume": 50})
    configuration = create_configuration(path=path, runtime_config=lambda: runtime)

    def unavailable(*args):
        raise OSError("synthetic disk unavailable")

    monkeypatch.setattr(os, "replace", unavailable)
    with pytest.raises(OSError, match="synthetic disk unavailable"):
        asyncio.run(configuration.save_output_volume(OutputVolume(37)))

    assert path.read_text(encoding="utf-8") == original
    assert runtime.VOICE_AGENT_CONFIG == {"output_volume": 50}


def test_dynamic_voice_target_is_rejected_before_configuration_execution(tmp_path):
    path = tmp_path / "config.py"
    marker = tmp_path / "must_not_exist"
    original = (
        'VOICE_AGENT_CONFIG = {"output_volume": 50}\n'
        'VOICE_AGENT_CONFIG.update({"output_volume": 60})\n'
        f'open({str(marker)!r}, "w").close()\n'
    )
    path.write_text(original, encoding="utf-8")
    runtime = SimpleNamespace(VOICE_AGENT_CONFIG={"output_volume": 50})
    configuration = create_configuration(path=path, runtime_config=lambda: runtime)

    with pytest.raises(RuntimeError, match="不是唯一的字典字面量"):
        asyncio.run(configuration.save_output_volume(OutputVolume(37)))

    assert not marker.exists()
    assert path.read_text(encoding="utf-8") == original
    assert runtime.VOICE_AGENT_CONFIG == {"output_volume": 50}


@pytest.mark.parametrize("failed", [False, True])
def test_cancelled_save_observes_disk_completion_and_releases_storage(tmp_path, monkeypatch, failed):
    import os

    path = tmp_path / "config.py"
    original = 'VOICE_AGENT_CONFIG = {"output_volume": 50}\n'
    path.write_text(original, encoding="utf-8")
    runtime = SimpleNamespace(VOICE_AGENT_CONFIG={"output_volume": 50})
    configuration = create_configuration(path=path, runtime_config=lambda: runtime)
    entered, release = threading.Event(), threading.Event()
    real_replace = os.replace

    def replace(*args):
        entered.set()
        assert release.wait(5)
        if failed:
            raise OSError("synthetic disk unavailable")
        return real_replace(*args)

    monkeypatch.setattr(os, "replace", replace)

    async def run():
        task = asyncio.create_task(configuration.save_output_volume(OutputVolume(37)))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(OSError if failed else asyncio.CancelledError):
            await task
        assert runtime.VOICE_AGENT_CONFIG["output_volume"] == (50 if failed else 37)
        if failed:
            assert path.read_text(encoding="utf-8") == original
        else:
            assert '37' in path.read_text(encoding="utf-8")
        monkeypatch.setattr(os, "replace", real_replace)
        assert (await configuration.save_output_volume(OutputVolume(42))).volume == 42
        assert runtime.VOICE_AGENT_CONFIG["output_volume"] == 42

    asyncio.run(run())
