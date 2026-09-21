"""Gmail REST APIを直接叩く最小クライアント(2026-08-16)。

google-api-python-clientは使わず、既存のrequests + `_http.request_with_retry`基盤
(calendar_sync/web_engagement_tool_client.py等と同じ)でREST呼び出しのみ実装する
(依存を増やさない方針)。OAuthリフレッシュトークンからアクセストークンを取得する処理も
自前で行う(google-authライブラリのCredentials/Requestは使わず、Googleのtoken
エンドポイントを直接叩く — こちらの方が依存が少なく、リトライ・タイムアウト設定も
既存の共有ヘルパーに統一できる)。
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from dataclasses import dataclass, field
from typing import Any, Iterator

from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_MAX_RATE_LIMIT_RETRIES,
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

# Gmail API / OAuth token エンドポイントへの429リトライ回数(2026-09-21)。
# 既定は`_http.DEFAULT_MAX_RATE_LIMIT_RETRIES`(30回。過去分の一括取り込みスクリプトのような
# 数時間規模のバッチではこれでよい)。一方、Vercel関数(300秒で強制終了)の中で動くGmail Push
# 経路では、429を30回×最大30秒待つと関数ごとタイムアウト→Pub/Subが再送→また30回待つ、の
# 増幅ループになる(2026-09-21の本番障害)。関数の中から呼ぶ側は`bounded_rate_limit_retries()`で
# 数回に絞ること。本モジュールの関数は引数の形を変えず、この値を各呼び出しで参照する
# (同期処理の深い所まで引数を通さずに済ませるため。contextvarなので、ハンドラ全体を
# 1本のスレッドで動かす限り中の呼び出し全部に効く)。
_max_rate_limit_retries: contextvars.ContextVar[int] = contextvars.ContextVar(
    "gmail_max_rate_limit_retries", default=DEFAULT_MAX_RATE_LIMIT_RETRIES
)


def current_max_rate_limit_retries() -> int:
    """いま有効な429リトライ回数(既定は`DEFAULT_MAX_RATE_LIMIT_RETRIES`)。"""
    return _max_rate_limit_retries.get()


@contextlib.contextmanager
def bounded_rate_limit_retries(max_retries: int) -> Iterator[None]:
    """このブロックの中で行うGmail API呼び出しの429リトライ回数を`max_retries`に絞る。"""
    token = _max_rate_limit_retries.set(max_retries)
    try:
        yield
    finally:
        _max_rate_limit_retries.reset(token)


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
        max_rate_limit_retries=current_max_rate_limit_retries(),
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
        max_rate_limit_retries=current_max_rate_limit_retries(),
    )
    raise_for_error(response, GmailApiError)
    return [GmailMessageRef(id=m["id"]) for m in response.json().get("messages", [])]


@dataclass(frozen=True)
class GmailMessagePage:
    """`list_messages_page()`の1ページ分。`next_page_token`がNoneなら最終ページ。"""

    refs: list[GmailMessageRef]
    next_page_token: str | None


# Gmail APIが`messages.list`で1回に返せる上限。
_MAX_MESSAGES_PER_PAGE = 500


def list_messages_page(
    access_token: str,
    *,
    query: str,
    page_token: str | None = None,
    max_results: int = _MAX_MESSAGES_PER_PAGE,
) -> GmailMessagePage:
    """任意の検索クエリで`messages.list`を1ページ分だけ叩く(2026-09-03、過去分の
    取り込み用に追加)。

    `list_recent_messages()`は「直近2日・最大100件・1ページのみ」という日次同期専用の
    決め打ちで、過去数か月分を辿るのに使えないため分離した。日次同期の挙動を変えると
    本番のリアルタイム同期に影響するため、既存関数はそのまま残している。
    """
    params: dict[str, Any] = {"q": query, "maxResults": min(max_results, _MAX_MESSAGES_PER_PAGE)}
    if page_token:
        params["pageToken"] = page_token
    response = request_with_retry(
        "GET",
        f"{_GMAIL_API_BASE}/messages",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        max_rate_limit_retries=current_max_rate_limit_retries(),
    )
    raise_for_error(response, GmailApiError)
    body = response.json()
    return GmailMessagePage(
        refs=[GmailMessageRef(id=m["id"]) for m in body.get("messages", [])],
        next_page_token=body.get("nextPageToken"),
    )


@dataclass(frozen=True)
class GmailMessage:
    id: str
    from_header: str
    to_header: str
    subject: str | None
    date_header: str | None
    snippet: str | None
    # 同じやり取りの束(2026-09-03、ChatGPTレビュー指摘)。返信ラグを「同じスレッド内の
    # 送信→受信」に限るために使う。これが無いと、別件で届いたメールを直前の送信への
    # 返信として数えてしまい、複数案件を並行している相手ほど中央値が短く出る。
    thread_id: str | None = None
    # Gmailが記録している受信/送信時刻(epochミリ秒、2026-09-03、ChatGPTレビュー指摘)。
    # `Date:`ヘッダーは送信側が作る値で、PCの時計ずれ・遅延配送・壊れた書式でズレる。
    # 「相手のメールが実際に届いた時間帯」を数えるならこちらが正しい。
    internal_date_ms: str | None = None


def _header_value(headers: list[dict[str, Any]], name: str) -> str | None:
    for h in headers:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value")
    return None


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
        max_rate_limit_retries=current_max_rate_limit_retries(),
    )
    raise_for_error(response, GmailApiError)
    return response.json()


class HistoryIdExpiredError(GmailApiError):
    """`startHistoryId`が古すぎてGmail側が404を返した場合に送出する(フル同期へのフォールバック
    のトリガー)。Gmailのhistoryレコードは一定期間で失効するため、Push未受信期間が長い等の
    理由で発生しうる(呼び出し元はsync.sync_rep()による通常のフル同期にフォールバックすること)。
    """


@dataclass(frozen=True)
class HistoryListResult:
    message_ids: list[str]
    # レスポンス自体に含まれる、その時点でのmailboxの最新historyId。呼び出し完了後に別途
    # `GET /users/me/profile`等で"現在の"historyIdを取得すると、取得完了までの間に新着
    # メールが来た場合その新着分のhistoryIdが保存値未満になり得て、次回`list_history()`で
    # 二度と拾えなくなる(恒久的な見逃し)。必ずこのレスポンス由来の値を使うこと
    # (2026-08-16、shirokuma-secレビューWARN対応)。全ページ中`historyId`が含まれる最後の
    # ページの値を採用する(取得できなければNone)。
    history_id: str | None
    # メッセージID → そのメッセージを載せていたhistoryレコードの`id`(2026-09-21)。
    # Push経路が時間予算で途中終了したとき、「最後まで処理し終えたレコード」の`id`を
    # `historyId`として保存し、次回はそこから再開するために使う(`sync.sync_rep_incremental()`)。
    # 処理し終えた先頭部分を毎回やり直して末尾へ永遠に届かない、を防ぐ。
    message_history_ids: dict[str, str] = field(default_factory=dict)


def list_history(access_token: str, start_history_id: str) -> HistoryListResult:
    """`GET /users/me/history`で`start_history_id`以降に追加されたメッセージIDの一覧、および
    レスポンス自体に含まれる最新の`historyId`を返す(2026-08-16、Push通知経由の増分同期用)。

    `historyTypes=messageAdded`のみ対象とする(削除・ラベル変更等は本同期の対象外)。
    ページネーション(`nextPageToken`)に対応し、全ページ分をまとめて返す。
    `start_history_id`が古すぎる場合、Gmail APIは404を返す(`HistoryIdExpiredError`を送出する
    ため、呼び出し元はフル同期にフォールバックすること)。
    """
    message_ids: list[str] = []
    message_history_ids: dict[str, str] = {}
    latest_history_id: str | None = None
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
            max_rate_limit_retries=current_max_rate_limit_retries(),
        )
        if response.status_code == 404:
            raise HistoryIdExpiredError(response.status_code, extract_error_message(response))
        raise_for_error(response, GmailApiError)
        data = response.json()
        for record in data.get("history", []):
            record_id = record.get("id")
            for added in record.get("messagesAdded", []):
                message = added.get("message") or {}
                message_id = message.get("id")
                if message_id:
                    message_ids.append(message_id)
                    if record_id:
                        message_history_ids[message_id] = str(record_id)
        response_history_id = data.get("historyId")
        if response_history_id:
            latest_history_id = str(response_history_id)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return HistoryListResult(
        message_ids=message_ids,
        history_id=latest_history_id,
        message_history_ids=message_history_ids,
    )


def get_message(access_token: str, message_id: str) -> GmailMessage:
    response = request_with_retry(
        "GET",
        f"{_GMAIL_API_BASE}/messages/{message_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
        max_rate_limit_retries=current_max_rate_limit_retries(),
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
        thread_id=data.get("threadId"),
        internal_date_ms=data.get("internalDate"),
    )
