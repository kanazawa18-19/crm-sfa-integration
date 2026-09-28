import json
import pytest
from src.sync_engine.owner_mapping import owner_to_external, owner_from_external, UNRESOLVED_OWNER


@pytest.fixture
def directory(monkeypatch):
    data = {'verified': True, 'fallback_notion_id': 'u1', 'users': [
        {'notion_id': 'u1', 'zoho_id': '123', 'kintone_code': 'member1', 'enabled': True},
        {'notion_id': 'u2', 'zoho_id': '456', 'kintone_code': 'member2', 'enabled': True}]}
    monkeypatch.setenv('CRM_USER_MAPPING_JSON', json.dumps(data))
    return data


def test_verified_owner_roundtrip_and_required_fallback(directory):
    assert owner_to_external(['u1'], 'zoho') == {'id':'123'}
    assert owner_from_external({'id':'123'}, 'zoho') == ['u1']
    assert owner_to_external(['unknown'], 'zoho') is None
    assert owner_to_external(['unknown'], 'kintone', required=True) == [{'code':'member1'}]
    assert owner_from_external([{'code':'member2'}], 'kintone') == ['u2']


def test_multiple_owners_are_not_reduced(directory):
    assert owner_to_external(['u1','u2'], 'zoho') is UNRESOLVED_OWNER
    assert owner_to_external(['u1','u2'], 'kintone') == [{'code':'member1'},{'code':'member2'}]
    assert owner_to_external(['u1','missing'], 'kintone') is UNRESOLVED_OWNER


def test_missing_or_inactive_directory_cannot_write(directory, monkeypatch):
    monkeypatch.delenv('CRM_USER_MAPPING_JSON')
    assert owner_to_external(['u1'], 'zoho') is UNRESOLVED_OWNER
    directory['users'][0]['enabled'] = False
    monkeypatch.setenv('CRM_USER_MAPPING_JSON', json.dumps(directory))
    assert owner_to_external(['u1'], 'zoho') is UNRESOLVED_OWNER


def test_inbound_owner_does_not_reduce_multiple_or_replace_unmapped_with_fallback(directory):
    from src.sync_engine.owner_mapping import hold_owner_inbound
    assert hold_owner_inbound(['u1', 'u2'], ['u1'], 'zoho')
    assert hold_owner_inbound(['unknown'], ['u1'], 'kintone')
    assert not hold_owner_inbound(['u2'], ['u1'], 'kintone')
    assert not hold_owner_inbound(['u1'], ['u2'], 'zoho')
    assert hold_owner_inbound(None, ['u1'], 'zoho')


def test_empty_notion_owner_does_not_accept_required_fallback_echo(directory):
    from src.sync_engine.owner_mapping import hold_owner_inbound
    assert hold_owner_inbound([], ['u1'], 'kintone')
    assert hold_owner_inbound([], ['u1'], 'zoho')


@pytest.mark.parametrize('layout_required', [False, True])
def test_zoho_required_uses_field_and_layout_metadata(layout_required):
    from types import SimpleNamespace
    from src.sync_engine.owner_mapping import zoho_owner_required
    class Client:
        def _request(self, method, path):
            data = {'fields': [{'api_name': 'Owner', 'system_mandatory': False}]} if '/fields?' in path else {
                'layouts': [{'sections': [{'fields': [{'api_name': 'Owner', 'required': layout_required}]}]}]}
            return SimpleNamespace(ok=True, status_code=200, json=lambda: data)
    assert zoho_owner_required(Client(), 'Deals') is layout_required
