"""表示タイトルが外部の氏名や既存補完へ混ざらないことを検証する。"""
from copy import deepcopy

import pytest

from src.db_schema.registry import get_schema
from src.sync_engine.contact_title import canonical_values, canonical_properties, NAME, CANONICAL_NAME, MANAGED
from src.sync_engine.clients.notion_client import HttpNotionClient, NotionApiError
from src.sync_engine.webhook_handlers.notion_webhook import _normalize_fetched_page, parse_notion_property_value

PAGE_ID = '00000000-0000-0000-0000-000000000001'
DB = get_schema('contact').notion_database_id


def page(managed=True, pure='検証 太郎'):
    return {'id': PAGE_ID, 'parent': {'database_id': DB},
            'last_edited_time': '2026-10-17T00:00:00Z', 'properties': {
        NAME: {'id': 'title', 'type': 'title', 'title': [{'plain_text': '検証 太郎（会社／test@example.invalid／電話）'}]},
        CANONICAL_NAME: {'id': 'pure', 'type': 'rich_text', 'rich_text': [{'plain_text': pure}] if pure else []},
        MANAGED: {'id': 'managed', 'type': 'checkbox', 'checkbox': managed},
        '携帯番号': {'id': 'phone', 'type': 'phone_number', 'phone_number': '090-0000-0000'},
    }}


@pytest.mark.parametrize('ids,expected', [
    (['title'], {}),
    (['pure'], {NAME: '検証 太郎'}),
    (['phone'], {'携帯番号': '090-0000-0000'}),
    (['title', 'phone'], {'携帯番号': '090-0000-0000'}),
    (['managed'], {NAME: '検証 太郎'}),
    (None, {NAME: '検証 太郎', '携帯番号': '090-0000-0000'}),
])
def test_webhook_only_transports_changed_canonical_fields(ids, expected):
    raw = page()
    original = deepcopy(raw)
    normalized = _normalize_fetched_page(raw, ids)
    assert {k: parse_notion_property_value(v) for k, v in normalized['properties'].items()} == expected
    assert raw == original


def test_cleared_canonical_name_is_not_restored_from_display_title():
    normalized = _normalize_fetched_page(page(pure=''), ['pure'])
    assert parse_notion_property_value(normalized['properties'][NAME]) is None
    assert canonical_values('contact', {MANAGED: True, CANONICAL_NAME: None, NAME: '装飾済み'})[NAME] is None


def test_old_contact_and_other_databases_are_unchanged():
    old = page(managed=False, pure='')
    assert canonical_properties('contact', old['properties']) == old['properties']
    assert canonical_properties('project', page()['properties']) == page()['properties']
    assert canonical_values('contact', {NAME: '元の氏名'}) == {NAME: '元の氏名'}
    normalized = _normalize_fetched_page(old, ['title'])
    assert normalized['properties'] == {NAME: old['properties'][NAME]}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr('src.sync_engine.clients.notion_client.record_notion_write', lambda **kw: None)
    monkeypatch.setattr(HttpNotionClient, '_resolve_for_audit', lambda self, values: values)
    return HttpNotionClient('contact', DB, api_key='test')


def test_get_page_is_canonical_but_raw_page_remains_raw(client, requests_mock):
    requests_mock.get(f'https://api.notion.com/v1/pages/{PAGE_ID}', json=page())
    assert client.get_page(PAGE_ID)[NAME] == '検証 太郎'
    assert client.get_raw_page(PAGE_ID)['properties'][NAME]['title'][0]['plain_text'].endswith('電話）')


@pytest.mark.parametrize('managed,field,kind', [(True, CANONICAL_NAME, 'rich_text'), (False, NAME, 'title')])
def test_inbound_name_uses_correct_storage(client, requests_mock, managed, field, kind):
    requests_mock.get(f'https://api.notion.com/v1/pages/{PAGE_ID}', json=page(managed, pure='検証 太郎' if managed else ''))
    patch = requests_mock.patch(f'https://api.notion.com/v1/pages/{PAGE_ID}', json={})
    client.update_page(PAGE_ID, {NAME: '検証 次郎'})
    assert patch.last_request.json() == {'properties': {field: {kind: [{'type': 'text', 'text': {'content': '検証 次郎'}}]}}}


