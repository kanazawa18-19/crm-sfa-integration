"""作成例外から固定分類とHTTP状態だけを記録する。本文・URLは読まない。"""
import requests

from src.sync_engine.clients._http import ApiError
from src.sync_engine.clients.zoho_client import CREATION_ERROR_CODES, ZohoApiError


def creation_failure_reason(error):
    category = "不明なエラー"
    details = []
    if isinstance(error, ApiError):
        category = "API応答エラー"
        status = error.status_code
        if type(status) is int and 100 <= status <= 599:
            details.append(f"HTTP {status}")
        if isinstance(error, ZohoApiError):
            code = getattr(error, "creation_error_code", None)
            if isinstance(code, str) and code in CREATION_ERROR_CODES:
                details.append(code)
    elif isinstance(error, (TimeoutError, requests.exceptions.Timeout)):
        category = "時間切れ"
    elif isinstance(error, requests.exceptions.ConnectionError):
        category = "接続エラー"
    elif isinstance(error, requests.exceptions.RequestException):
        category = "通信エラー"
    diagnostic = " / ".join([category, *details])
    return f"作成呼び出しの失敗（{diagnostic}）。作成結果の確認待ち。自動で再作成しません"
