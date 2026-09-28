from types import SimpleNamespace
from unittest.mock import Mock
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from src.api.dependencies import wiring_dependency
from src.api.routes import sync_review as module
from src.sync_engine.id_mapping import SQLiteIdMappingStore, IdMapping
from src.sync_review.domain import ReviewForbidden


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv('DASHBOARD_API_TOKEN', 'test-only')
    store = SQLiteIdMappingStore(':memory:')
    store.upsert(IdMapping(notion_key='p', db_key='client_master'))
    service = Mock()
    dispatcher = SimpleNamespace(_store=store, _field_review=service)
    journal = Mock()
    journal.get.return_value = {'id': 'r', 'dbKey': 'client_master', 'notionKey': 'p',
                                'propertyName': '取引先名', 'state': 'pending', 'revision': 0}
    journal.decision.return_value = {**journal.get.return_value, 'state': 'confirmed', 'revision': 1}
    monkeypatch.setattr(module, 'FieldReviewJournal', lambda: journal)
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[wiring_dependency] = lambda: SimpleNamespace(dispatcher=SimpleNamespace(_dispatcher=dispatcher))
    return TestClient(app), journal, service


def test_review_token_cannot_use_development_auth_bypass(api, monkeypatch):
    client, journal, service = api
    monkeypatch.delenv('DASHBOARD_API_TOKEN')
    monkeypatch.setenv('ALLOW_UNAUTHENTICATED_DASHBOARD_API', 'true')
    response = client.post('/api/sync-review/decision', json={'id': 'r', 'actor_id': 'u', 'action': 'confirm', 'revision': 0})
    assert response.status_code == 401
    journal.decision.assert_not_called()


def test_non_manager_never_executes(api):
    client, journal, service = api
    journal.decision.side_effect = ReviewForbidden('マネージャーだけが処理できます')
    response = client.post('/api/sync-review/decision', headers={'Authorization': 'Bearer test-only'},
                           json={'id': 'r', 'actor_id': 'viewer', 'action': 'approve', 'revision': 0})
    assert response.status_code == 403
    service.execute.assert_not_called()


def test_first_confirmation_does_not_write(api):
    client, journal, service = api
    response = client.post('/api/sync-review/decision', headers={'Authorization': 'Bearer test-only'},
                           json={'id': 'r', 'actor_id': 'manager', 'action': 'confirm', 'revision': 0})
    assert response.status_code == 200
    assert response.json()['state'] == 'confirmed'
    service.execute.assert_not_called()
