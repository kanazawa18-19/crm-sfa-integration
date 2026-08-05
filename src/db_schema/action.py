"""⑥ アクション管理DB（SA-AC-xxx）。03_プロパティ定義 該当行を反映。"""

from __future__ import annotations

from src.db_schema.base import (
    DatabaseSchema,
    PropertyDefinition,
    PropertyType,
    RequirementLevel,
    SyncScope,
    common_internal_properties,
)

ACTION_SCHEMA = DatabaseSchema(
    key="action",
    display_name="アクション管理DB",
    id_prefix="SA-AC-",
    kintone_key="アクション管理 レコード番号",
    zoho_key="アクション",
    properties=(
        PropertyDefinition(
            name="営業部アクションID",
            property_type=PropertyType.TITLE,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description="SA-AC-xxx",
        ),
        PropertyDefinition(
            name="アクション名",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="アクション種別",
            property_type=PropertyType.SELECT,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description="テレアポ / 訪問商談 / オンライン商談 / メール / 自動メール",
            options=("テレアポ", "訪問商談", "オンライン商談", "メール", "自動メール"),
        ),
        PropertyDefinition(
            name="アクション日",
            property_type=PropertyType.DATE,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description="接触回数の時系列判定に使用",
        ),
        PropertyDefinition(
            name="商談回数（何回目）",
            property_type=PropertyType.SELECT,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.ALL_TOOLS,
            description="【商談】1回目、2回目…。自動採番",
        ),
        PropertyDefinition(
            name="担当営業",
            property_type=PropertyType.USER,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description='kintone「対応者」に対応',
        ),
        PropertyDefinition(
            name="案件管理",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="接触回数の集計単位",
            relation_target="project",
        ),
        PropertyDefinition(
            name="取引先マスター",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            relation_target="client_master",
        ),
        PropertyDefinition(
            name="先方担当者",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="連絡先DBへ紐付け",
            relation_target="contact",
        ),
        PropertyDefinition(
            name="履歴メモ",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description='kintone「コメント」に対応',
        ),
        PropertyDefinition(
            name="録画・録音URL",
            property_type=PropertyType.URL,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.NOTION_ONLY,
            description="商談録画 / Notta等の音声ファイル",
        ),
        PropertyDefinition(
            name="kintone_Act_ID",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
            description="kintoneアクション管理レコード番号の一意キー",
        ),
        *common_internal_properties(),
    ),
)
