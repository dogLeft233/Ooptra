import json
from datetime import datetime, timedelta, timezone

import pytest

from behavior import create_visit_store as AutoVisitStateStore
from core import config_file_store

NOW = datetime(2026, 10, 6, 15, 59, tzinfo=timezone.utc)


def store_at(tmp_path):
    store = AutoVisitStateStore(tmp_path / 'state.json')
    store.load()
    return store


def test_success_counts_once_and_stops_at_three(tmp_path):
    store = store_at(tmp_path)
    for number in range(3):
        assert store.can_join('a', NOW, 3, 0)
        visit = str(number)
        store.reserve(visit, 'a', NOW)
        assert store.snapshot(NOW)['daily_count'] == number
        store.confirm(visit, 'a', NOW)
        store.confirm(visit, 'a', NOW)
    assert not store.can_join('b', NOW, 3, 0)
    assert store.snapshot(NOW)['area_counts'] == {'a': 3}


def test_pending_survives_restart_and_rollback_never_counts(tmp_path):
    store = store_at(tmp_path)
    store.reserve('pending', 'a', NOW)
    restored = AutoVisitStateStore(tmp_path / 'state.json')
    restored.load()
    assert restored.snapshot(NOW)['pending'] == {
        'pending': {'area': 'a', 'reserved_at': NOW.isoformat()}}
    assert restored.snapshot(NOW)['daily_count'] == 0
    assert not restored.can_join('b', NOW, 0, 0)
    restored.rollback('pending')
    restored.rollback('pending')
    assert restored.can_join('b', NOW, 0, 0)
    assert restored.snapshot(NOW)['daily_count'] == 0


def test_utc_sixteen_rolls_day_but_preserves_cooldowns_and_old_confirmations(tmp_path):
    store = store_at(tmp_path)
    store.reserve('v', 'a', NOW)
    store.confirm('v', 'a', NOW)
    until = NOW + timedelta(hours=3)
    store.set_global_cooldown(until)
    store.set_area_cooldown('a', until)
    next_day = NOW + timedelta(minutes=1)
    state = store.snapshot(next_day)
    assert state['day'] == '2026-10-07'
    assert state['daily_count'] == 0
    assert state['area_counts'] == {}
    store.confirm('v', 'a', next_day)
    restored = AutoVisitStateStore(tmp_path / 'state.json')
    restored.load()
    assert restored.snapshot(next_day) == state
    assert not restored.can_join('b', next_day, 3, 0)
    assert restored.can_join('a', until, 3, 0)


def test_day_clock_rollback_cannot_reset_quotas_repeatedly(tmp_path):
    store = store_at(tmp_path)
    tomorrow = NOW + timedelta(minutes=1)
    store.snapshot(tomorrow)
    store.reserve('v', 'a', tomorrow)
    store.confirm('v', 'a', tomorrow)
    assert not store.can_join('a', NOW, 3, 0)
    assert store.snapshot(NOW)['daily_count'] == 1
    assert store.snapshot(tomorrow)['daily_count'] == 1


def test_area_limit_and_manual_cooldown_are_isolated(tmp_path):
    store = store_at(tmp_path)
    store.reserve('v', 'a', NOW)
    store.confirm('v', 'a', NOW)
    assert not store.can_join('a', NOW, 0, 1)
    assert store.can_join('b', NOW, 0, 1)
    until = NOW + timedelta(hours=2)
    store.set_area_cooldown('b', until)
    assert not store.can_join('b', NOW, 0, 0)
    assert store.can_join('a', NOW, 0, 0)
    assert store.can_join('b', until, 0, 0)


@pytest.mark.parametrize('contents', ['{', '{}', '{"daily_count": -1}', '[]'])
def test_existing_broken_file_raises_without_overwriting(tmp_path, contents):
    path = tmp_path / 'state.json'
    path.write_text(contents, encoding='utf-8')
    store = AutoVisitStateStore(path)
    with pytest.raises(ValueError, match='状态'):
        store.load()
    assert path.read_text(encoding='utf-8') == contents


def test_failed_atomic_replace_preserves_file_and_memory(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    store.reserve('v', 'a', NOW)
    old = (tmp_path / 'state.json').read_bytes()
    old_replace = config_file_store.os.replace

    def fail_replacement(source, target):
        if str(source).endswith('.tmp'):
            raise OSError('disk failure')
        return old_replace(source, target)

    monkeypatch.setattr(config_file_store.os, 'replace', fail_replacement)
    with pytest.raises(OSError, match='状态'):
        store.confirm('v', 'a', NOW)
    assert (tmp_path / 'state.json').read_bytes() == old
    assert store.snapshot(NOW)['daily_count'] == 0
    assert 'v' in store.snapshot(NOW)['pending']


def test_confirm_requires_matching_reservation(tmp_path):
    store = store_at(tmp_path)
    with pytest.raises(ValueError):
        store.confirm('v', 'a', NOW)
    store.reserve('v', 'a', NOW)
    with pytest.raises(ValueError):
        store.confirm('v', 'b', NOW)
    assert store.snapshot(NOW)['daily_count'] == 0


def test_read_failure_is_not_a_missing_file(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    store.reserve('v', 'a', NOW)
    path_type = type(tmp_path)

    def unreadable(*args, **kwargs):
        raise PermissionError('denied')

    monkeypatch.setattr(path_type, 'read_text', unreadable)
    restored = AutoVisitStateStore(tmp_path / 'state.json')
    with pytest.raises(OSError, match='状态'):
        restored.load()


def test_persisted_state_corruption_rejects_invalid_counts(tmp_path):
    store = store_at(tmp_path)
    store.reserve('v', 'a', NOW)
    path = tmp_path / 'state.json'
    state = json.loads(path.read_text(encoding='utf-8'))
    state['daily_count'] = True
    path.write_text(json.dumps(state), encoding='utf-8')
    with pytest.raises(ValueError, match='状态'):
        AutoVisitStateStore(path).load()


def test_invalid_utf8_file_reports_state_error(tmp_path):
    path = tmp_path / 'state.json'
    path.write_bytes(b'\xff')
    with pytest.raises(ValueError, match='状态'):
        AutoVisitStateStore(path).load()


def test_missing_known_file_cannot_silently_reset_quota(tmp_path):
    store = store_at(tmp_path)
    store.reserve('v', 'a', NOW)
    store.confirm('v', 'a', NOW)
    (tmp_path / 'state.json').unlink()
    with pytest.raises(OSError, match='状态'):
        store.load()
