"""実サービスと定期再開をつなぎ、確認待ちの無限再処理を防ぐ。"""
from src.hub_creation.worker import drain_creation_scans
from src.hub_creation.domain import CreationHeld
from src.hub_creation.duplicate_scan import PENDING
from tests.hub_creation.test_service import setup


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
