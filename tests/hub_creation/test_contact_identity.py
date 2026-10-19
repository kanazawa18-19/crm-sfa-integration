"""同名だけで止めず、欠損・メール一致・所属一致は引き続き保留する。"""
import pytest
from src.hub_creation.contact_identity import distinct_contacts


@pytest.mark.parametrize('email,companies,expected', [
    ('other@example.test', ['company-b'], True),
    (' PERSON@EXAMPLE.TEST ', ['company-b'], False),
    ('other@example.test', ['company-a'], False),
    ('other@example.test', ['company-b', 'company-a'], False),
    ('', ['company-b'], False),
    ('other@example.test', [], False),
    ('other@example.test', None, False),
])
def test_別管理は両根拠が異なる場合だけ(email, companies, expected):
    assert distinct_contacts(
        {'メールアドレス': 'person@example.test', '取引先マスター': ['company-a']},
        {'メールアドレス': email, '取引先マスター': companies},
    ) is expected


def test_Zoho同名でも別メール別所属なら別管理():
    from src.hub_creation.duplicate_scan import contact_matcher
    matches = contact_matcher({'姓': '試験', '名': '', 'メールアドレス': 'a@example.test', '_zoho_company': {'id': '100'}})
    row = {'Last_Name': '試験', 'First_Name': '', 'Email': 'b@example.test', 'field25': {'id': '200'}}
    assert not matches(row)
    assert matches({**row, 'field25': None})
    assert matches({**row, 'Email': ' A@EXAMPLE.TEST '})


def test_旧親の対象先限定復旧と再実行():
    from tests.hub_creation.test_service import setup
    from src.record_merge.domain import digest
    service, event, journal, notion, adapter, store = setup()
    notion.created = '2026-09-01T00:00:00Z'
    plan = {'db_key': event.db_key, 'id': event.external_id, 'target': 'zoho',
            'hash': digest(service._creation_properties(event.db_key, notion.get_raw_page('page')))}
    assert service.handle(event) is None
    assert service.handle(event, recovery=plan) == 'hub_creation_complete'
    service.handle(event, recovery=plan)
    assert adapter.creates == 1


def test_復旧原本変更は作成しない():
    from tests.hub_creation.test_service import setup
    service, event, journal, notion, adapter, store = setup()
    plan = {'db_key': event.db_key, 'id': event.external_id, 'target': 'zoho', 'hash': 'old'}
    assert service.handle(event, recovery=plan) == 'hub_creation_held'
    assert adapter.creates == 0


@pytest.mark.parametrize('changed', [False, True])
def test_シート由来原本の限定復旧はシート状態を上書きしない(changed):
    from tests.hub_creation.test_service import setup
    from src.record_merge.domain import digest
    from unittest.mock import Mock
    service, event, journal, notion, adapter, store = setup()
    journal.rows['sheet:registration', 'notion'] = {'state': 'created', 'externalId': 'page'}
    service.sheet_gateway = Mock()
    plan = {'db_key': event.db_key, 'id': event.external_id, 'target': 'zoho',
            'hash': 'old' if changed else digest(service._creation_properties(event.db_key, notion.get_raw_page('page')))}
    service.handle(event, recovery=plan)
    service.sheet_gateway.update.assert_not_called()
    assert adapter.creates == (0 if changed else 1)


def test_復旧は指定外の送り先を処理しない():
    from tests.hub_creation.test_service import setup, Adapter
    from src.record_merge.domain import digest
    service, event, journal, notion, adapter, store = setup()
    other = Adapter(); other.target = 'kintone'
    service.adapters.append(other)
    plan = {'db_key': event.db_key, 'id': event.external_id, 'target': 'zoho',
            'hash': digest(service._creation_properties(event.db_key, notion.get_raw_page('page')))}
    assert service.handle(event, recovery=plan) == 'hub_creation_complete'
    assert adapter.creates == 1 and other.creates == 0
    assert ('notion:page', 'kintone') not in journal.rows
    assert store.get('page').kintone_id is None


def test_所属の未取得部分があると別管理を判定しない():
    from src.hub_creation.service import HubCreationService
    from src.hub_creation.domain import CreationHeld
    with pytest.raises(CreationHeld):
        HubCreationService._creation_properties('contact', {'properties': {'取引先マスター': {'has_more': True}}})


def test_旧Zoho所属は正本と同じとして保留():
    from src.hub_creation.duplicate_scan import contact_matcher
    matches = contact_matcher({'姓': '試験', '名': '', 'メールアドレス': 'a@example.test',
                              '_zoho_company': {'id': 'new'}, '_zoho_company_aliases': {'old': 'new'}})
    assert matches({'Last_Name': '試験', 'First_Name': '', 'Email': 'b@example.test', 'field25': {'id': 'old'}})


def test_照合条件の所属変更はしおりを再利用しない():
    from src.hub_creation.duplicate_scan import scan_input_hash
    context = {'dbKey': 'contact', 'sourceProperties': {'名前': '試験'}, 'contactComparison': {'version': 2, 'company': 'old'}}
    assert scan_input_hash(context) != scan_input_hash({**context, 'contactComparison': {'version': 2, 'company': 'new'}})


def test_統合中の原本は復旧しない(monkeypatch):
    from tests.hub_creation.test_service import setup
    from src.record_merge.domain import digest, MergeHeld
    service, event, journal, notion, adapter, store = setup()
    plan = {'db_key': event.db_key, 'id': event.external_id, 'target': 'zoho',
            'hash': digest(service._creation_properties(event.db_key, notion.get_raw_page('page')))}
    def blocked(*args):
        raise MergeHeld('統合中')
    monkeypatch.setattr('src.record_merge.aliases.require_record_available', blocked)
    with pytest.raises(MergeHeld):
        service.handle(event, recovery=plan)
    assert adapter.creates == 0
