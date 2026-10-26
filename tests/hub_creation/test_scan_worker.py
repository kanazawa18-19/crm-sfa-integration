"""実サービスと定期再開をつなぎ、確認待ちの無限再処理を防ぐ。"""
import math
from src.hub_creation.worker import drain_creation_scans
from src.hub_creation.domain import CreationHeld
from src.hub_creation.duplicate_scan import PENDING
from tests.hub_creation.test_service import setup
import pytest
from src.infrastructure.http_budget import http_budget, remaining


class Journal:
    def __init__(self):
        self.state = 'pending'
        self.deferred = []
        self.finished = []
    def due(self, limit):
        return [{'sourceKey': 'notion:page', 'sourceId': 'page', 'dbKey': 'chain'}] if self.state == 'pending' else []
    def defer(self, key): self.deferred.append(key)
    def done(self, key): self.finished.append(key); self.state = 'done'
    def hold(self, key, reason): self.state = 'held'


def test_worker_finishes_created_only():
    service, _, _, _, adapter, _ = setup()
    journal = Journal()
    result = drain_creation_scans(service, journal=journal)
    assert result['completed'] == 1 and adapter.creates == 1
    assert journal.finished == ['notion:page']


def test_unknown_post_finishes_scan_without_claiming_success():
    service, _, attempts, _, adapter, _ = setup(); adapter.fail = True
    journal = Journal()
    result = drain_creation_scans(service, journal=journal)
    assert result['completed'] == 0 and journal.finished == ['notion:page']
    drain_creation_scans(service, journal=journal)
    assert adapter.creates == 1 and attempts.get('notion:page', 'zoho')['state'] == 'reserved'


def test_pending_scan_remains_available_for_next_run():
    service, _, _, _, adapter, _ = setup()
    def pending(*args): raise CreationHeld(PENDING)
    adapter.plan = pending
    journal = Journal()
    assert drain_creation_scans(service, journal=journal)['held'] == 1
    assert journal.state == 'pending'


def test_other_holds_stop_repeated_notion_writes():
    for duplicate in (False, True):
        service, _, _, notion, adapter, _ = setup()
        notion.duplicate = duplicate; adapter.hold = True
        journal = Journal()
        drain_creation_scans(service, journal=journal)
        assert journal.state == 'held'
        writes = len(notion.notes)
        assert drain_creation_scans(service, journal=journal)['processed'] == 0
        assert len(notion.notes) == writes and adapter.creates == 0


def test_disabled_service_does_not_access_journal():
    assert drain_creation_scans(None, journal=object()) == {'disabled': True}


@pytest.mark.parametrize('seconds', [0, 29, -1, float('inf'), float('nan')])
def test_insufficient_or_invalid_budget_never_reads_journal(seconds):
    if seconds < 0 or not math.isfinite(seconds):
        with pytest.raises(ValueError):
            drain_creation_scans(object(), journal=object(), budget_seconds=seconds)
    else:
        assert drain_creation_scans(object(), journal=object(), budget_seconds=seconds) == {
            'processed': 0, 'completed': 0, 'held': 0}


@pytest.mark.parametrize('parent_seconds', [0, 20])
def test_short_parent_budget_prevents_queue_read(parent_seconds):
    with http_budget(parent_seconds):
        assert drain_creation_scans(object(), journal=object())['processed'] == 0
    assert remaining() is None


@pytest.mark.parametrize('seconds, expected', [(60, 60), (600, 240)])
def test_worker_passes_shorter_http_budget_and_restores_context(monkeypatch, seconds, expected):
    monkeypatch.setattr('src.infrastructure.http_budget.time.monotonic', lambda: 0)
    service, _, _, _, adapter, _ = setup()
    original = service.handle
    seen = []
    def handle(*args, **kwargs):
        seen.append(remaining())
        return original(*args, **kwargs)
    service.handle = handle
    assert drain_creation_scans(service, journal=Journal(), budget_seconds=seconds)['completed'] == 1
    assert seen == [expected] and adapter.creates == 1
    assert remaining() is None


def test_queue_read_uses_remaining_time_and_next_run_can_resume(monkeypatch):
    clock = [0]
    monkeypatch.setattr('src.infrastructure.http_budget.time.monotonic', lambda: clock[0])
    service, _, _, _, adapter, _ = setup()
    journal = Journal()
    original = journal.due
    def due(limit):
        clock[0] += 31
        return original(limit)
    journal.due = due
    assert drain_creation_scans(service, journal=journal, budget_seconds=60)['processed'] == 0
    assert journal.deferred == [] and adapter.creates == 0
    journal.due = original
    assert drain_creation_scans(service, journal=journal, budget_seconds=60)['completed'] == 1
    assert adapter.creates == 1
