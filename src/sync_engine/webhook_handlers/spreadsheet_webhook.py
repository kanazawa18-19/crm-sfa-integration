"""スプレッドシートWebhookの受信ハンドラ（05_同期・競合制御「変更検知の仕組み」: GASのonEdit）。

Google Apps ScriptのonEditトリガーが、編集された行の全カラム値をJSONでPOSTする想定
（GASスクリプト自体の実装は本リポジトリのスコープ外。09_開発ロードマップ側の別タスク）。

なお、同期エンジンがSheets API経由で行う書き込みはGASのシンプルトリガーonEditを発火させない
（Apps Scriptの仕様上、API経由の変更はonEditの対象外）ため、この経路では無限ループはほぼ
発生しない。とはいえ他ハンドラとの一貫性・将来のGAS実装変更に備え、X-Sync-System-IDヘッダー
のチェックは同様に行う。

想定ペイロード例（テストフィクスチャは tests/sync_engine/webhook_handlers/ を参照）。
sheet はDBの表示名から末尾「DB」を除いたタブ名と仮定する（例:「案件管理DB」→「案件管理」）:
{
  "sheet": "案件管理",
  "row": 42,
  "editedAt": "2026-08-05T09:00:00+09:00",
  "values": {
    "案件ID": "MSA-PJ-001",
    "営業ステータス": "提案中",
    "初期費用（イニシャル）": 500000
  }
}
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from src.db_schema.base import Tool
from src.db_schema.registry import ALL_SCHEMAS
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


def _default_sheet_to_db_key() -> dict[str, str]:
    # display_nameから機械的に導出すると表示名変更で壊れるため、明示的なspreadsheet_sheet_name
    # フィールドを使う（WARN対応）。
    return {schema.spreadsheet_sheet_name: schema.key for schema in ALL_SCHEMAS}


def spreadsheet_payload_to_sync_event(
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    sheet_to_db_key: Mapping[str, str] | None = None,
) -> SyncEvent:
    """GAS onEdit由来のWebhookペイロードを共通のSyncEventへ変換する。"""
    resolver = sheet_to_db_key if sheet_to_db_key is not None else _default_sheet_to_db_key()
    sheet = payload["sheet"]
    db_key = resolver.get(sheet)
    if db_key is None:
        raise ValueError(f"unknown spreadsheet sheet name: {sheet!r}")

    return SyncEvent(
        source_tool=Tool.SPREADSHEET,
        db_key=db_key,
        external_id=str(payload["row"]),
        occurred_at=parse_iso_datetime(payload["editedAt"]),
        properties=dict(payload.get("values") or {}),
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
    if not verify_webhook_secret(headers, "SPREADSHEET_WEBHOOK_SECRET"):
        return unauthorized_response()

    try:
        body = event.get("body")
        payload = json.loads(body) if isinstance(body, str) else (body or {})
        sync_event = spreadsheet_payload_to_sync_event(payload, headers)
    except json.JSONDecodeError as exc:
        return bad_request_response(f"invalid JSON payload: {exc}")
    except (KeyError, ValueError) as exc:
        return bad_request_response(str(exc))
    except Exception:
        logger.exception("unexpected error while parsing spreadsheet webhook payload")
        return internal_error_response()

    try:
        result: DispatchResult | None = (
            dispatcher.dispatch(sync_event) if dispatcher is not None else None
        )
    except Exception:
        logger.exception("unexpected error while dispatching spreadsheet sync event")
        return internal_error_response()

    return {
        "statusCode": 200,
        "body": json.dumps({"skipped": result.skipped if result is not None else None}),
    }
