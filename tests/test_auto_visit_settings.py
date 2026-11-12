from copy import deepcopy
from types import MappingProxyType

import pytest

from behavior import (
    effective_area,
    merge_auto_visit_patch,
    parse_auto_visit_config,
)


def test_default_rules_and_disabled_area():
    config = parse_auto_visit_config(None)
    area = effective_area(config, 'missing')
    assert config.check_interval_minutes == (10, 20)
    assert config.daily_limit == 3
    assert area == {
        'enabled': False, 'daily_limit': 0, 'join_probability': .2,
        'stay_minutes': (10, 20), 'auto_cooldown_minutes': (30, 60),
        'manual_cooldown_minutes': (120, 240),
        'enter_prompts': ['在玩什么游戏？'], 'leave_prompts': ['拜拜，我下了'],
    }


def test_sparse_patch_preserves_other_areas_and_empty_prompts():
    raw = {'areas': {'a': {'enabled': True, 'overrides': {'join_probability': .8}},
                     'b': {'enabled': True, 'overrides': {'enter_prompts': ['hi']}}}}
    original = deepcopy(raw)
    merged = merge_auto_visit_patch(raw, {'areas': {'a': {'overrides': {
        'join_probability': None, 'enter_prompts': []}}}})
    assert raw == original
    assert merged['areas']['b'] == raw['areas']['b']
    assert merged['areas']['a']['overrides'] == {'enter_prompts': []}
    area = effective_area(parse_auto_visit_config(merged), 'a')
    assert area['join_probability'] == .2
    assert area['enter_prompts'] == []
    assert area['enabled'] is True


def test_area_null_deletes_only_requested_area():
    assert merge_auto_visit_patch({'areas': {'a': {}, 'b': {}}}, {
        'areas': {'a': None}})['areas'] == {'b': {}}


def test_prompts_strip_empty_items_and_snapshot_has_no_input_aliases():
    raw = {'defaults': {'enter_prompts': ['  hello  ', '  ']},
           'areas': {'a': {'enabled': True}}}
    config = parse_auto_visit_config(raw)
    raw['defaults']['enter_prompts'][0] = 'changed'
    area = effective_area(config, 'a')
    assert area['enter_prompts'] == ['hello']
    area['enter_prompts'].append('changed')
    assert effective_area(config, 'a')['enter_prompts'] == ['hello']


@pytest.mark.parametrize('raw', [
    {'check_interval_minutes': [20, 10]},
    {'check_interval_minutes': [0, 20]},
    {'check_interval_minutes': [1, 10081]},
    {'daily_limit': True}, {'daily_limit': -1}, {'daily_limit': 1.5},
    {'defaults': {'join_probability': float('nan')}},
    {'defaults': {'join_probability': float('inf')}},
    {'defaults': {'join_probability': 1.1}},
    {'defaults': {'stay_minutes': [True, 10]}},
    {'defaults': {'enter_prompts': 'hi'}},
    {'defaults': {'enter_prompts': [1]}},
    {'defaults': {'enter_prompts': ['x' * 501]}},
    {'defaults': {'leave_prompts': ['x'] * 51}},
    {'unknown': 1}, {'areas': []}, {'areas': {'': {}}},
    {'areas': {'a': {'enabled': 'false'}}},
    {'areas': {'a': {'overrides': {'check_interval_minutes': [1, 2]}}}},
    {'areas': {'a': {'daily_limit': -1}}},
])
def test_invalid_config_is_rejected(raw):
    with pytest.raises(ValueError):
        parse_auto_visit_config(raw)


def test_patch_is_validated_before_return_and_does_not_mutate_input():
    raw = {'areas': {'a': {'enabled': False}}}
    with pytest.raises(ValueError):
        merge_auto_visit_patch(raw, {'areas': {'a': {'enabled': True}}, 'daily_limit': -1})
    assert raw == {'areas': {'a': {'enabled': False}}}


def test_sparse_merge_accepts_readonly_mappings():
    raw = MappingProxyType({'defaults': MappingProxyType({'join_probability': .5}),
                            'areas': MappingProxyType({'a': MappingProxyType({'enabled': True})})})
    result = merge_auto_visit_patch(raw, {'defaults': {'enter_prompts': []},
                                          'areas': {'a': {'daily_limit': 1}}})
    assert result['defaults'] == {'join_probability': .5, 'enter_prompts': []}
    assert result['areas']['a'] == {'enabled': True, 'daily_limit': 1}
