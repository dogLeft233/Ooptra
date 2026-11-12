from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from webui import config_editor as editor

SOURCE = '''# user comment must remain
CUSTOM = "keep me"
VOICE_AGENT_CONFIG = {"enabled": False, "auto_join": True}  # old config remains inert
VOICE_AUTO_VISIT_CONFIG = {
    "check_interval_minutes": [10, 20],  # check comment remains
    "areas": {"a": {"enabled": True, "overrides": {"join_probability": 0.8}},
              "b": {"enabled": True, "overrides": {"enter_prompts": ["hello"]}}},
}
'''


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / 'config.py'
    path.write_text(SOURCE, encoding='utf-8')
    module = SimpleNamespace()
    monkeypatch.setattr(editor, 'CONFIG_PATH', str(path))
    monkeypatch.setattr(editor, '_runtime_config', lambda: module)
    return path, module


def read_namespace(path):
    namespace = {}
    exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), namespace)
    return namespace


def test_auto_visit_save_merges_domains_and_restores_inheritance(config_file):
    path, module = config_file
    result = editor.apply_auto_visit_updates({'areas': {'a': {'overrides': {
        'join_probability': None, 'enter_prompts': []}}}})
    raw = read_namespace(path)['VOICE_AUTO_VISIT_CONFIG']
    assert raw['areas']['a']['overrides'] == {'enter_prompts': []}
    assert raw['areas']['b']['overrides']['enter_prompts'] == ['hello']
    assert raw == module.VOICE_AUTO_VISIT_CONFIG
    assert result['restart_required'] is False
    assert '# user comment must remain' in path.read_text(encoding='utf-8')
    assert '# check comment remains' in path.read_text(encoding='utf-8')
    assert read_namespace(path)['CUSTOM'] == 'keep me'


def test_generic_updates_accept_auto_visit_mapping_without_string_coercion(config_file):
    path, _ = config_file
    editor.apply_updates({'auto_visit': {'defaults': {'enter_prompts': []}},
                          'voice': {'enabled': True}})
    raw = read_namespace(path)
    assert raw['VOICE_AUTO_VISIT_CONFIG']['defaults']['enter_prompts'] == []
    assert raw['VOICE_AGENT_CONFIG']['enabled'] is True


@pytest.mark.parametrize('patch', [
    {'daily_limit': -1}, {'areas': {'a': {'enabled': 'yes'}}},
    {'defaults': {'join_probability': float('nan')}},
    {'areas': {'a': {'overrides': {'unknown': None}}}},
    {'check_interval_minutes': [20, 10]}, {'unknown': {}},
])
def test_invalid_patch_never_changes_file_or_runtime(config_file, patch):
    path, module = config_file
    original = path.read_bytes()
    with pytest.raises(ValueError):
        editor.apply_updates({'voice': {'enabled': True}, 'auto_visit': patch})
    assert path.read_bytes() == original
    assert not vars(module)


def test_auto_join_is_rejected_in_schema_and_never_migrated(config_file):
    path, _ = config_file
    original = path.read_bytes()
    assert 'auto_join' not in editor.FIELD_SPECS['voice']
    with pytest.raises(ValueError):
        editor.apply_updates({'voice': {'auto_join': True}})
    assert path.read_bytes() == original


def test_missing_auto_visit_config_is_appended_without_enabling_areas(config_file):
    path, module = config_file
    path.write_text('CUSTOM = 42\nVOICE_AGENT_CONFIG = {"auto_join": True}\n', encoding='utf-8')
    editor.apply_auto_visit_updates({'daily_limit': 2})
    raw = read_namespace(path)
    assert raw['CUSTOM'] == 42
    assert raw['VOICE_AUTO_VISIT_CONFIG'] == {'daily_limit': 2}
    assert module.VOICE_AUTO_VISIT_CONFIG == {'daily_limit': 2}


def test_sync_runtime_is_serialized_with_sequential_sparse_saves(config_file, monkeypatch):
    path, module = config_file
    first_sync = Event()
    release_first = Event()
    second_sync = Event()
    calls = []

    def delayed_runtime():
        # The injected runtime port is reached under the same lock as the file
        # commit. A second save cannot overtake the first runtime synchronization.
        calls.append(True)
        if len(calls) == 1:
            first_sync.set()
            assert release_first.wait(3)
        else:
            second_sync.set()
        return module

    monkeypatch.setattr(editor, '_runtime_config', delayed_runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(editor.apply_auto_visit_updates, {'areas': {'a': {'daily_limit': 1}}})
        assert first_sync.wait(3)
        second = pool.submit(editor.apply_auto_visit_updates, {'areas': {'c': {'enabled': True}}})
        try:
            assert not second_sync.wait(.1)
        finally:
            release_first.set()
        first.result(timeout=3)
        second.result(timeout=3)
    raw = read_namespace(path)['VOICE_AUTO_VISIT_CONFIG']
    assert raw['areas']['a']['daily_limit'] == 1
    assert raw['areas']['b']['enabled'] is True
    assert raw['areas']['c']['enabled'] is True
    assert raw == module.VOICE_AUTO_VISIT_CONFIG


def test_failed_disk_write_never_syncs_runtime(config_file, monkeypatch):
    path, module = config_file
    original = path.read_bytes()

    def fail_write(*args):
        raise OSError('disk failed')

    import os
    monkeypatch.setattr(os, 'replace', fail_write)
    with pytest.raises(OSError):
        editor.apply_auto_visit_updates({'daily_limit': 2})
    assert path.read_bytes() == original
    assert not vars(module)


def test_example_has_canonical_defaults_and_disabled_areas():
    source = (Path(__file__).resolve().parents[1] / 'config.example.py').read_text(encoding='utf-8')
    namespace = {}
    exec(compile(source, '<example>', 'exec'), namespace)
    assert 'auto_join' not in namespace['VOICE_AGENT_CONFIG']
    assert namespace['VOICE_AUTO_VISIT_CONFIG'] == {
        'check_interval_minutes': [10, 20], 'daily_limit': 3,
        'defaults': {'join_probability': .2, 'stay_minutes': [10, 20],
                     'auto_cooldown_minutes': [30, 60], 'manual_cooldown_minutes': [120, 240],
                     'enter_prompts': ['在玩什么游戏？'], 'leave_prompts': ['拜拜，我下了']},
        'areas': {},
    }
