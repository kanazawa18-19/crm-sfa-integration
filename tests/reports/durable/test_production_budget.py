"""日報入口の時間切れでDB処理を増やさず、残時間だけを収集へ渡す。"""
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src.infrastructure import http_budget as budget
from src.reports.durable import production


@pytest.fixture
def setup(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(budget.time, 'monotonic', lambda: now[0])
    journal = Mock()
    journal.next_job.return_value = {'phase': 'project'}
    factory = Mock(return_value=journal)
    monkeypatch.setattr(production, 'ReportJournal', factory)
    lock = Mock()

    @contextmanager
    def acquire(*args):
        lock()
        yield

    monkeypatch.setattr(production, 'acquire_record_sync_lock', acquire)
    monkeypatch.setattr(production, 'HttpNotionClient', Mock())
    monkeypatch.setattr(production, 'report_destination', Mock(return_value='synthetic'))
    runner = Mock()
    runner.run.return_value = {'state': 'collecting'}
    monkeypatch.setattr(production, 'ReportRunner', Mock(return_value=runner))

    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 26, 11, tzinfo=timezone.utc)

    monkeypatch.setattr(production, 'datetime', Clock)
    return now, journal, factory, lock, runner


@pytest.mark.parametrize('seconds', [-1, float('nan'), float('inf'), -float('inf')])
def test_invalid_budget_never_opens_database(setup, seconds):
    _, _, factory, lock, _ = setup
    with pytest.raises(ValueError):
        production.run_durable_report_batch(budget_seconds=seconds)
    factory.assert_not_called()
    lock.assert_not_called()


def test_zero_budget_never_opens_database(setup):
    _, _, factory, lock, _ = setup
    assert production.run_durable_report_batch(budget_seconds=0)['state'] == 'budget_exhausted'
    factory.assert_not_called()
    lock.assert_not_called()


def test_expired_parent_never_opens_database(setup):
    now, _, factory, lock, _ = setup
    with budget.http_budget(2):
        now[0] = 2
        assert production.run_durable_report_batch()['state'] == 'budget_exhausted'
    factory.assert_not_called()
    lock.assert_not_called()


@pytest.mark.parametrize('stage', ['lock', 'detect_missing_dates', 'ensure', 'next_job', 'clients'])
def test_expiry_after_each_preparation_stage_stops_following_work(setup, monkeypatch, stage):
    now, journal, _, lock, runner = setup

    def expire(*args, **kwargs):
        now[0] = 10
        return {'phase': 'project'}

    if stage == 'lock':
        lock.side_effect = expire
    elif stage == 'clients':
        monkeypatch.setattr(production, 'HttpNotionClient', expire)
    else:
        getattr(journal, stage).side_effect = expire
    result = production.run_durable_report_batch(budget_seconds=10)
    assert result['state'] == 'budget_exhausted'
    runner.run.assert_not_called()
    if stage == 'lock':
        journal.detect_missing_dates.assert_not_called()
    if stage in {'lock', 'detect_missing_dates'}:
        journal.ensure.assert_not_called()
    if stage in {'lock', 'detect_missing_dates', 'ensure'}:
        journal.next_job.assert_not_called()


@pytest.mark.parametrize('seconds,parent,expected', [(500, 500, 227), (80, 500, 77), (500, 50, 47)])
def test_runner_gets_remaining_time_after_database_work(setup, seconds, parent, expected):
    now, journal, _, _, runner = setup
    journal.next_job.side_effect = lambda: (now.__setitem__(0, 3) or {'phase': 'project'})
    with budget.http_budget(parent):
        production.run_durable_report_batch(start_today=False, budget_seconds=seconds)
        assert budget.remaining() == parent - 3
    journal.ensure.assert_not_called()
    assert runner.run.call_args.kwargs == {
        'collection_budget': min(120, expected), 'total_budget': expected}


def test_idle_and_busy_results_are_preserved(setup, monkeypatch):
    _, journal, _, _, runner = setup
    journal.next_job.return_value = None
    assert production.run_durable_report_batch()['state'] == 'idle'
    monkeypatch.setattr(production, 'acquire_record_sync_lock', Mock(side_effect=production.RecordSyncBusy()))
    assert production.run_durable_report_batch()['state'] == 'busy'
    runner.run.assert_not_called()
