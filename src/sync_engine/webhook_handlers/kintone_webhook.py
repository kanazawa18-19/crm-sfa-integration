"""kintone Webhookの受信ハンドラ（05_同期・競合制御「変更検知の仕組み」: kintone Webhook）。

2026-08-14、kintone→Notion方向のリアルタイム反映を有効化する方針となった（金沢さん確認済み。
それまでは「kintoneは他ツール→kintoneへの一方向書き込み専用」としていたが、双方向連携へ
拡張する）。kintone管理画面側でのWebhook購読設定は別途手動作業が必要（このモジュール自体は
それより前に実装済みのため、kintone側の設定と組み合わせて初めて実運用が始まる）。
手順の詳細は`docs/kintone_webhook_activation_note.md`を参照。

認証: kintoneのWebhook設定画面はカスタムHTTPヘッダーを送信できないため（kintone公式ヘルプで
確認済み、設定できるのは説明／Webhook URL／通知条件／有効化チェックボックスのみ）、
Webhook URL自体にクエリパラメータとして共有シークレットを埋め込む
（`verify_webhook_query_param()`参照）。

kintoneの実際のWebhook通知は type/app/record を含む形で送信され、recordは
kintone REST APIのレコード取得結果と同じ形式（各フィールドが {"value": ...} でラップされる）。
recordのキーはkintoneの実フィールドコードであり、これはNotionプロパティ名とは一致しない
ことが多い（例: kintoneの「契約進捗状況」列 → Notionの「営業ステータス」プロパティ）。
以前はフィールドコードをそのままNotionプロパティ名として扱う素朴な実装だったが、これでは
実際にはほぼ全てのプロパティがDispatcher側で「スキーマに存在しない」として黙って
スキップされ、kintone→Notionの反映が実質機能しないまま「設定済み」に見えてしまう
（Zoho側で2026-08-12に発覚した同種のBLOCKERと同じ落とし穴）。この変換は
`kintone_field_transforms.KINTONE_FIELD_TRANSFORMS`（フィールドコード→
(Notionプロパティ名, 値変換関数)、db_key別）に委譲する。リレーション解決が必要な
フィールドや派生値フィールドは意図的に対象外（詳細は`kintone_field_transforms.py`
のモジュールdocstring参照）。

想定ペイロード例（テストフィクスチャは tests/sync_engine/webhook_handlers/ を参照。
"商談中（B）"の括弧の全角/半角は、一括移行時に実CSVで確認済みの表記
`src/migration/project_mapping.py`の`_STATUS_ALIASES`に合わせている。ただしCSV
エクスポートとWebhook/REST APIは別の取得経路であり、`normalize_date`がCSVとNotion API
とで日付形式の違いを踏んだ前例があるため、**本番のkintone Webhookを有効化する前に
実際のペイロード（またはGET /k/v1/record.json）で1件確認し、この表記が実際に一致する
ことを確認すること**）:
{
  "type": "record.updated",
  "app": {"id": "123"},
  "record": {
    "$id": {"type": "__ID__", "value": "45"},
    "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
    "契約進捗状況": {"type": "DROP_DOWN", "value": "商談中（B）"},
    "提案料金（イニシャル）": {"type": "NUMBER", "value": "500000"}
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
    verify_webhook_query_param,
)
from src.sync_engine.webhook_handlers.kintone_field_transforms import KINTONE_FIELD_TRANSFORMS

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

    # KINTONE_FIELD_TRANSFORMSでフィールドコードごとに(Notionプロパティ名, 値変換関数)を
    # 引く（zoho_webhook.pyと同じ「1フィールド単位で失敗を閉じ込める」方針、モジュール
    # docstring参照）。マッピング未整備・意図的に対象外のフィールドコードや、値変換に失敗
    # した場合は、当該フィールドのみスキップしログを残す（他フィールドの処理やイベント
    # 全体は継続する）。
    field_mapping = KINTONE_FIELD_TRANSFORMS.get(db_key, {})
    properties: dict[str, Any] = {}
    for code, field in record.items():
        if code in _SYSTEM_FIELD_CODES:
            continue
        mapped = field_mapping.get(code)
        if mapped is None:
            logger.info(
                "kintone webhook: ignoring field code=%r for db_key=%r "
                "(not in KINTONE_FIELD_TRANSFORMS; excluded on purpose or not yet covered)",
                code,
                db_key,
            )
            continue
        notion_property, transform = mapped
        try:
            properties[notion_property] = transform(field["value"])
        except Exception:
            logger.warning(
                "kintone webhook: failed to transform field code=%r for db_key=%r; "
                "skipping this field only",
                code,
                db_key,
                exc_info=True,
            )

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

    認証: kintoneのWebhook設定画面はカスタムHTTPヘッダーもbodyへの任意フィールド追加も
    サポートせず、指定できるのは「Webhook URL」欄のみのため、他ハンドラのような
    verify_webhook_secret()（ヘッダー方式）は使えない。代わりにURLのクエリパラメータ
    （`?secret=...`）で共有シークレットを検証する`verify_webhook_query_param()`を使う
    （詳細は同関数のdocstring・`docs/kintone_webhook_activation_note.md`参照）。
    """
    headers = event.get("headers") or {}
    query_params = event.get("query_params") or {}
    if not verify_webhook_query_param(
        query_params, param_name="secret", env_var="KINTONE_WEBHOOK_SECRET"
    ):
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
