"""kintone Webhookの受信ハンドラ（05_同期・競合制御「変更検知の仕組み」: kintone Webhook）。

kintoneの実際のWebhook通知は type/app/record を含む形で送信され、recordは
kintone REST APIのレコード取得結果と同じ形式（各フィールドが {"value": ...} でラップされる）。

想定ペイロード例（テストフィクスチャは tests/sync_engine/webhook_handlers/ を参照）:
{
  "type": "record.updated",
  "app": {"id": "123"},
  "record": {
    "$id": {"type": "__ID__", "value": "45"},
    "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
    "営業ステータス": {"type": "DROP_DOWN", "value": "商談中(B)"},
    "初期費用（イニシャル）": {"type": "NUMBER", "value": "500000"}
  }
}
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher, DispatchResult
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_headers import HEADER_NAME
from src.sync_engine.webhook_handlers._common import (
    bad_request_response,
    get_header,
    internal_error_response,
    logger,
    parse_iso_datetime,
    unauthorized_response,
    verify_webhook_secret,
)

_UPDATED_AT_FIELD_CODE = "更新日時"

# 03_プロパティ定義の内部管理項目（created_at/updated_at/last_synced_at）に相当するkintone標準
# フィールド。DatabaseSchema.propertiesとして個別管理される項目ではないため伝播対象から除く。
_SYSTEM_FIELD_CODES = frozenset(
    {"$id", "$revision", _UPDATED_AT_FIELD_CODE, "作成日時", "作成者", "更新者", "レコード番号"}
)

# config/.env.example の KINTONE_API_TOKEN_* と対になるアプリID。
# kintoneと同期するDBは取引先マスタ/案件管理/アクション管理の3アプリのみ（01_システム構成）。
_APP_ID_ENV_VARS: dict[str, str] = {
    "client_master": "KINTONE_APP_ID_CLIENT",
    "project": "KINTONE_APP_ID_PROJECT",
    "action": "KINTONE_APP_ID_ACTION",
}


def _default_app_id_to_db_key() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for db_key, env_var in _APP_ID_ENV_VARS.items():
        app_id = os.environ.get(env_var)
        if app_id:
            mapping[app_id] = db_key
    return mapping


def kintone_payload_to_sync_event(
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    app_id_to_db_key: Mapping[str, str] | None = None,
) -> SyncEvent:
    """kintone Webhookペイロードを共通のSyncEventへ変換する。"""
    resolver = app_id_to_db_key if app_id_to_db_key is not None else _default_app_id_to_db_key()
    app_id = str(payload["app"]["id"])
    db_key = resolver.get(app_id)
    if db_key is None:
        raise ValueError(f"unknown kintone app id: {app_id!r}")

    record = payload["record"]
    record_id = record["$id"]["value"]
    occurred_at = parse_iso_datetime(record[_UPDATED_AT_FIELD_CODE]["value"])
    properties = {
        code: field["value"] for code, field in record.items() if code not in _SYSTEM_FIELD_CODES
    }

    return SyncEvent(
        source_tool=Tool.KINTONE,
        db_key=db_key,
        external_id=record_id,
        occurred_at=occurred_at,
        properties=properties,
        sync_system_id=get_header(headers, HEADER_NAME),
    )


def handler(
    event: Mapping[str, Any], context: object, *, dispatcher: Dispatcher | None = None
) -> dict[str, Any]:
    """Lambda/Cloud Functions エントリポイント（API Gateway形式のHTTPイベントを想定）。

    実際のデプロイ設定（SAM/Serverless Framework等）は範囲外。dispatcherを注入すれば
    変換後のSyncEventをそのままディスパッチする（未注入時は変換結果の検証のみ行う）。
    """
    headers = event.get("headers") or {}
    if not verify_webhook_secret(headers, "KINTONE_WEBHOOK_SECRET"):
        return unauthorized_response()

    try:
        body = event.get("body")
        payload = json.loads(body) if isinstance(body, str) else (body or {})
        sync_event = kintone_payload_to_sync_event(payload, headers)
    except json.JSONDecodeError as exc:
        return bad_request_response(f"invalid JSON payload: {exc}")
    except (KeyError, ValueError) as exc:
        return bad_request_response(str(exc))
    except Exception:
        logger.exception("unexpected error while parsing kintone webhook payload")
        return internal_error_response()

    try:
        result: DispatchResult | None = (
            dispatcher.dispatch(sync_event) if dispatcher is not None else None
        )
    except Exception:
        logger.exception("unexpected error while dispatching kintone sync event")
        return internal_error_response()

    return {
        "statusCode": 200,
        "body": json.dumps({"skipped": result.skipped if result is not None else None}),
    }
