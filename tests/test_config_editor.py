
"""config.py 就地改写的纯逻辑测试：不启动服务，也不需要 config.py 真的存在。"""

from __future__ import annotations

import ast

import pytest

from operations import patch_config_text, validate_config_text
from webui import config_editor as editor

SAMPLE = '''\
"""示例配置：这行模块 docstring 与下面的注释都必须原样保留"""
OOPZ_CONFIG = {
    # 这是一条必须保留的注释
    "login_phone": "",
    "proxy": "",
}
ONEBOT_V11_CONFIG = {
    "ws_reverse_url": "ws://127.0.0.1:6200/ws",
    "http_post_urls": [],
}
'''


def test_normalize_rejects_unknown_group():
    with pytest.raises(ValueError):
        editor._normalize_updates({"nope": {"a": 1}})


def test_normalize_rejects_unknown_field():
    with pytest.raises(ValueError):
        editor._normalize_updates({"webui": {"nope": 1}})


def test_normalize_rejects_readonly_field():
    """凭据字段只能由登录流程写回，配置接口不许改。"""
    with pytest.raises(ValueError):
        editor._normalize_updates({"oopz": {"app_version": "1"}})


def test_normalize_drops_blank_sensitive_value():
    """敏感项留空表示「不修改」，只应用其它的真实改动。"""
    normalized = editor._normalize_updates({"webui": {"token": None, "log_lines": 400}})
    assert normalized == {"webui": {"log_lines": 400}}


def test_normalize_keeps_sensitive_value_when_present():
    assert editor._normalize_updates({"webui": {"token": "abc"}}) == {"webui": {"token": "abc"}}


def test_normalize_enforces_prefix():
    with pytest.raises(ValueError):
        editor._normalize_updates({"onebot": {"ws_reverse_url": "http://127.0.0.1/ws"}})


def test_normalize_enforces_bounds():
    with pytest.raises(ValueError):
        editor._normalize_updates({"webui": {"log_lines": 10}})


def test_normalize_splits_list_field():
    normalized = editor._normalize_updates({"onebot": {"http_post_urls": "a, b ,c"}})
    assert normalized["onebot"]["http_post_urls"] == ["a", "b", "c"]


def test_normalize_rejects_empty_change_set():
    with pytest.raises(ValueError):
        editor._normalize_updates({"webui": {"token": None}})


