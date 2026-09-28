"""定期再開は照合待ちだけを扱い、結果不明の作成をやり直さない。"""
from types import SimpleNamespace
from src.hub_creation.worker import drain_creation_scans


class Journal:
    def __init__(self): self.deferred=[]; self.finished=[]
    def due(self,limit): return [{'sourceKey':'notion:p','sourceId':'p','dbKey':'chain'}]
    def defer(self,key): self.deferred.append(key)
    def done(self,key): self.finished.append(key)


def test_worker_reconstructs_notion_event_and_finishes_created_only():
    events=[]; journal=Journal()
    service=SimpleNamespace(handle=lambda event:events.append(event),
        journal=SimpleNamespace(get=lambda *a:{'state':'created'}))
    result=drain_creation_scans(service,journal=journal)
    assert result['completed']==1
    assert events[0].external_id=='p' and events[0].properties=={}
    assert journal.finished==['notion:p']


def test_unknown_post_finishes_scan_without_claiming_success():
    journal=Journal()
    service=SimpleNamespace(handle=lambda event:'hub_creation_held',
        journal=SimpleNamespace(get=lambda *a:{'state':'reserved'}))
    result=drain_creation_scans(service,journal=journal)
    assert result['completed']==0 and journal.finished==['notion:p']


def test_pending_scan_remains_available_for_next_run():
    journal=Journal()
    service=SimpleNamespace(handle=lambda event:'hub_creation_held',
        journal=SimpleNamespace(get=lambda *a:{'state':'blocked'}))
    result=drain_creation_scans(service,journal=journal)
    assert result['held']==1 and not journal.finished


def test_disabled_service_does_not_access_journal():
    assert drain_creation_scans(None,journal=object())=={'disabled':True}
