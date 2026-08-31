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


# kintone案件管理の実フィールドコード（2026-08-31、GET /k/v1/app/form/fields.json で検証）。
# コードとラベルが一致しないので、必ずコードで持つ。
_ACTION_TITLE_PROPERTY = "商談回数・電話回数・メール回数（何回目）"

_KINTONE_PROJECT_FACILITY_FIELD = "店舗名"  # ラベル: 施設名（会社名）
_KINTONE_PROJECT_SERVICE_FIELDS = (
    "ドロップダウン_0",  # ラベル: サービス（ショット）
    "複数選択",  # ラベル: サービス（ランニング）
    "複数選択_0",  # ラベル: サービス（イニシャル）
)


# kintoneアクション管理の実フィールドコード（2026-08-31、実APIで検証）。
_KINTONE_ACTION_CLIENT_FIELD = "client_name"  # ラベル: 顧客名（法人・個人・施設）
_KINTONE_ACTION_CONTENT_FIELD = "actionContent"  # ラベル: アクション内容


def _flatten_kintone_value(value: Any) -> list[str]:
    """kintoneの単一選択/複数選択の値を、空を除いた文字列のリストにする。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def compose_kintone_project_name(raw_record: Mapping[str, Any]) -> str | None:
    """kintoneの案件レコードから、案件管理DBの「案件名」を組み立てる。

    **kintoneの案件管理アプリには「案件名」に相当する項目が無い**（2026-08-31確認）。
    そのため、そのままでは必須プロパティが埋まらず、kintone発の新規案件は
    Notionに1件も作られない状態だった。

    金沢さんの方針（2026-08-31）に従い「施設名（会社名）＋提案サービス名」で組み立てる。
    **片方しか無ければ、あるほうだけを使う。** 両方無ければNoneを返し、
    従来どおり必須プロパティ不足としてSlackへ通知する（勝手に埋めない）。

    サービスはショット/ランニング/イニシャルの3項目に分かれているため、
    重複を除いて出現順に「・」でつなぐ。
    """
    facility = str(raw_record.get(_KINTONE_PROJECT_FACILITY_FIELD) or "").strip()

    services: list[str] = []
    for field in _KINTONE_PROJECT_SERVICE_FIELDS:
        for name in _flatten_kintone_value(raw_record.get(field)):
            if name not in services:
                services.append(name)

    parts = [part for part in (facility, "・".join(services)) if part]
    return " ".join(parts) if parts else None


def compose_kintone_action_title(raw_record: Mapping[str, Any]) -> str | None:
    """kintoneのアクションレコードから、アクション履歴DBのタイトルを組み立てる。

    **kintoneのアクション管理にはタイトルに相当する項目が無い**（2026-08-31確認。
    フィールドは 顧客名／アクション内容／対応者／コメント／次回アクション日／
    提案サービス／担当者名 のみ）。移行スクリプトもこのプロパティを作っていない。
    そのため、そのままでは必須プロパティが埋まらず、kintone発の新規アクションは
    Notionに1件も作られない状態だった。

    案件名と同じ方針で「顧客名＋アクション内容」を組み立てる。
    **片方しか無ければ、あるほうだけを使う。** 両方無ければNoneを返し、
    従来どおり必須プロパティ不足としてSlackへ通知する（勝手に埋めない）。

    プロパティ名が「商談回数・電話回数・メール回数（何回目）」なのは、Notion側で
    タイトル列に付けられている名前がこれだから（回数そのものを入れる欄ではなく、
    実際にはZoho側も「テレアポ」等のアクション名が入っている）。
    """
    client = str(raw_record.get(_KINTONE_ACTION_CLIENT_FIELD) or "").strip()
    content = str(raw_record.get(_KINTONE_ACTION_CONTENT_FIELD) or "").strip()
    parts = [part for part in (client, content) if part]
    return " ".join(parts) if parts else None


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

    # kintoneの案件管理には「案件名」に相当する項目が無いため、ここで組み立てる。
    # 1フィールド→1プロパティ固定の対応表では複数フィールドを合成できないので、
    # レコード全体を持っているこの新規作成の経路でだけ行う（更新時は組み立て直さない。
    # 一度付いた案件名が更新のたびに書き換わるのは意図と違う）。
    if db_key == "project" and "案件名" not in properties:
        composed = compose_kintone_project_name(raw_record)
        if composed is not None:
            properties["案件名"] = composed

    # アクション管理も同じ理由（kintone側にタイトルに相当する項目が無い）。
    if db_key == "action" and _ACTION_TITLE_PROPERTY not in properties:
        composed = compose_kintone_action_title(raw_record)
        if composed is not None:
            properties[_ACTION_TITLE_PROPERTY] = composed

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