def test_patched_text_preserves_comments_and_untouched_keys():
    patched = patch_config_text(SAMPLE, {"oopz": {"proxy": "direct"}}, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    assert "这是一条必须保留的注释" in patched
    assert '"ws_reverse_url": "ws://127.0.0.1:6200/ws"' in patched
    ast.parse(patched)
    namespace: dict[str, object] = {}
    exec(compile(patched, "<patched>", "exec"), namespace)
    oopz = namespace["OOPZ_CONFIG"]
    assert isinstance(oopz, dict)
    assert oopz["proxy"] == "direct"
    assert oopz["login_phone"] == ""


def test_patched_text_only_touches_target():
    patched = patch_config_text(SAMPLE, {"onebot": {"ws_reverse_url": "ws://x/ws"}}, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    assert '"ws_reverse_url": "ws://x/ws"' in patched
    assert '"proxy": ""' in patched


def test_patched_text_inserts_missing_field():
    patched = patch_config_text(SAMPLE, {"oopz": {"default_area": "A1"}}, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    namespace: dict[str, object] = {}
    exec(compile(patched, "<patched>", "exec"), namespace)
    oopz = namespace["OOPZ_CONFIG"]
    assert isinstance(oopz, dict)
    assert oopz["default_area"] == "A1"
    assert oopz["proxy"] == ""


def test_validate_text_rejects_broken_syntax():
    with pytest.raises(RuntimeError):
        validate_config_text("OOPZ_CONFIG = {")


def test_validate_text_accepts_good_syntax():
    validate_config_text(SAMPLE)


def test_field_specs_are_well_formed():
    """元数据自洽：tier 合法、高级项有分组、隐藏项只放自动维护的凭据字段。"""
    hidden = set()
    for group, fields in editor.FIELD_SPECS.items():
        assert group in editor.GROUP_SOURCES
        for field, meta in fields.items():
            tier = meta.get("tier", "adv")
            assert tier in {"basic", "adv", "hidden"}, (group, field)
            if tier == "adv":
                assert meta.get("section"), (group, field)
            if tier == "hidden":
                hidden.add(field)
            if meta.get("type") == "select":
                assert meta.get("options"), (group, field)
            if meta.get("vendor"):
                assert field in editor.FIELD_SPECS["voice"], (group, field)
    assert hidden == {"app_version", "device_id", "person_uid", "jwt_token"}
    for group in editor.FIELD_SPECS:
        assert editor.GROUP_META[group]["title"]
        assert editor.GROUP_META[group]["desc"]


def test_normalize_select_rejects_unknown_option():
    with pytest.raises(ValueError):
        editor._normalize_updates({"voice": {"backend": "not_a_vendor"}})


def test_normalize_select_accepts_preset_and_custom():
    assert editor._normalize_updates({"voice": {"backend": "gemini_live"}})["voice"]["backend"] == "gemini_live"
    # allow_custom 的字段可以填预设外的值
    normalized = editor._normalize_updates({"voice": {"gemini.voice": "MyCustomVoice"}})
    assert normalized["voice"]["gemini.voice"] == "MyCustomVoice"


def test_normalize_nested_sensitive_blank_dropped():
    normalized = editor._normalize_updates({"voice": {"gemini.api_key": None, "gemini.voice": "Kore"}})
    assert normalized == {"voice": {"gemini.voice": "Kore"}}


NESTED_SAMPLE = '''\
VOICE_AGENT_CONFIG = {
    "backend": "gemini_live",
    "proxy": "",
    "gemini": {
        "api_key": "",
        "model": "gemini-2.0-flash-live-001",
        "voice": "Puck",
    },
}
'''


def test_patched_text_updates_nested_value():
    patched = patch_config_text(NESTED_SAMPLE, {"voice": {"gemini.voice": "Kore"}}, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    namespace: dict[str, object] = {}
    exec(compile(patched, "<patched>", "exec"), namespace)
    voice = namespace["VOICE_AGENT_CONFIG"]
    assert isinstance(voice, dict)
    assert voice["gemini"]["voice"] == "Kore"
    assert voice["gemini"]["model"] == "gemini-2.0-flash-live-001"
    assert voice["backend"] == "gemini_live"


def test_patched_text_creates_missing_nested_dict():
    text = 'VOICE_AGENT_CONFIG = {\n    "backend": "gemini_live",\n}\n'
    patched = patch_config_text(
        text, {"voice": {"gemini.voice": "Aoede", "gemini.model": "gemini-2.0-flash-live-001"}}
    , sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    namespace: dict[str, object] = {}
    exec(compile(patched, "<patched>", "exec"), namespace)
    voice = namespace["VOICE_AGENT_CONFIG"]
    assert isinstance(voice, dict)
    assert voice["gemini"]["voice"] == "Aoede"
    assert voice["gemini"]["model"] == "gemini-2.0-flash-live-001"
    assert voice["backend"] == "gemini_live"


def test_patched_text_nested_and_flat_mixed():
    patched = patch_config_text(
        NESTED_SAMPLE, {"voice": {"proxy": "clash", "gemini.voice": "Fenrir"}}
    , sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    namespace: dict[str, object] = {}
    exec(compile(patched, "<patched>", "exec"), namespace)
    voice = namespace["VOICE_AGENT_CONFIG"]
    assert isinstance(voice, dict)
    assert voice["proxy"] == "clash"
    assert voice["gemini"]["voice"] == "Fenrir"


def test_patched_text_nested_preserves_comments():
    """嵌套字段就地改写时，字典内注释必须原样保留。"""
    text = (
        "VOICE_AGENT_CONFIG = {\n"
        "    \"backend\": \"gemini_live\",  # 总开关注释\n"
        "    \"gemini\": {\n"
        "        \"api_key\": \"\",                # 或 GEMINI_API_KEY\n"
        "        \"voice\": \"Puck\",\n"
        "    },\n"
        "}\n"
    )
    patched = patch_config_text(text, {"voice": {"gemini.voice": "Kore"}}, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    assert "# 总开关注释" in patched
    assert "# 或 GEMINI_API_KEY" in patched
    namespace: dict[str, object] = {}
    exec(compile(patched, "<patched>", "exec"), namespace)
    assert namespace["VOICE_AGENT_CONFIG"]["gemini"]["voice"] == "Kore"


def test_proxy_select_options_cover_clash_default():
    """模型页代理下拉必须包含 127.0.0.1:7890（clash 别名）入口。"""
    meta = editor.FIELD_SPECS["voice"]["proxy"]
    values = editor._select_values(meta)
    assert "clash" in values
    assert meta.get("allow_custom")


def test_model_vendor_fields_cover_url_key_model_voice():
    """模型配置页要能配 base_url / key / 模型名 / 音色。"""
    voice = editor.FIELD_SPECS["voice"]
    for field in (
        "gemini.base_url",
        "gemini.api_key",
        "gemini.model",
        "gemini.voice",
        "mimo.base_url",
        "mimo.api_key",
        "mimo.llm_model",
        "mimo.tts_voice",
        "openai.base_url",
        "openai.api_key",
        "openai.realtime_model",
        "openai.voice",
    ):
        assert field in voice, field
        assert voice[field].get("vendor") in {"gemini_live", "mimo_cascade", "openai_realtime"}
    # 音色与模型名支持预设下拉 + 自定义
    for field in ("gemini.voice", "mimo.tts_voice", "openai.voice", "gemini.model"):
        assert voice[field]["type"] == "select"
        assert voice[field].get("options")
        assert voice[field].get("allow_custom")


@pytest.mark.parametrize("source", [
    'OOPZ_CONFIG = {"proxy": "direct"}\n',
    'OOPZ_CONFIG = {"proxy": "direct",}\n',
    'OOPZ_CONFIG = {}\n',
    'OOPZ_CONFIG = {\n    "proxy": "direct"\n}\n',
    'OOPZ_CONFIG = {\n    "proxy": "direct"  # keep this comment\n}\n',
    'OOPZ_CONFIG = {"proxy": ("direct")}\n',
    'OOPZ_CONFIG = {\n    "proxy": ((\n        "direct"\n    ))  # keep this comment\n}\n',
    'OOPZ_CONFIG = {"proxy": (("direct")),}\n',
    'OOPZ_CONFIG = {\n    "proxy": ("direct")  # keep this comment, )\n}\n',
    'OOPZ_CONFIG = {"备注": "测试", "proxy": ("direct")}\n',
])
def test_missing_fields_preserve_valid_dictionary_syntax(source):
    patched = patch_config_text(source, {"oopz": {
        "default_area": "area", "default_channel": "channel",
    }}, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    namespace = {}
    exec(compile(patched, "<patched>", "exec"), namespace)
    assert namespace["OOPZ_CONFIG"]["default_area"] == "area"
    assert namespace["OOPZ_CONFIG"]["default_channel"] == "channel"
    if "proxy" in source:
        assert namespace["OOPZ_CONFIG"]["proxy"] == "direct"
    if "keep this comment" in source:
        assert "keep this comment" in patched


def test_missing_nested_field_preserves_existing_expression():
    source = ('DEFAULT = "Puck"\nVOICE_AGENT_CONFIG = {"gemini": {"voice": DEFAULT}}\n')
    patched = patch_config_text(source, {"voice": {"gemini.model": "model"}}, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    namespace = {}
    exec(compile(patched, "<patched>", "exec"), namespace)
    assert namespace["VOICE_AGENT_CONFIG"]["gemini"] == {"voice": "Puck", "model": "model"}
    assert '"voice": DEFAULT' in patched


def test_missing_nested_field_preserves_parenthesized_value_and_outer_fields():
    source = (
        'VOICE_AGENT_CONFIG = {\n'
        '    "gemini": {\n'
        '        "voice": (("Puck"))  # keep this comment, )\n'
        '    },\n'
        '    "proxy": "direct",\n'
        '}\n'
    )
    patched = patch_config_text(source, {"voice": {"gemini.model": "model"}}, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    namespace = {}
    exec(compile(patched, "<patched>", "exec"), namespace)
    assert namespace["VOICE_AGENT_CONFIG"] == {
        "gemini": {"voice": "Puck", "model": "model"}, "proxy": "direct",
    }
    assert '(("Puck"))' in patched
    assert "# keep this comment, )" in patched


def test_missing_nested_dict_preserves_expressions_and_comments():
    source = ('DEFAULT = "personality"\nVOICE_AGENT_CONFIG = {\n'
              '    "persona": DEFAULT,  # keep persona\n}\n')
    patched = patch_config_text(source, {"voice": {
        "gemini.voice": "Kore", "gemini.model": "model", "proxy": "direct",
    }}, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    namespace = {}
    exec(compile(patched, "<patched>", "exec"), namespace)
    assert namespace["VOICE_AGENT_CONFIG"] == {
        "persona": "personality", "proxy": "direct",
        "gemini": {"voice": "Kore", "model": "model"},
    }
    assert '"persona": DEFAULT,  # keep persona' in patched


def test_nested_expression_is_rejected_without_rewriting_unrelated_values():
    source = 'DEFAULT = {"voice": "Puck"}\nVOICE_AGENT_CONFIG = {"gemini": DEFAULT}\n'
    with pytest.raises(RuntimeError, match="gemini"):
        patch_config_text(source, {"voice": {"gemini.voice": "Kore"}}, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
