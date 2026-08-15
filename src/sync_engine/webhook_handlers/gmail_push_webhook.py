"""Gmail Push通知(Google Cloud Pub/Sub push subscription)の受信ハンドラ(2026-08-16)。

`src/gmail_sync/watch_registration.py`で登録した`users.watch()`により、対象メールボックスに
新着があるたびにGoogle CloudのPub/Subが本エンドポイントへpush通知を送ってくる想定。実際の
Pub/Sub push配信フォーマットは以下の形(Google公式ドキュメント通り):
{
  "message": {
    "data": "<base64エンコードされたJSON>",
    "messageId": "...",
    "publishTime": "..."
  },
  "subscription": "..."
}
`data`をbase64デコード＋JSONパースすると`{"emailAddress": "...", "historyId": "..."}`が
得られる(Gmail API公式ドキュメントのwatch通知ペイロード形式)。

認証: kintone_webhook.pyと同じくクエリパラメータ方式(`?token=...`)を使う。Pub/SubのOIDC
署名検証はより複雑なため、既存の踏襲パターン(共有シークレットのクエリパラメータ埋め込み)を
優先する(push subscription URL自体に`?token=<秘密の値>`を付与しておく設計。詳細は
`verify_webhook_query_param()`のdocstring参照)。

担当者が見つからない・処理中に例外が起きた場合も、Pub/Subの再送ループを防ぐため必ず200を
返す(ログにのみ記録する。`notify.py`の「副次的な連携なので例外を握りつぶす」方針と同じ
考え方 — ただしこちらは副次的というより「今回のPush分は次回のフル同期(セーフティネット)で
拾われる」という意味合い)。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any, Mapping

from src.db_schema.registry import get_schema
from src.gmail_sync import db, sync
from src.gmail_sync.token_crypto import decrypt_token
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.webhook_handlers._common import (
    logger,
    unauthorized_response,
    verify_webhook_query_param,
)

_CONTACT_DB_KEY = "contact"


def _default_contact_client() -> HttpNotionClient:
    schema = get_schema(_CONTACT_DB_KEY)
    return HttpNotionClient(_CONTACT_DB_KEY, schema.notion_database_id)


def _internal_domains() -> frozenset[str]:
    raw = os.environ.get("INTERNAL_EMAIL_DOMAINS", "")
    return frozenset(domain.strip().lower() for domain in raw.split(",") if domain.strip())


def _extract_email_address(payload: Mapping[str, Any]) -> str | None:
    """Pub/Sub pushペイロード(`{"message": {"data": "<base64>"}, ...}`)から
    `emailAddress`を取り出す。形式が想定外の場合はNoneを返す(例外にしない — 呼び出し元が
    常に200を返す方針のため、ここでは「取り出せなかった」ことだけ判別できればよい)。
    """
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    data_b64 = message.get("data")
    if not isinstance(data_b64, str):
        return None
    try:
        decoded = base64.b64decode(data_b64)
        inner = json.loads(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(inner, dict):
        return None
    email_address = inner.get("emailAddress")
    return email_address if isinstance(email_address, str) and email_address.strip() else None


def handler(
    event: Mapping[str, Any],
    context: object,
    *,
    contact_client: HttpNotionClient | None = None,
) -> dict[str, Any]:
    """Lambda/Cloud Functions エントリポイント(API Gateway形式のHTTPイベントを想定)。

    `contact_client`未注入時は環境変数から本番用の`HttpNotionClient`を構築する
    (テスト時のみ注入を想定)。
    """
    query_params = event.get("query_params") or {}
    if not verify_webhook_query_param(
        query_params, param_name="token", env_var="GMAIL_PUBSUB_VERIFICATION_TOKEN"
    ):
        return unauthorized_response()

    try:
        body = event.get("body")
        payload = json.loads(body) if isinstance(body, str) else (body or {})
    except json.JSONDecodeError:
        # Pub/Subの再送ループを防ぐため、ペイロード不正でも200を返す(モジュールdocstring参照)。
        logger.warning("gmail_push_webhook: received invalid JSON payload")
        return {"statusCode": 200, "body": json.dumps({"processed": False, "reason": "invalid_json"})}

    email_address = _extract_email_address(payload)
    if email_address is None:
        logger.warning("gmail_push_webhook: could not extract emailAddress from payload")
        return {
            "statusCode": 200,
            "body": json.dumps({"processed": False, "reason": "missing_email_address"}),
        }
    # `_extract_addresses()`(sync.py)等、他のメールアドレス比較箇所との一貫性に合わせ、
    # 比較前に小文字化する(2026-08-16、shirokuma-secレビューWARN対応。`RepGmailConnection.
    # repEmail`の保存側の大文字小文字ゆれ自体は別問題だが、少なくとも比較時点では吸収する)。
    email_address_normalized = email_address.strip().lower()

    try:
        conn = db.find_connection_by_email(email_address_normalized)
        if conn is None:
            logger.warning(
                "gmail_push_webhook: no RepGmailConnection found for emailAddress=%s",
                email_address_normalized,
            )
            return {
                "statusCode": 200,
                "body": json.dumps({"processed": False, "reason": "unknown_rep"}),
            }

        refresh_token = decrypt_token(conn.refresh_token_enc)
        client = contact_client if contact_client is not None else _default_contact_client()
        count = sync.sync_rep_incremental(
            conn.rep_email, refresh_token, client, internal_domains=_internal_domains()
        )
    except Exception:
        # Pub/Subの再送ループを防ぐため、処理中の例外でも200を返す(モジュールdocstring参照)。
        # 取りこぼした分は次回のsync_all()(日次セーフティネット)で拾われる。
        logger.exception(
            "gmail_push_webhook: unexpected error while processing push notification for %s",
            email_address_normalized,
        )
        return {"statusCode": 200, "body": json.dumps({"processed": False, "reason": "error"})}

    return {"statusCode": 200, "body": json.dumps({"processed": True, "logged_count": count})}
