"""web-engagement-tool側のGoogle Calendar連携API（`POST /api/calendar/events`）を呼ぶクライアント。

営業担当者ごとのGoogle Calendar OAuth接続はweb-engagement-tool側で管理されており、本モジュールは
そのAPIを呼ぶだけの薄いHTTPクライアント。`src/sync_engine/clients/_http.py`の
`request_with_retry`/`raise_for_error`/`ApiError`を再利用する（既存の`HttpNotionClient`・
`NotionUserDirectory`と同じHTTPクライアント基盤）。
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


class CalendarSyncApiError(ApiError):
    """web-engagement-tool側カレンダー連携API呼び出し失敗時に送出する例外。"""


class WebEngagementToolCalendarClient:
    """web-engagement-tool側`POST /api/calendar/events`を呼ぶクライアント。

    `base_url`（省略時`WEB_ENGAGEMENT_TOOL_URL`環境変数）・`api_token`（省略時
    `CALENDAR_SYNC_API_TOKEN`環境変数）を必要とする。両方とも未設定の場合は`ValueError`を
    送出する（既存の`HttpNotionClient`が`NOTION_API_KEY`未設定時に送出するパターンに合わせる）。
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
            "CALENDAR_SYNC_API_TOKEN"
        )
        if not self._api_token:
            raise ValueError(
                "CALENDAR_SYNC_API_TOKEN environment variable (or api_token argument) "
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

    def upsert_event(
        self,
        *,
        rep_email: str,
        notion_project_id: str,
        summary: str,
        date: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        """`POST /api/calendar/events`を呼び、予定を作成/更新（upsert）する。

        対象の営業担当者がまだGoogle Calendar連携をしていない場合（422、
        `{"error": "rep_not_connected", ...}`）は例外を投げず、
        `{"skipped": "rep_not_connected", "rep_email": rep_email}`を返す（Webhook処理全体を
        失敗させるべきではないため）。401/400/5xx等その他のエラーは`CalendarSyncApiError`を
        送出する。
        """
        body: dict[str, Any] = {
            "rep_email": rep_email,
            "notion_project_id": notion_project_id,
            "summary": summary,
            "date": date,
        }
        if description is not None:
            body["description"] = description

        response = request_with_retry(
            "POST",
            f"{self._base_url}/api/calendar/events",
            headers=self._headers(),
            json_body=body,
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
            # upsert前提のAPI契約（同じnotion_project_idで複数回呼んでも重複作成されない）
            # のため、タイムアウト/5xx時の再送で問題ない。
            idempotent=True,
        )

        if response.status_code == 422:
            error_body = response.json() if response.content else {}
            if error_body.get("error") == "rep_not_connected":
                return {"skipped": "rep_not_connected", "rep_email": rep_email}

        raise_for_error(response, CalendarSyncApiError)
        return response.json()
