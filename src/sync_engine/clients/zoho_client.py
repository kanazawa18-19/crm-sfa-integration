"""Zoho CRM API (`https://www.zohoapis.com/crm/v2/`) へ実HTTP通信を行う `ZohoClient` Protocol実装。

`src/sync_engine/sync_targets/zoho_sync.py` の `ZohoClient` Protocolを満たす。
認証はOAuth2アクセストークン。`ZOHO_CLIENT_ID`/`ZOHO_CLIENT_SECRET`/`ZOHO_REFRESH_TOKEN`から
アクセストークンをリフレッシュ（`https://accounts.zoho.com/oauth/v2/token`）し、
有効期限内はメモリ内にキャッシュして毎回リフレッシュしないようにする。

Zoho APIのレスポンスは`{"data": [...]}`形式でラップされているため、本モジュールで
1件目（`data[0]`）の取り出しを吸収する。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    raise_for_error,
    request_with_retry,
)

_ACCOUNTS_BASE_URL = "https://accounts.zoho.com"
_API_BASE_URL = "https://www.zohoapis.com/crm/v2"
# アクセストークンの実際の有効期限より早めに失効扱いにし、期限ぎりぎりでの401を避ける。
_EXPIRY_SAFETY_MARGIN_SECONDS = 60


class ZohoApiError(ApiError):
    """Zoho CRM API呼び出し失敗時に送出する例外。"""


class HttpZohoClient:
    """Zoho CRM API (GET/POST/PUT) を用いた `ZohoClient` Protocol実装。

    アクセストークンはメモリ内でキャッシュし、有効期限が切れるまで再利用する
    （呼び出しのたびにリフレッシュしない）。

    Notion向け`build_notion_property_value`と異なり、本モジュールはZohoのフィールド種別
    （例: 複数選択・関連リストで単一値をリストへ自動正規化する等）に応じた値の整形は行わない。
    Zohoのモジュール・フィールドごとに期待される値の形式（単一値かリストか、要素の形状）が
    異なるため、呼び出し元が対象フィールドの型に応じた正しい形式で値を渡す責任を持つ。
    """

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        accounts_base_url: str = _ACCOUNTS_BASE_URL,
        api_base_url: str = _API_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._client_id = client_id if client_id is not None else os.environ.get("ZOHO_CLIENT_ID")
        self._client_secret = (
            client_secret if client_secret is not None else os.environ.get("ZOHO_CLIENT_SECRET")
        )
        self._refresh_token = (
            refresh_token if refresh_token is not None else os.environ.get("ZOHO_REFRESH_TOKEN")
        )
        if not self._client_id:
            raise ValueError(
                "ZOHO_CLIENT_ID environment variable (or client_id argument) is required but not set"
            )
        if not self._client_secret:
            raise ValueError(
                "ZOHO_CLIENT_SECRET environment variable (or client_secret argument) "
                "is required but not set"
            )
        if not self._refresh_token:
            raise ValueError(
                "ZOHO_REFRESH_TOKEN environment variable (or refresh_token argument) "
                "is required but not set"
            )
        self._accounts_base_url = accounts_base_url.rstrip("/")
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

        self._access_token: str | None = None
        self._access_token_expires_at: datetime | None = None
        # 並行アクセス時に複数スレッドが同時に期限切れと判定し、無駄なリフレッシュが
        # 重複発火することを防ぐ。
        self._token_lock = threading.Lock()

    def _get_access_token(self) -> str:
        now = datetime.now(timezone.utc)
        if (
            self._access_token is not None
            and self._access_token_expires_at is not None
            and now < self._access_token_expires_at
        ):
            return self._access_token
        with self._token_lock:
            # ロック取得待ちの間に他スレッドがリフレッシュ済みの可能性があるため再確認する。
            now = datetime.now(timezone.utc)
            if (
                self._access_token is not None
                and self._access_token_expires_at is not None
                and now < self._access_token_expires_at
            ):
                return self._access_token
            return self._refresh_access_token(now)

    def _refresh_access_token(self, now: datetime) -> str:
        response = request_with_retry(
            "POST",
            f"{self._accounts_base_url}/oauth/v2/token",
            params={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            },
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
        )
        raise_for_error(response, ZohoApiError)
        body = response.json()
        access_token = body["access_token"]
        expires_in = int(body.get("expires_in", 3600))
        self._access_token = access_token
        self._access_token_expires_at = now + timedelta(
            seconds=max(expires_in - _EXPIRY_SAFETY_MARGIN_SECONDS, 0)
        )
        return access_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Zoho-oauthtoken {self._get_access_token()}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        idempotent: bool = True,
    ) -> requests.Response:
        return request_with_retry(
            method,
            f"{self._api_base_url}{path}",
            headers=self._headers(),
            json_body=json_body,
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
            idempotent=idempotent,
        )

    def get_record(self, module: str, record_id: str) -> dict[str, Any] | None:
        response = self._request("GET", f"/{module}/{record_id}")
        if response.status_code in (404, 204):
            return None
        raise_for_error(response, ZohoApiError)
        data = response.json().get("data") or []
        return data[0] if data else None

    def insert_record(self, module: str, record: dict[str, Any]) -> str:
        # 作成系（非冪等）操作のため、タイムアウト/5xx時の重複レコード作成を避けリトライしない。
        response = self._request(
            "POST", f"/{module}", json_body={"data": [record]}, idempotent=False
        )
        raise_for_error(response, ZohoApiError)
        result = response.json()["data"][0]
        if result.get("code") != "SUCCESS":
            raise ZohoApiError(response.status_code, str(result.get("message", result)))
        return str(result["details"]["id"])

    def update_record(self, module: str, record_id: str, record: dict[str, Any]) -> None:
        body = {"data": [{**record, "id": record_id}]}
        response = self._request("PUT", f"/{module}/{record_id}", json_body=body)
        raise_for_error(response, ZohoApiError)
        result = response.json()["data"][0]
        if result.get("code") != "SUCCESS":
            raise ZohoApiError(response.status_code, str(result.get("message", result)))
