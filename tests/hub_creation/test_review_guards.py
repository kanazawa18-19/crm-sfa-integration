"""外部作成直前の時間枠と、作成に関係する入力の変更を検証する。"""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from tests.hub_creation.test_service import setup


@pytest.mark.parametrize('seconds', [0, 39.9, 40, 40.1])
def test_budget_is_checked_after_last_read_before_reservation(monkeypatch, seconds):
    service, event, journal, notion, adapter, _ = setup()
    deferred = []
    adapter.duplicate_scan = SimpleNamespace(exists=lambda: True, defer=lambda: deferred.append(True))
    monkeypatch.setattr('src.infrastructure.http_budget.remaining', lambda: seconds)
    outcome = service.handle(event)
    if seconds <= 40:
        assert outcome == 'hub_creation_held'
        assert journal.claims == [] and adapter.creates == 0 and deferred == [True]
        monkeypatch.setattr('src.infrastructure.http_budget.remaining', lambda: 100)
        assert service.handle(event) == 'hub_creation_complete'
        assert adapter.creates == 1
    else:
        assert outcome == 'hub_creation_complete' and adapter.creates == 1


@pytest.mark.parametrize('change, permitted', [('unrelated', True), ('title', False), ('archived', False), ('parent', False)])
def test_last_read_checks_only_creation_inputs(change, permitted):
    service, event, journal, notion, adapter, _ = setup()
    adapter.duplicate_scan = SimpleNamespace(exists=lambda: True)
    first = notion.get_raw_page('page'); latest = deepcopy(first)
    if change == 'unrelated':
        latest['properties']['表示用数式'] = {'type': 'formula', 'formula': {'type': 'number', 'number': 1}}
        latest['properties']['グループ名']['id'] = 'display-metadata'
    elif change == 'title':
        latest['properties']['グループ名']['title'][0]['plain_text'] = '変更後'
    elif change == 'archived': latest['archived'] = True
    else: latest['parent'] = {'database_id': 'other'}
    pages = iter([first, latest]); notion.get_raw_page = lambda _: next(pages)
    assert service.handle(event) == ('hub_creation_complete' if permitted else 'hub_creation_held')
    assert adapter.creates == int(permitted)
    assert len(journal.claims) == int(permitted)
