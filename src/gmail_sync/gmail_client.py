"""Gmail REST APIを直接叩く最小クライアント(2026-08-16)。

google-api-python-clientは使わず、既存のrequests + `_http.request_with_retry`基盤
(calendar_sync/web_engagement_tool_client.py等と同じ)でREST呼び出しのみ実装する
(依存を増やさない方針)。OAuthリフレッシュトークンからアクセストークンを取得する処理も
自前で行う(google-authライブラリのCredentials/Requestは使わず、Googleのtoken
エンドポイントを直接叩く — こちらの方が依存が少なく、リトライ・タイムアウト設定も
既存の共有ヘルパーに統一できる)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    extract_error_message,
    raise_for_error,
    request_with_retry,
)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_MAX_MESSAGES_PER_SYNC = 100
# cron実行間隔(1日1回想定)より少し広めに取り、実行遅延・タイムゾーン差で取りこぼさない
# ようにする(重複はgmailMessageIdでdb.email_log_exists()により弾かれるため安全)。
_SEARCH_WINDOW_DAYS = 2


class GmailApiError(ApiError):
    """Gmail API呼び出し失敗時に送出する例外。"""


def _client_credentials() -> tuple[str, str]:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET is not set")
    return client_id, client_secret


def refresh_access_token(refresh_token: str) -> str:
    """リフレッシュトークンから新しいアクセストークンを取得する。"""
    client_id, client_secret = _client_credentials()
    response = request_with_retry(
        "POST",
        _TOKEN_URL,
        json_body={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=DEFAULT_TIMEOUT_SECONDS,
        max_retries=DEFAULT_MAX_RETRIES,
    )
    raise_for_error(response, GmailApiError)
    access_token = response.json().get("access_token")
    if not access_token:
        raise GmailApiError(response.status_code, "no access_token in refresh response")
    return access_token


@dataclass(frozen=True)
class GmailMessageRef:
    id: str


def list_recent_messages(access_token: str) -> list[GmailMessageRef]:
    """直近`_SEARCH_WINDOW_DAYS`日分の送受信メール一覧(最大`_MAX_MESSAGES_PER_SYNC`件)を返す。

    特定の連絡先を先に知っている必要はない(Zoho CRM方式=メアド一致による自動関連付けの
    ため、まず「最近のメール全部」を取得し、各メッセージの送信者/宛先をsync.py側で
    連絡先DBと突き合わせる設計)。
    """
    response = request_with_retry(
        "GET",
        f"{_GMAIL_API_BASE}/messages",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"q": f"newer_than:{_SEARCH_WINDOW_DAYS}d", "maxResults": _MAX_MESSAGES_PER_SYNC},
    )
    raise_for_error(response, GmailApiError)
    return [GmailMessageRef(id=m["id"]) for m in response.json().get("messages", [])]


@dataclass(frozen=True)
class GmailMessage:
    id: str
    from_header: str
    to_header: str
    subject: str | None
    date_header: str | None
    snippet: str | None


def _header_value(headers: list[dict[str, Any]], name: str) -> str | None:
    for h in headers:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value")
    return None


def get_profile(access_token: str) -> dict[str, Any]:
    """`GET /users/me/profile`でmailbox全体の現在の`historyId`等を取得する(2026-08-16)。

    `sync.sync_rep_incremental()`が、フル同期実施後・`list_history()`処理後に、次回以降の
    増分取得の起点として保存する`historyId`を得るために使う(`list_history()`のレスポンス
    自体には最新の`historyId`が含まれないため)。
    """
    response = request_with_retry(
        "GET",
        f"{_GMAIL_API_BASE}/profile",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    raise_for_error(response, GmailApiError)
    return response.json()


def watch_mailbox(access_token: str, topic_name: str) -> dict[str, Any]:
    """`POST /users/me/watch`でPush通知(Cloud Pub/Sub)を登録・更新する(2026-08-16)。

    レスポンスの`historyId`/`expiration`(epoch ms文字列)をそのまま返す
    (`watch_registration.register_or_renew_watch()`が`db.update_watch_state()`へ渡す)。
    同じmailboxに対して何度呼んでも安全(Google側が既存のwatchを上書きする)。
    """
    response = request_with_retry(
        "POST",
        f"{_GMAIL_API_BASE}/watch",
        headers={"Authorization": f"Bearer {access_token}"},
        json_body={"topicName": topic_name, "labelIds": ["INBOX"]},
    )
    raise_for_error(response, GmailApiError)
    return response.json()


class HistoryIdExpiredError(GmailApiError):
    """`startHistoryId`が古すぎてGmail側が404を返した場合に送出する(フル同期へのフォールバック
    のトリガー)。Gmailのhistoryレコードは一定期間で失効するため、Push未受信期間が長い等の
    理由で発生しうる(呼び出し元はsync.sync_rep()による通常のフル同期にフォールバックすること)。
    """


def list_history(access_token: str, start_history_id: str) -> list[str]:
    """`GET /users/me/history`で`start_history_id`以降に追加されたメッセージIDの一覧を返す
    (2026-08-16、Push通知経由の増分同期用)。

    `historyTypes=messageAdded`のみ対象とする(削除・ラベル変更等は本同期の対象外)。
    ページネーション(`nextPageToken`)に対応し、全ページ分をまとめて返す。
    `start_history_id`が古すぎる場合、Gmail APIは404を返す(`HistoryIdExpiredError`を送出する
    ため、呼び出し元はフル同期にフォールバックすること)。
    """
    message_ids: list[str] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
        }
        if page_token:
            params["pageToken"] = page_token
        response = request_with_retry(
            "GET",
            f"{_GMAIL_API_BASE}/history",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        if response.status_code == 404:
            raise HistoryIdExpiredError(response.status_code, extract_error_message(response))
        raise_for_error(response, GmailApiError)
        data = response.json()
        for record in data.get("history", []):
            for added in record.get("messagesAdded", []):
                message = added.get("message") or {}
                message_id = message.get("id")
                if message_id:
                    message_ids.append(message_id)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return message_ids


def get_message(access_token: str, message_id: str) -> GmailMessage:
    response = request_with_retry(
        "GET",
        f"{_GMAIL_API_BASE}/messages/{message_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
    )
    raise_for_error(response, GmailApiError)
    data = response.json()
    headers = data.get("payload", {}).get("headers", [])
    return GmailMessage(
        id=data["id"],
        from_header=_header_value(headers, "From") or "",
        to_header=_header_value(headers, "To") or "",
        subject=_header_value(headers, "Subject"),
        date_header=_header_value(headers, "Date"),
        snippet=data.get("snippet"),
    )
