"""web-engagement-tool側のLead連携API（`POST /api/leads/sync`）を呼ぶクライアント。

このAPIはメールアドレスをキーとしたupsertエンドポイントであり、web-engagement-tool側で
本プロジェクト（crm-sfa-integration）から呼ばれることを前提に用意されている（本モジュールは
そのAPIを呼ぶだけの薄いHTTPクライアント）。`src/sync_engine/clients/_http.py`の
`request_with_retry`/`raise_for_error`/`ApiError`を再利用する（`src/calendar_sync`の
`WebEngagementToolCalendarClient`と同じHTTPクライアント基盤）。
"""

from __future__ import annotations

import os
from typing import Any

from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    raise_for_error,
    request_with_retry,
)


class LeadSyncApiError(ApiError):
    """web-engagement-tool側Lead連携API呼び出し失敗時に送出する例外。"""


class WebEngagementToolLeadSyncClient:
    """web-engagement-tool側`POST /api/leads/sync`を呼ぶクライアント。

    `base_url`（省略時`WEB_ENGAGEMENT_TOOL_URL`環境変数。`WebEngagementToolCalendarClient`と
    同じ環境変数を共有する。同一の連携先サービスであるため）・`api_token`（省略時
    `CRM_SFA_SYNC_API_TOKEN`環境変数）を必要とする。両方とも未設定の場合は`ValueError`を
    送出する（`WebEngagementToolCalendarClient`と同じパターン）。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._base_url = base_url if base_url is not None else os.environ.get(
            "WEB_ENGAGEMENT_TOOL_URL"
        )
        if not self._base_url:
            raise ValueError(
                "WEB_ENGAGEMENT_TOOL_URL environment variable (or base_url argument) "
                "is required but not set"
            )
        self._base_url = self._base_url.rstrip("/")

        self._api_token = api_token if api_token is not None else os.environ.get(
            "CRM_SFA_SYNC_API_TOKEN"
        )
        if not self._api_token:
            raise ValueError(
                "CRM_SFA_SYNC_API_TOKEN environment variable (or api_token argument) "
                "is required but not set"
            )

        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }

    def upsert_lead(
        self,
        *,
        email: str,
        company: str | None = None,
        last_name: str | None = None,
        first_name: str | None = None,
        phone: str | None = None,
        assigned_rep_email: str | None = None,
    ) -> dict[str, Any]:
        """`POST /api/leads/sync`を呼び、Leadを作成/更新（upsert）する。

        `email`のみ必須。`company`/`last_name`/`first_name`/`phone`/`assigned_rep_email`は
        `None`の場合はリクエストボディへ含めない（省略可能なフィールドのため）。

        メールアドレスによるupsert前提のAPI契約（同じ`email`で複数回呼んでも重複作成
        されない）のため、タイムアウト/5xx時の再送で問題ない
        （`WebEngagementToolCalendarClient.upsert_event`と同じ理由で`idempotent=True`）。
        """
        body: dict[str, Any] = {"email": email}
        if company is not None:
            body["company"] = company
        if last_name is not None:
            body["last_name"] = last_name
        if first_name is not None:
            body["first_name"] = first_name
        if phone is not None:
            body["phone"] = phone
        if assigned_rep_email is not None:
            body["assigned_rep_email"] = assigned_rep_email

        response = request_with_retry(
            "POST",
            f"{self._base_url}/api/leads/sync",
            headers=self._headers(),
            json_body=body,
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
            idempotent=True,
        )

        raise_for_error(response, LeadSyncApiError)
        return response.json()
