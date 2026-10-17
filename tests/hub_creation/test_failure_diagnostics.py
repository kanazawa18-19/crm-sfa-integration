"""自由文を読まずに作成失敗を分類する。"""
import pytest
import requests

from src.hub_creation.failure_diagnostics import creation_failure_reason
from src.sync_engine.clients._http import ApiError
from src.sync_engine.clients.zoho_client import ZohoApiError


@pytest.mark.parametrize('error,category', [
    (TimeoutError('private secret'), '時間切れ'),
    (requests.exceptions.ReadTimeout('private secret'), '時間切れ'),
    (requests.exceptions.ConnectionError('private secret'), '接続エラー'),
    (requests.exceptions.RequestException('private secret'), '通信エラー'),
    (RuntimeError('private secret'), '不明なエラー'),
    (ApiError(403, 'private secret'), 'HTTP 403'),
])
def test_only_fixed_classification_is_recorded(error, category):
    reason = creation_failure_reason(error)
    assert category in reason
    assert 'private secret' not in reason
    assert '自動で再作成しません' in reason


@pytest.mark.parametrize('status', ['private secret', True, 600, 0])
def test_untrusted_status_and_error_code_are_omitted(status):
    error = ZohoApiError(status, 'private secret')
    error.creation_error_code = 'private secret'
    reason = creation_failure_reason(error)
    assert 'HTTP' not in reason
    assert 'private secret' not in reason
