"""所属判定で読む原本ごとに、必要な関連欄だけを検証する。"""
from copy import deepcopy

import pytest

from src.contact_affiliation_job.gateway import AffiliationClient
from src.sync_engine.clients.notion_client import NotionRelationDataError


def client_with_page(monkeypatch, key, page):
    client = AffiliationClient(key, 'test-database', api_key='test-key')
    monkeypatch.setattr(client, 'get_raw_page', lambda _: deepcopy(page))
    return client


def test_master_without_contact_affiliation_property_can_be_read(monkeypatch):
    page = {'id': 'test-master', 'properties': {
        '取引先名': {'type': 'title', 'title': []},
    }}
    client = client_with_page(monkeypatch, 'client_master', page)
    assert client.get_raw_page_with_relations('test-master', {'連絡先'}) == page


def test_contact_missing_affiliation_still_fails_closed(monkeypatch):
    client = client_with_page(monkeypatch, 'contact', {'properties': {}})
    with pytest.raises(NotionRelationDataError):
        client.get_raw_page_with_relations('test-contact', set())


def test_contact_expands_affiliation_but_not_unrelated_activity(monkeypatch):
    company_id = '00000000-0000-0000-0000-000000000001'
    page = {'id': 'test-contact', 'properties': {
        '取引先マスター': {'id': 'company', 'type': 'relation', 'relation': [], 'has_more': True},
        '活動履歴': {'type': 'relation', 'relation': [], 'has_more': True},
    }}
    client = client_with_page(monkeypatch, 'contact', page)
    calls = []

    class Response:
        status_code = 200
        ok = True

        def json(self):
            return {'results': [{'type': 'relation', 'relation': {'id': company_id}}],
                    'has_more': False, 'next_cursor': None}

    def request(method, path, **kwargs):
        calls.append((method, path))
        return Response()

    monkeypatch.setattr(client, '_request', request)
    result = client.get_raw_page_with_relations('test-contact', {'取引先マスター', '活動履歴'})
    assert result['properties']['取引先マスター']['relation'] == [{'id': company_id}]
    assert result['properties']['取引先マスター']['has_more'] is False
    assert result['properties']['活動履歴']['has_more'] is True
    assert len(calls) == 1
    assert '/properties/company?' in calls[0][1]
