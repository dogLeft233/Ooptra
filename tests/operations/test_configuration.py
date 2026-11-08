"""通过系统维护公开入口验证持久化配置，不依赖 WebUI 或真实配置。"""

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

    result = configuration.save_output_volume(OutputVolume.parse("37"))

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
        configuration.save_output_volume(OutputVolume.parse(101))

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
        configuration.save_output_volume(OutputVolume(37))

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
        configuration.save_output_volume(OutputVolume(37))

    assert not marker.exists()
    assert path.read_text(encoding="utf-8") == original
    assert runtime.VOICE_AGENT_CONFIG == {"output_volume": 50}


def test_normalized_save_updates_multiple_groups_without_losing_environment_values(tmp_path):
    from operations.models import ConfigurationChange
    path = tmp_path / "config.py"
    path.write_text(
        '# user comment\nVOICE_AGENT_CONFIG = {"gemini": {"voice": "old", "model": "disk"}}\n'
        'WEBUI_CONFIG = {"port": 3090, "host": "127.0.0.1"}\n', encoding="utf-8")
    runtime = SimpleNamespace(
        VOICE_AGENT_CONFIG={"gemini": {"voice": "old", "model": "environment"}},
        WEBUI_CONFIG={"port": 3090, "host": "environment-host"})
    change = ConfigurationChange.from_normalized(
        {"voice": {"gemini.voice": "new"}, "webui": {"port": 4090}},
        sources={"voice": "VOICE_AGENT_CONFIG", "webui": "WEBUI_CONFIG"})
    receipt = create_configuration(path=path, runtime_config=lambda: runtime).save_fields(change)
    assert receipt.changed_fields() == {"voice": ["gemini.voice"], "webui": ["port"]}
    assert runtime.VOICE_AGENT_CONFIG == {"gemini": {"voice": "new", "model": "environment"}}
    assert runtime.WEBUI_CONFIG == {"port": 4090, "host": "environment-host"}
    assert '# user comment' in path.read_text(encoding="utf-8")


def test_parsed_configuration_owns_snapshot_of_nested_inputs(tmp_path):
    from operations.models import ConfigurationChange
    path = tmp_path / "config.py"
    path.write_text('VOICE_AUTO_VISIT_CONFIG = {}\n', encoding="utf-8")
    raw = {"auto_visit": {"defaults": {"enter_prompts": ["original"]}}}
    change = ConfigurationChange.from_normalized(raw, sources={"auto_visit": "VOICE_AUTO_VISIT_CONFIG"})
    raw["auto_visit"]["defaults"]["enter_prompts"].append("late mutation")
    copy = change.to_updates()
    copy["auto_visit"]["defaults"]["enter_prompts"].append("also late")
    runtime = SimpleNamespace()
    create_configuration(path=path, runtime_config=lambda: runtime).save_fields(change)
    assert runtime.VOICE_AUTO_VISIT_CONFIG == {"defaults": {"enter_prompts": ["original"]}}