def test_name_write_fails_closed_when_current_record_cannot_be_read(client, requests_mock):
    requests_mock.get(f'https://api.notion.com/v1/pages/{PAGE_ID}', status_code=404, json={})
    patch = requests_mock.patch(f'https://api.notion.com/v1/pages/{PAGE_ID}', json={})
    with pytest.raises(NotionApiError):
        client.update_page(PAGE_ID, {NAME: '検証 次郎'})
    assert not patch.called


@pytest.mark.parametrize('enabled', [False, True])
def test_only_enabled_new_contacts_are_opted_in(client, requests_mock, monkeypatch, enabled):
    monkeypatch.setenv('NOTION_CONTACT_TITLES_ENABLED', '1' if enabled else '0')
    post = requests_mock.post('https://api.notion.com/v1/pages', json={'id': PAGE_ID})
    client.create_page_once({NAME: '検証 太郎'})
    saved = post.last_request.json()['properties']
    assert (CANONICAL_NAME in saved) is enabled
    assert (MANAGED in saved) is enabled
    if enabled:
        assert saved[MANAGED] == {'checkbox': True}
        assert saved[CANONICAL_NAME]['rich_text'][0]['text']['content'] == '検証 太郎'


def test_name_lookup_supports_mixed_old_and_new_contacts(client, monkeypatch):
    monkeypatch.setenv('NOTION_CONTACT_TITLES_ENABLED', '1')
    assert client.title_match_filter(NAME, {'equals': '検証 太郎'}) == {'or': [
        {'property': NAME, 'title': {'equals': '検証 太郎'}},
        {'property': CANONICAL_NAME, 'rich_text': {'equals': '検証 太郎'}},
    ]}


def test_stopping_new_opt_in_keeps_existing_name_search(client, monkeypatch):
    monkeypatch.setenv('NOTION_CONTACT_TITLES_ENABLED', '0')
    monkeypatch.setenv('NOTION_CONTACT_TITLES_SCHEMA_READY', '1')
    assert CANONICAL_NAME == client.title_match_filter(NAME, {'equals': '検証 太郎'})['or'][1]['property']
    assert MANAGED not in client._new_page_properties({NAME: '検証 太郎'})


def test_new_opt_in_is_off_by_default(client, monkeypatch):
    monkeypatch.delenv('NOTION_CONTACT_TITLES_ENABLED', raising=False)
    assert MANAGED not in client._new_page_properties({NAME: '検証 太郎'})


def test_generic_client_reads_contact_canonical_name(requests_mock):
    client = HttpNotionClient('client_master', get_schema('client_master').notion_database_id, api_key='test')
    requests_mock.get(f'https://api.notion.com/v1/pages/{PAGE_ID}', json=page())
    assert client.get_page(PAGE_ID)[NAME] == '検証 太郎'


def test_external_creation_uses_canonical_name():
    from src.hub_creation.service import HubCreationService
    assert HubCreationService._creation_properties('contact', page())[NAME] == '検証 太郎'


def test_missing_canonical_field_fails_closed():
    with pytest.raises(ValueError):
        canonical_values('contact', {MANAGED: True, NAME: '装飾タイトル'})


@pytest.mark.parametrize('managed,pure', [(False, '検証 太郎')])
def test_flag_transition_cannot_send_decorated_or_empty_name(managed, pure):
    with pytest.raises(ValueError):
        _normalize_fetched_page(page(managed=managed, pure=pure), ['managed'])


def test_disabled_managed_record_fails_closed_on_later_reads():
    with pytest.raises(ValueError):
        canonical_values('contact', {MANAGED: False, CANONICAL_NAME: '検証 太郎', NAME: '装飾済み'})
    with pytest.raises(ValueError):
        canonical_properties('contact', page(managed=False)['properties'])


def test_compact_parent_id_still_uses_canonical_name():
    raw = page()
    raw['parent']['database_id'] = DB.replace('-', '')
    normalized = _normalize_fetched_page(raw, ['pure'])
    assert parse_notion_property_value(normalized['properties'][NAME]) == '検証 太郎'


def test_initial_empty_flag_does_not_clear_name():
    assert NAME not in _normalize_fetched_page(page(pure=''), ['managed'])['properties']
