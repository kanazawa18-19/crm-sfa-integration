"""⑤ サービス・商品DB（PRD-xxx、新規独立）。03_プロパティ定義 該当行を反映。"""

from __future__ import annotations

from src.db_schema.base import (
    DatabaseSchema,
    PropertyDefinition,
    PropertyType,
    RequirementLevel,
    SyncScope,
    common_internal_properties,
)

PRODUCT_SCHEMA = DatabaseSchema(
    key="product",
    display_name="サービス・商品DB",
    id_prefix="PRD-",
    kintone_key="サービス（ショット／ランニング）",
    zoho_key="サービス・商品",
    properties=(
        PropertyDefinition(
            name="サービスID",
            property_type=PropertyType.TITLE,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description="PRD-xxx",
        ),
        PropertyDefinition(
            name="サービス名",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description="リピッテ / メイリー / ホテラボ / オルト 等",
        ),
        PropertyDefinition(
            name="課金形態",
            property_type=PropertyType.SELECT,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description="月額ストック / イニシャルスポット / 成果報酬",
            options=("月額ストック", "イニシャルスポット", "成果報酬"),
        ),
        PropertyDefinition(
            name="標準初期費用",
            property_type=PropertyType.CURRENCY,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="見積の基準値",
        ),
        PropertyDefinition(
            name="標準月額費用",
            property_type=PropertyType.CURRENCY,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="見積の基準値",
        ),
        PropertyDefinition(
            name="クロスセル対象基準",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.NOTION_ONLY,
            description="併売の推奨条件を定義",
        ),
        *common_internal_properties(),
    ),
)
