from types import SimpleNamespace
from unittest.mock import Mock
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from src.api.dependencies import wiring_dependency
from src.api.routes import record_merge as module
from src.sync_review.domain import ReviewForbidden


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv('DASHBOARD_API_TOKEN', 'test-only')
    operations = Mock()
    operations.list_jobs.return_value = {'items': []}
    monkeypatch.setattr(module, 'MergeOperations', lambda *args: operations)
    app = FastAPI(); app.include_router(module.router)
    dispatcher = SimpleNamespace(_targets={}, _store=object())
    app.dependency_overrides[wiring_dependency] = lambda: SimpleNamespace(dispatcher=SimpleNamespace(_dispatcher=dispatcher))
    return TestClient(app), operations


@pytest.mark.parametrize('token', [None, 'Bearer wrong'])
def test_missing_or_wrong_token_never_reaches_operation(api, token):
    client, operations = api
    response = client.post('/api/record-merge', json={'actor_id': 'u', 'action': 'list'},
                           headers={'Authorization': token} if token else {})
    assert response.status_code == 401
    operations.list_jobs.assert_not_called()


def test_valid_token_still_requires_database_manager(api):
    client, operations = api
    operations.list_jobs.side_effect = ReviewForbidden('マネージャーだけが処理できます')
    response = client.post('/api/record-merge', json={'actor_id': 'viewer', 'action': 'list'},
                           headers={'Authorization': 'Bearer test-only'})
    assert response.status_code == 403


def test_compare_rejects_non_notion_id_before_api(api):
    client, operations = api
    response = client.post('/api/record-merge', json={'actor_id': 'manager', 'action': 'compare',
        'source_id': '../other', 'target_id': 'not-a-page'}, headers={'Authorization': 'Bearer test-only'})
    assert response.status_code == 409
    operations.compare.assert_not_called()
