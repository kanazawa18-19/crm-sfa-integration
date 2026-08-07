"""Google API（Drive/Sheets/Docs）向けアクセストークンの解決。

サービスアカウント（`GOOGLE_SERVICE_ACCOUNT_JSON`環境変数、JSON文字列そのもの）を最優先で
使う。サービスアカウントの鍵は失効せず自動更新できるため、本番運用に適する
（対して`GOOGLE_ACCESS_TOKEN`は約1時間で失効するOAuthアクセストークンで、ローカル動作確認
専用）。どちらも未設定の場合はエラーにする（fail-closed）。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import google.auth.transport.requests
from google.oauth2 import service_account

_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]

# credentials.valid（google-auth内部でexpiry - 3分45秒を閾値に判定）より先に効くことは
# ほぼ無いが、念のための保険として残す（shirokuma-secレビュー: 実質的な決め手にはならない
# 点を明記）。
_EXPIRY_MARGIN_SECONDS = 60

_cached_credentials: service_account.Credentials | None = None
# 複数リクエストが同時にトークン取得・更新へ入ると、無駄なリフレッシュAPI呼び出しの重複や
# credentials.token/expiryの設定順序が乱れる恐れがあるため、zoho_client.pyのトークン
# キャッシュと同じdouble-checked lockingパターンで排他制御する（shirokuma-secレビュー指摘）。
_lock = threading.Lock()


def _load_service_account_credentials() -> service_account.Credentials | None:
    global _cached_credentials
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        return None
    if _cached_credentials is None:
        info = json.loads(raw_json)
        _cached_credentials = service_account.Credentials.from_service_account_info(
            info, scopes=_SCOPES
        )
    return _cached_credentials


def get_google_access_token() -> str:
    """有効なGoogle APIアクセストークンを返す（必要に応じて取得・更新する）。"""
    credentials = _load_service_account_credentials()
    if credentials is not None:
        if credentials.valid and not _expires_soon(credentials):
            assert credentials.token is not None
            return credentials.token
        with _lock:
            # ロック取得待ちの間に他スレッドが更新済みの可能性があるため再確認する。
            if not credentials.valid or _expires_soon(credentials):
                credentials.refresh(google.auth.transport.requests.Request())
        if credentials.token is None:
            raise RuntimeError("failed to obtain an access token from the service account credentials")
        return credentials.token

    manual_token = os.environ.get("GOOGLE_ACCESS_TOKEN")
    if manual_token:
        return manual_token

    raise ValueError(
        "GOOGLE_SERVICE_ACCOUNT_JSON または GOOGLE_ACCESS_TOKEN environment variable "
        "is required but not set"
    )


def _expires_soon(credentials: service_account.Credentials) -> bool:
    if credentials.expiry is None:
        return False
    remaining = (credentials.expiry - _utcnow()).total_seconds()
    return remaining < _EXPIRY_MARGIN_SECONDS


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def reset_cache() -> None:
    """モジュールレベルキャッシュを明示的にクリアする（テスト用）。"""
    global _cached_credentials
    _cached_credentials = None
