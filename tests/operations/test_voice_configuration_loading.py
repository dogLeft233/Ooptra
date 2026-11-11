"""通过公开配置入口验证环境优先级和导入边界。"""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from operations import load_voice_agent_settings
from operations.models import VoiceAgentSettings, VoiceApiSettings


def test_explicit_sources_preserve_environment_priority_and_compatibility_defaults():
    config = SimpleNamespace(
        VOICE_AGENT_CONFIG={
            "backend": "aliyun_deepseek", "output_volume": "700", "cascade_turn_limit": -1,
            "force_reply_keywords": "小蓝, , 小橙", "mimo": {"api_key": "disk-mimo"},
        },
        VOICE_API_CONFIG={"enabled": True, "token": "unused-old-token"},
        WEBUI_CONFIG={"token": " shared-token "},
    )
    settings, api = load_voice_agent_settings(
        runtime_config=config,
        environ={"MIMO_API_KEY": "env-mimo", "DEEPSEEK_API_KEY": "env-deepseek"},
        local_environment={"DASHSCOPE_API_KEY": "local-dashscope", "DEEPSEEK_API_KEY": "local-deepseek"},
    )
    assert settings.backend == "aliyun_deepseek"
    assert settings.mimo_api_key == "env-mimo"
    assert settings.deepseek_api_key == "env-deepseek"
    assert settings.dashscope_api_key == "local-dashscope"
    assert settings.output_volume == 100
    assert settings.cascade_turn_limit == 0
    assert settings.force_reply_keywords == ["小蓝", "小橙"]
    assert api.enabled and api.token == "shared-token"


def test_public_configuration_models_hide_secret_values_in_repr():
    settings = VoiceAgentSettings(mimo_api_key="synthetic-mimo", gemini_api_key="synthetic-gemini",
                                  openai_api_key="synthetic-openai",
                                  proxy="http://user:synthetic-proxy-secret@localhost:7890")
    assert "synthetic" not in repr(settings)
    assert "synthetic" not in repr(VoiceApiSettings(token="synthetic-token"))


def test_importing_configuration_capability_never_loads_deployment_secrets():
    source = Path(__file__).resolve().parents[2] / "src"
    script = '''
import importlib.abc
import sys
class RejectConfigurationImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"config", "dotenv"}:
            raise AssertionError("configuration imported before explicit load")
sys.meta_path.insert(0, RejectConfigurationImport())
import operations
from operations.models import VoiceAgentSettings
assert callable(operations.load_voice_agent_settings)
assert VoiceAgentSettings().enabled is False
'''
    result = subprocess.run([sys.executable, "-c", script], env={**os.environ, "PYTHONPATH": str(source)},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_reasoning_effort_saves_and_loads_through_public_configuration(tmp_path):
    from operations import create_configuration
    from operations.models import ConfigurationChange
    from webui.config_editor import GROUP_SOURCES, _normalize_updates

    path = tmp_path / "config.py"
    path.write_text('VOICE_AGENT_CONFIG = {"aliyun_deepseek": {}}\n')
    runtime = SimpleNamespace(VOICE_AGENT_CONFIG={"aliyun_deepseek": {}})
    service = create_configuration(path=path, runtime_config=lambda: runtime)
    for effort in ("none", "low", "high", "max"):
        update = _normalize_updates({"voice": {"aliyun_deepseek.reasoning_effort": effort}})
        service.save_fields(ConfigurationChange.from_normalized(update, sources=GROUP_SOURCES))
        loaded, _ = load_voice_agent_settings(runtime_config=runtime, environ={}, local_environment={})
        assert loaded.deepseek_reasoning_effort == effort
        disk = {}
        exec(path.read_text(), disk)
        assert disk["VOICE_AGENT_CONFIG"]["aliyun_deepseek"]["reasoning_effort"] == effort


def test_invalid_reasoning_effort_is_rejected_at_configuration_boundary():
    import pytest

    from webui.config_editor import _normalize_updates

    with pytest.raises(ValueError):
        _normalize_updates({"voice": {"aliyun_deepseek.reasoning_effort": "typo"}})
    with pytest.raises(ValueError):
        load_voice_agent_settings(runtime_config=SimpleNamespace(
            VOICE_AGENT_CONFIG={"aliyun_deepseek": {"reasoning_effort": "typo"}}),
            environ={}, local_environment={})
    defaults, _ = load_voice_agent_settings(runtime_config=SimpleNamespace(), environ={}, local_environment={})
    assert defaults.deepseek_reasoning_effort == "low"


def test_audio_wait_settings_roundtrip_and_invalid_values_are_rejected(tmp_path):
    import pytest

    from operations import create_configuration
    from operations.models import ConfigurationChange
    from webui.config_editor import GROUP_SOURCES, _normalize_updates

    path = tmp_path / 'config.py'
    path.write_text('VOICE_AGENT_CONFIG = {}\n')
    runtime = SimpleNamespace(VOICE_AGENT_CONFIG={})
    service = create_configuration(path=path, runtime_config=lambda: runtime)
    values = {"first_audio_timeout_seconds": 50, "audio_gap_timeout_seconds": 7}
    update = _normalize_updates({"voice": values})
    service.save_fields(ConfigurationChange.from_normalized(update, sources=GROUP_SOURCES))
    loaded, _ = load_voice_agent_settings(runtime_config=runtime, environ={}, local_environment={})
    assert loaded.first_audio_timeout_seconds == 50 and loaded.audio_gap_timeout_seconds == 7
    for key in values:
        for value in (True, 0, "bad", 121):
            with pytest.raises(ValueError):
                load_voice_agent_settings(runtime_config=SimpleNamespace(VOICE_AGENT_CONFIG={key:value}),
                    environ={}, local_environment={})
