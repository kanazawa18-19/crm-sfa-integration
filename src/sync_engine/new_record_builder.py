"""新規レコード作成（`Dispatcher`の`unknown_record`スキップ解消）向けに、kintone/Zohoから
取得したレコード全体データをNotionプロパティへ変換する（2026-08-25、Round2）。

Webhookで届く変更差分（1〜数フィールド）向けに整備済みの1フィールド単位変換テーブル
（`KINTONE_FIELD_TRANSFORMS`/`ZOHO_LABEL_FIELD_MAPPINGS`）を、レコード全体の各フィールドに
対してループ適用する軽量な方式（`src/migration/migration_pipeline.py`のようなバッチ移行専用の
重い処理を流用せず、既存の1フィールド単位変換ロジックをそのまま再利用する。事前調査で
確認済みの方針）。

kintone/Zohoいずれのフィールドテーブルも「⑥アクション履歴DBの取引先マスターリレーション」
（`client_name`/`取引先`・`【Notion】取引先マスター`）を含んでおり、これらは新規レコード
作成時にも同じ解決ロジック（完全一致のみ自動、曖昧なら`RelationReviewQueue`へ）がそのまま
適用される。「案件」(project)のような紐付け先を特定できないリレーションは、いずれの
テーブルにもエントリが存在しないため、新規作成時も自然に空欄のまま作成される
（Round1・上記リレーション解決と同じ方針）。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.sync_engine.webhook_handlers.kintone_field_transforms import (
    KINTONE_FIELD_TRANSFORMS,
)
from src.sync_engine.webhook_handlers.kintone_field_transforms import (
    SKIP_FIELD as _KINTONE_SKIP_FIELD,
)
from src.sync_engine.webhook_handlers.kintone_field_transforms import (
    kintone_action_record_context,
)
from src.sync_engine.webhook_handlers.zoho_field_transforms import (
    SKIP_FIELD as _ZOHO_SKIP_FIELD,
)
from src.sync_engine.webhook_handlers.zoho_field_transforms import (
    ZOHO_LABEL_FIELD_MAPPINGS,
    zoho_action_relation_context,
)
from src.sync_engine.zoho_field_mapping import resolve_zoho_field_label

logger = logging.getLogger(__name__)


def build_notion_properties_for_new_record(
    *, source_tool: Tool, db_key: str, external_id: str, raw_record: Mapping[str, Any]
) -> dict[str, Any]:
    """kintone/Zohoから取得したレコード全体データ（`raw_record`）を、Notion新規ページ作成用の
    プロパティdict（プロパティ名→値、`build_notion_properties`が受け付ける内部形式）へ変換する。

    `raw_record`の形式は呼び出し元（`SyncTarget.get_record()`）が返すものと同じ:
    - kintone: フィールドコード→生の値のフラットな辞書（`unwrap_kintone_record`済み、
      `KintoneSyncTarget.get_record()`参照）。
    - Zoho: api_name→生の値の辞書（Zoho CRM APIのレスポンスそのまま、`ZohoSyncTarget.get_record()`
      参照）。

    マッピング未整備・意図的に対象外のフィールド、値変換に失敗したフィールドは黙ってスキップし
    （Webhook部分更新と同じ「1フィールド単位で失敗を閉じ込める」方針）、結果のdictにはそれ以外の
    フィールドを含める。必須プロパティが欠けていないかどうかの判定は呼び出し元
    （`Dispatcher`）の責務とする（本関数はプロパティの変換のみを行う）。
    """
    if source_tool is Tool.KINTONE:
        return _build_from_kintone_record(db_key=db_key, external_id=external_id, raw_record=raw_record)
    if source_tool is Tool.ZOHO:
        return _build_from_zoho_record(db_key=db_key, external_id=external_id, raw_record=raw_record)
    raise ValueError(f"unsupported source_tool for new record creation: {source_tool!r}")


def _build_from_kintone_record(
    *, db_key: str, external_id: str, raw_record: Mapping[str, Any]
) -> dict[str, Any]:
    field_mapping = KINTONE_FIELD_TRANSFORMS.get(db_key, {})
    properties: dict[str, Any] = {}
    # kintone_action_record_context(): db_key="action"のclient_name（取引先マスターリレーション
    # 解決）がRelationReviewQueueへの記録に使うレコードIDを暗黙に伝播させる（Webhookハンドラと
    # 同じ仕組み、kintone_field_transforms.pyのモジュールdocstring参照）。
    with kintone_action_record_context(external_id):
        for code, value in raw_record.items():
            mapped = field_mapping.get(code)
            if mapped is None:
                continue
            notion_property, transform = mapped
            try:
                transformed_value = transform(value)
            except Exception:
                logger.warning(
                    "new record creation: failed to transform kintone field code=%r for "
                    "db_key=%r (external_id=%r); skipping this field only",
                    code,
                    db_key,
                    external_id,
                    exc_info=True,
                )
                continue
            if transformed_value is _KINTONE_SKIP_FIELD:
                # 未解決のリレーション（取引先マスターの名寄せが曖昧・候補なし）。このプロパティ
                # は含めない（新規ページ作成時は「まだNotion側に値が無い」状態のため、Webhook
                # 部分更新の「既存値を上書きしない」とは異なり、単に空欄のまま作成される）。
                continue
            properties[notion_property] = transformed_value
    return properties


def _build_from_zoho_record(
    *, db_key: str, external_id: str, raw_record: Mapping[str, Any]
) -> dict[str, Any]:
    schema = get_schema(db_key)
    field_mapping = ZOHO_LABEL_FIELD_MAPPINGS.get(db_key, {})
    properties: dict[str, Any] = {}
    # zoho_action_relation_context(): db_key="action"の取引先マスターリレーション解決に
    # 必要な当該レコードの現在値・レコードID を伝播させる（Webhookハンドラと同じ仕組み）。
    # ここでは既に`raw_record`がレコード全体（field22/field6を含む）であるため、追加の
    # Zoho API呼び出し（zoho_client）は不要（zoho_client=None）。
    with zoho_action_relation_context(external_id, raw_record, None):
        for api_name, value in raw_record.items():
            label = resolve_zoho_field_label(schema.zoho_api_module, api_name)
            if label is None:
                continue
            mapped = field_mapping.get(label)
            if mapped is None:
                continue
            notion_property, transform = mapped
            try:
                transformed_value = transform(value)
            except Exception:
                logger.warning(
                    "new record creation: failed to transform Zoho field api_name=%r "
                    "(label=%r) for db_key=%r (external_id=%r); skipping this field only",
                    api_name,
                    label,
                    db_key,
                    external_id,
                    exc_info=True,
                )
                continue
            if transformed_value is _ZOHO_SKIP_FIELD:
                continue
            properties[notion_property] = transformed_value
    return properties
