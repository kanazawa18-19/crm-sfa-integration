"""Notionユーザー（people型プロパティの1件）をメールアドレスへ解決する。

`src/api/user_directory.py`の`NotionUserDirectory`（`GET /v1/users`一覧をキャッシュしID→
名前解決する、ダッシュボード機能用）とは責務が異なるため変更しない。本モジュールは
`GET /v1/users/{user_id}`（単体取得）を直接呼び、email解決のみを行う軽量な関数を提供する。
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

_NOTION_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"


class NotionUserEmailLookupError(ApiError):
    """Notion `GET /v1/users/{user_id}` 呼び出し失敗時に送出する例外。"""


def get_notion_user_email(user_id: str, *, api_key: str | None = None) -> str | None:
    """Notionユーザー1件をメールアドレスへ解決する。

    `GET /v1/users/{user_id}`を呼び、`person.email`を返す。以下の場合は`None`を返す
    （例外にしない。呼び出し元は`None`の場合カレンダー同期をスキップする設計とする）:
    - ユーザーが見つからない（404）
    - `person`キーが無い（botユーザー等）
    - `person.email`が無い

    `api_key`省略時は`NOTION_API_KEY`環境変数を使う。
    """
    resolved_api_key = api_key if api_key is not None else os.environ.get("NOTION_API_KEY")
    if not resolved_api_key:
        raise ValueError(
            "NOTION_API_KEY environment variable (or api_key argument) is required but not set"
        )

    headers = {
        "Authorization": f"Bearer {resolved_api_key}",
        "Notion-Version": _NOTION_VERSION,
    }
    response = request_with_retry(
        "GET",
        f"{_BASE_URL}/users/{user_id}",
        headers=headers,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        max_retries=DEFAULT_MAX_RETRIES,
        backoff_base=DEFAULT_BACKOFF_BASE_SECONDS,
        idempotent=True,
    )
    if response.status_code == 404:
        return None
    raise_for_error(response, NotionUserEmailLookupError)

    user: dict[str, Any] = response.json()
    person = user.get("person")
    if not person:
        return None
    return person.get("email")
