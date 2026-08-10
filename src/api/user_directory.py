"""Notion「people」型プロパティ（ユーザーID配列）を表示名に解決する。

Notion APIの`GET /v1/users`は`NOTION_API_KEY`のIntegrationに「ユーザー情報の読み取り」
権限が必要（詳細はdocs/dashboard_note.md参照）。
"""

from __future__ import annotations

import os
from typing import Any, Sequence

from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RATE_LIMIT_RETRIES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    raise_for_error,
    request_with_retry,
)

_NOTION_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"


class NotionUserDirectoryError(ApiError):
    """Notion `GET /v1/users` 呼び出し失敗時に送出する例外。"""


class NotionUserDirectory:
    """Notionワークスペースのユーザー一覧を`id -> name`にキャッシュして名前解決する。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = _BASE_URL,
        notion_version: str = _NOTION_VERSION,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_rate_limit_retries: int = DEFAULT_MAX_RATE_LIMIT_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("NOTION_API_KEY")
        if not self._api_key:
            raise ValueError(
                "NOTION_API_KEY environment variable (or api_key argument) is required but not set"
            )
        self._base_url = base_url.rstrip("/")
        self._notion_version = notion_version
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_rate_limit_retries = max_rate_limit_retries
        self._backoff_base = backoff_base
        self._names_by_id: dict[str, str] | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Notion-Version": self._notion_version,
        }

    def _load(self) -> dict[str, str]:
        if self._names_by_id is not None:
            return self._names_by_id

        names_by_id: dict[str, str] = {}
        start_cursor: str | None = None
        while True:
            params: dict[str, Any] = {}
            if start_cursor is not None:
                params["start_cursor"] = start_cursor
            response = request_with_retry(
                "GET",
                f"{self._base_url}/users",
                headers=self._headers(),
                params=params,
                timeout=self._timeout,
                max_retries=self._max_retries,
                max_rate_limit_retries=self._max_rate_limit_retries,
                backoff_base=self._backoff_base,
                idempotent=True,
            )
            raise_for_error(response, NotionUserDirectoryError)
            data = response.json()
            for user in data.get("results") or []:
                names_by_id[user["id"]] = user.get("name") or user["id"]
            if not data.get("has_more"):
                break
            start_cursor = data.get("next_cursor")

        self._names_by_id = names_by_id
        return names_by_id

    def resolve(self, user_id: str) -> str:
        """ユーザーIDを表示名へ解決する。見つからない場合はuser_idをそのまま返す。"""
        return self._load().get(user_id, user_id)

    def resolve_many(self, user_ids: Sequence[str]) -> list[str]:
        return [self.resolve(user_id) for user_id in user_ids]
