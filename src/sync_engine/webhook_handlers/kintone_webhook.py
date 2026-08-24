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
ことが多い（例: 案件管理アプリの実フィールドコード"ドロップダウン_2"→表示ラベル「契約進捗
状況」→Notionの「営業ステータス」プロパティ）。**フィールドコードは表示ラベルとも一致しない
ことが多い**（2026-08-14、実際にWebhookを有効化した直後に発覚。当初はCSV移行データの列名
＝表示ラベルをそのままフィールドコードとして使ってしまい、実際にはほぼ全てのプロパティが
Dispatcher側で「スキーマに存在しない」として黙ってスキップされ、kintone→Notionの反映が
実質機能しないまま「設定済み」に見える状態になっていた。`GET /k/v1/app/form/fields.json`で
実際のコードを検証し修正済み。Zoho側で2026-08-12に発覚した同種のBLOCKERと同じ落とし穴）。
この変換は`kintone_field_transforms.KINTONE_FIELD_TRANSFORMS`（フィールドコード→
(Notionプロパティ名, 値変換関数)、db_key別）に委譲する。リレーション解決が必要な
フィールドや派生値フィールドは大半が意図的に対象外だが、⑥アクション管理の
「👨‍👩‍👧‍👦 取引先マスター」リレーションのみ2026-08-25に例外として対応した
（`src/relation_sync/`によるローカルインデックス経由の同期的な名寄せ。詳細は
`kintone_field_transforms.py`のモジュールdocstring参照）。

想定ペイロード例（テストフィクスチャは tests/sync_engine/webhook_handlers/ を参照。
フィールドコードは実際のkintone環境で検証済みの値、コメントに表示ラベルを付記する。
"商談中（B）"の括弧は一括移行時に実CSVで確認済みの全角表記だが、Webhook/REST API経由の
実データが半角括弧でも動くよう`normalize_project_status()`側で正規化している
（2026-08-14、金沢さん指摘対応。`src/migration/project_mapping.py`参照）:
{
  "type": "record.updated",
  "app": {"id": "123"},
  "record": {
    "$id": {"type": "__ID__", "value": "45"},
    "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
    "ドロップダウン_2": {"type": "DROP_DOWN", "value": "商談中（B）"},
    "初期費用": {"type": "NUMBER", "value": "500000"}
  }
}
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

from src.audit_log.actor_context import set_actor
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
from src.sync_engine.webhook_handlers.kintone_field_transforms import (
    KINTONE_FIELD_TRANSFORMS,
    SKIP_FIELD,
    kintone_action_record_context,
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


def _kintone_actor_label(record: Mapping[str, Any]) -> str | None:
    """kintoneレコードの「更新者」（無ければ「作成者」）フィールドから、監査ログの
    `actorLabel`に使う表示名を取り出す（obasan-qualityレビューWARN対応、2026-08-17）。
    実際のkintone REST API/Webhookのレコード表現では、これらのフィールドは
    `{"type": "MODIFIER"|"CREATOR", "value": {"code": "user1", "name": "山田太郎"}}`
    の形（`name`はkintone側のユーザー表示名設定に依存し、無い場合もある）。
    どちらも取れない場合はNone（`actorLabel`は省略可のため、その場合は経路名のみ記録される）。
    """
    modifier = record.get("更新者") or record.get("作成者")
    if not isinstance(modifier, dict):
        return None
    value = modifier.get("value")
    if not isinstance(value, dict):
        return None
    return value.get("name") or value.get("code")


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
    # kintone_action_record_context(): db_key="action"の"client_name"（取引先マスター
    # リレーション解決）がRelationReviewQueueへの記録に使うレコードIDを暗黙に伝播させる
    # （kintone_field_transforms.pyのモジュールdocstring参照）。他db_key/フィールドは
    # このコンテキストを参照しないため無害。
    with kintone_action_record_context(record_id):
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
                value = transform(field["value"])
            except Exception:
                logger.warning(
                    "kintone webhook: failed to transform field code=%r for db_key=%r; "
                    "skipping this field only",
                    code,
                    db_key,
                    exc_info=True,
                )
                continue
            if value is SKIP_FIELD:
                # 未解決のリレーション（例: 取引先マスターの名寄せが曖昧・候補なし）。
                # 既存のNoneハンドリング（明示的にプロパティをクリアする）とは意味が異なり、
                # このプロパティへの書き込み自体を行わない（既存の値を上書きしない）。
                logger.info(
                    "kintone webhook: relation unresolved for field code=%r for db_key=%r; "
                    "skipping this property (not clearing existing value)",
                    code,
                    db_key,
                )
                continue
            properties[notion_property] = value

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
        actor_label = _kintone_actor_label(payload.get("record") or {})
        with set_actor("kintone_webhook", label=actor_label):
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
