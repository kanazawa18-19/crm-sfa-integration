"""欠測を正常と誤認せず、古い観測中の滞留にも経過時間を加算する。"""
from copy import deepcopy
import pytest
from src.sync_capacity.health import assess
from src.sync_capacity.observation import observe_queue
from tests.sync_capacity.test_observation import queue


def check(value, **kwargs):
    return assess(value, now=1000, last_worker_success=990,
                  freshness_seconds=60, waiting_seconds=100, processing_seconds=50, **kwargs)


def normal():
    return observe_queue(queue([{'state': 'pending', 'created_at': 980},
                                {'state': 'processing', 'created_at': 800, 'updated_at': 970}]), clock=lambda: 990)


def test_processing_age_uses_claim_time_not_original_creation():
    report = normal()
    assert report['oldest_processing_age_seconds'] == 20
    assert check(report)['healthy'] is True


@pytest.mark.parametrize('bad', [None, True, float('nan'), 1001, 'x'])
def test_invalid_processing_age_is_not_healthy(bad):
    report = observe_queue(queue([{'state': 'processing', 'updated_at': bad}]), clock=lambda: 990)
    assert 'processing_age_unknown' in check(report)['alerts']


def test_staleness_and_long_processing_are_independent():
    report = normal()
    report['oldest_processing_age_seconds'] = 45
    assert 'processing_overdue' in check(report)['alerts']
    report['scan_finished_at'] = 900
    assert 'observation_stale_or_invalid' in check(report)['alerts']


@pytest.mark.parametrize('report', [None, {}, {'counts': 'bad'}, {'complete': False}])
def test_missing_observation_is_never_healthy(report):
    assert not check(deepcopy(report))['healthy']


def test_absent_worker_success_requires_attention():
    assert 'worker_success_missing_or_stale' in assess(normal(), now=1000, last_worker_success=None,
        freshness_seconds=60, waiting_seconds=100, processing_seconds=50)['alerts']


def test_claim_during_scan_is_valid_and_age_is_counted_only_once():
    times = iter([960, 980, 990])
    report = observe_queue(queue([{'state': 'processing', 'updated_at': 975}]), clock=lambda: next(times))
    assert report['oldest_processing_age_seconds'] == 15
    assert report['invalid_processing_timestamp_count'] == 0
    assert check(report)['healthy'] is True
    # 実経過25秒。走査開始からの40秒を二重加算すると誤通知になる。
    assert assess(report, now=1000, last_worker_success=990,
        freshness_seconds=60, waiting_seconds=100, processing_seconds=30)['healthy'] is True


def test_arrival_during_scan_and_clock_reversal_are_distinguished():
    times = iter([960, 980, 990])
    report = observe_queue(queue([{'state': 'pending', 'created_at': 975}]), clock=lambda: next(times))
    assert report['invalid_waiting_timestamp_count'] == 0
    assert check(report)['healthy'] is True
    times = iter([960, 990, 980])
    report = observe_queue(queue([{'state': 'processing', 'updated_at': 970}]), clock=lambda: next(times))
    assert 'age_reference_invalid' in check(report)['alerts']
