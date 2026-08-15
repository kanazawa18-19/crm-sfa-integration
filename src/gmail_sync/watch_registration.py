"""Gmail Push通知(`users.watch()` + Cloud Pub/Sub)のwatchチャンネル登録・延長(2026-08-16)。

`src/sync_engine/zoho_watch_channel.py`(Zoho CRM Notifications)と同じ思想を踏襲するが、
ZohoはCRM全体で1つのchannel_idを環境変数(`ZOHO_WATCH_CHANNEL_ID`)で管理するのに対し、
Gmailの`watch()`は担当者(メールボックス)ごとに個別のため、`RepGmailConnection`テーブルの
行ごとにDBで状態管理する(`dashboard/prisma/schema.prisma`の`historyId`/`watchExpiration`)。

Google仕様上、`watch()`の有効期限は登録・延長時点から最大7日。`renew_all_watches()`は
`GET /api/cron/gmail-watch-renewal`(Vercel Cron、1日1回)から呼ばれ、失効が近い(残り2日
以内)または未登録の担当者だけを対象に登録・延長する(全担当者を毎回叩く無駄を避ける)。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from src.gmail_sync import db, gmail_client
from src.gmail_sync.token_crypto import decrypt_token

logger = logging.getLogger(__name__)

# renew_all_watches()が「延長が必要」と判断する残り猶予日数。Google仕様の上限(7日)に対し
# 十分な安全マージンを取る(cronは1日1回のみのため、ぎりぎりまで待つと1回の実行漏れで
# 失効しうる)。
_RENEWAL_THRESHOLD_DAYS = 2

_PUBSUB_TOPIC_NAME_ENV_VAR = "GMAIL_PUBSUB_TOPIC_NAME"


class GmailWatchNotConfiguredError(Exception):
    """`topic_name`が指定されず、環境変数`GMAIL_PUBSUB_TOPIC_NAME`も未設定のため、
    watch登録・延長処理を実行できない場合に送出する。"""


def register_or_renew_watch(rep_email: str, refresh_token: str, topic_name: str) -> None:
    """1名分のGmail Push通知watchを登録・延長する。冪等(何度呼んでも安全、Zoho watch
    channelと同じ設計思想 — Google側が既存のwatchを上書きするため、重複登録による
    エラーは起きない)。"""
    access_token = gmail_client.refresh_access_token(refresh_token)
    result = gmail_client.watch_mailbox(access_token, topic_name)

    history_id = result.get("historyId")
    expiration_ms = result.get("expiration")
    if not history_id or not expiration_ms:
        raise gmail_client.GmailApiError(
            200, f"watch response missing historyId/expiration: {result!r}"
        )

    expiration = datetime.fromtimestamp(int(expiration_ms) / 1000, tz=timezone.utc)
    db.update_watch_state(rep_email, str(history_id), expiration)


def _needs_renewal(conn: db.RepGmailConnection, *, now: datetime) -> bool:
    if conn.watch_expiration is None:
        return True
    return conn.watch_expiration - now <= timedelta(days=_RENEWAL_THRESHOLD_DAYS)


def renew_all_watches(*, topic_name: str | None = None) -> dict[str, str]:
    """全`RepGmailConnection`をループし、失効が近い/未登録の担当者だけwatchを登録・延長する。

    `topic_name`省略時は環境変数`GMAIL_PUBSUB_TOPIC_NAME`を使う。どちらも得られない場合は
    `GmailWatchNotConfiguredError`を送出する(Gmail APIへは到達しない)。

    1名の延長失敗が他の担当の延長を止めないよう、担当ごとにtry/exceptで独立させる
    (`sync.sync_all()`と同じ方針)。戻り値は`{rep_email: "renewed"|"skipped"|"error: ..."}`。
    """
    resolved_topic_name = topic_name if topic_name is not None else os.environ.get(_PUBSUB_TOPIC_NAME_ENV_VAR)
    if not resolved_topic_name:
        raise GmailWatchNotConfiguredError(
            f"topic_nameが指定されておらず、環境変数{_PUBSUB_TOPIC_NAME_ENV_VAR}も未設定のため、"
            "Gmail watchの登録・延長対象のPub/Subトピックを特定できません。"
            f"{_PUBSUB_TOPIC_NAME_ENV_VAR}にGoogle Cloud側で作成済みのトピックのフルリソース名"
            "(例: projects/xxxx/topics/gmail-notifications)を設定してください。"
        )

    now = datetime.now(timezone.utc)
    results: dict[str, str] = {}
    for conn in db.list_gmail_connections():
        if not _needs_renewal(conn, now=now):
            results[conn.rep_email] = "skipped"
            continue
        try:
            refresh_token = decrypt_token(conn.refresh_token_enc)
            register_or_renew_watch(conn.rep_email, refresh_token, resolved_topic_name)
            results[conn.rep_email] = "renewed"
        except Exception as exc:
            logger.exception("gmail_sync: failed to renew watch for rep %s", conn.rep_email)
            results[conn.rep_email] = f"error: {exc}"
    return results
