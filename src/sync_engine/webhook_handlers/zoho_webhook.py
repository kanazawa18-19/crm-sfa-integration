"""Zoho CRM Webhookの受信ハンドラ（05_同期・競合制御「変更検知の仕組み」: Notification Webhook）。

01_システム構成「疎結合設計」：ENABLE_ZOHOがFalseの場合、本ハンドラはペイロードの変換すら
行わず早期リターンする（他システムに一切影響を与えずZoho連携を切り離せることを保証する）。

想定ペイロード例（テストフィクスチャは tests/sync_engine/webhook_handlers/ を参照）。
module は DatabaseSchema.zoho_api_module（実際のZoho CRM APIモジュール名。例: Deals）と
対応させる。zoho_key（移行元モジュール名の日本語ラベル）とは別物なので注意:
{
  "module": "Deals",
  "operation": "update",
  "token": "（scripts/register_zoho_webhook.pyが登録時に指定したtoken文字列がそのまま返る）",
  "data": [
    {
      "id": "4876876000000488001",
      "Modified_Time": "2026-08-05T09:00:00+09:00",
      "営業ステータス": "商談中(B)",
      "初期費用（イニシャル）": 500000
    }
  ]
}

認証: Zoho Notifications（watch）APIは着信リクエストへ任意のHTTPヘッダーを付与させる仕組みを
持たないため、他ハンドラのようなverify_webhook_secret()（X-Webhook-Secretヘッダー方式）は
使えない。代わりに上記の通りbody内の"token"フィールドをZOHO_WEBHOOK_SECRETと照合する
verify_webhook_body_token()（_common.py）を使う。詳細はdocs/zoho_webhook_activation_note.md参照。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from src.db_schema.base import Tool
from src.db_schema.registry import ALL_SCHEMAS, get_schema
from src.sync_engine.dispatcher import Dispatcher, DispatchResult
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_headers import HEADER_NAME
from src.sync_engine.sync_targets.zoho_sync import is_zoho_enabled
from src.sync_engine.webhook_handlers._common import (
    bad_request_response,
    get_header,
    internal_error_response,
    logger,
    parse_iso_datetime,
    unauthorized_response,
    verify_webhook_body_token,
)

_MODIFIED_TIME_FIELD = "Modified_Time"
_SYSTEM_FIELDS = frozenset({"id", _MODIFIED_TIME_FIELD})


def _default_module_to_db_key() -> dict[str, str]:
    # zoho_key（移行元Zohoモジュール名の日本語ラベル）ではなく、実際のZoho CRM APIの
    # module値と一致するzoho_api_moduleで逆引きする（BLOCKER4: zoho_keyは表示用ラベルであり
    # 実際のWebhookペイロードのmodule値とは一致しないため）。
    return {schema.zoho_api_module: schema.key for schema in ALL_SCHEMAS}


def zoho_payload_to_sync_event(
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    module_to_db_key: Mapping[str, str] | None = None,
) -> SyncEvent:
    """Zoho Notification Webhookペイロードを共通のSyncEventへ変換する。"""
    resolver = module_to_db_key if module_to_db_key is not None else _default_module_to_db_key()
    module = payload["module"]
    db_key = resolver.get(module)
    if db_key is None:
        raise ValueError(f"unknown Zoho module: {module!r}")

    records = payload.get("data") or []
    if not records:
        raise ValueError("zoho webhook payload has no data records")
    record = records[0]

    # Dispatcher側（未定義プロパティのスキップ）で根本的には保護されるが、Zoho側でも
    # 早期に警告ログを出しておく（Notionのようなrollup/formula型の大量発生は想定しにくいため、
    # notion_webhook.pyのような型ホワイトリストまでは設けない簡易対応）。
    schema = get_schema(db_key)
    properties: dict[str, Any] = {}
    for k, v in record.items():
        if k in _SYSTEM_FIELDS:
            continue
        try:
            schema.get_property(k)
        except KeyError:
            logger.warning(
                "ignoring unknown Zoho property '%s' for db_key=%r (not in schema)",
                k,
                db_key,
            )
            continue
        properties[k] = v

    return SyncEvent(
        source_tool=Tool.ZOHO,
        db_key=db_key,
        external_id=str(record["id"]),
        occurred_at=parse_iso_datetime(record[_MODIFIED_TIME_FIELD]),
        properties=properties,
        sync_system_id=get_header(headers, HEADER_NAME),
    )


def handler(
    event: Mapping[str, Any], context: object, *, dispatcher: Dispatcher | None = None
) -> dict[str, Any]:
    """Lambda/Cloud Functions エントリポイント（API Gateway形式のHTTPイベントを想定）。

    実際のデプロイ設定（SAM/Serverless Framework等）は範囲外。ENABLE_ZOHO=False時は
    ペイロード変換・dispatcherへのdispatchのいずれも行わずスキップする。

    Zohoはtokenをbody内に返すため、認証（verify_webhook_body_token）にはbodyのJSONパースが
    必要になる。他ハンドラ（ヘッダー方式）と異なり、JSONパース失敗（400）はtoken検証（401）
    より先に判定される点に注意。
    """
    if not is_zoho_enabled():
        return {"statusCode": 200, "body": json.dumps({"skipped": "zoho_disabled"})}

    headers = event.get("headers") or {}

    try:
        body = event.get("body")
        payload = json.loads(body) if isinstance(body, str) else (body or {})
    except json.JSONDecodeError as exc:
        return bad_request_response(f"invalid JSON payload: {exc}")

    # BLOCKER2: 構文的には正しいJSONでも辞書でない場合（例: "null"/"[1,2,3]"/"42"/"\"x\""/"true"）、
    # 未認証の送信者がこの時点でverify_webhook_body_token()内のpayload.get()に到達すると
    # AttributeErrorが未捕捉のまま外へ漏れてしまうため、JSONパース失敗と同様に400へ倒す。
    if not isinstance(payload, dict):
        return bad_request_response("request body must be a JSON object")

    if not verify_webhook_body_token(payload, token_field="token", env_var="ZOHO_WEBHOOK_SECRET"):
        return unauthorized_response()

    try:
        sync_event = zoho_payload_to_sync_event(payload, headers)
    except (KeyError, ValueError) as exc:
        return bad_request_response(str(exc))
    except Exception:
        logger.exception("unexpected error while parsing zoho webhook payload")
        return internal_error_response()

    try:
        result: DispatchResult | None = (
            dispatcher.dispatch(sync_event) if dispatcher is not None else None
        )
    except Exception:
        logger.exception("unexpected error while dispatching zoho sync event")
        return internal_error_response()

    return {
        "statusCode": 200,
        "body": json.dumps({"skipped": result.skipped if result is not None else None}),
    }
