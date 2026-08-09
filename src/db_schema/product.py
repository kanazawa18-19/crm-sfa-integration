"""⑤ サービス・商品DB（PRD-xxx、新規独立）。

既存4DBと異なり、Notion側は現時点でtitleプロパティ「名前」のみを持つ空DB
（database_id=3b4d8ea8-d4f3-80ed-a431-c2e4f5561fd6）として存在する。
titleは仕様書03節が想定していた「サービスID（自動採番）」ではなく、既存のNotion側
「名前」をそのまま使う。仕様書03節の「サービス名」テキストプロパティは「名前」titleと
重複するため新設しない。

標準初期費用・標準月額費用は、既存4DB側の実データ（初期費用・月額費用）がNotionの
実際の型として number であることに合わせ、CURRENCY型ではなくNUMBER型で定義する。
"""

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
    zoho_api_module="Products",
    spreadsheet_sheet_name="サービス・商品",
    notion_database_id="3b4d8ea8-d4f3-80ed-a431-c2e4f5561fd6",
    properties=(
        PropertyDefinition(
            name="名前",
            property_type=PropertyType.TITLE,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description="Notion側で既存のtitleプロパティ。サービス名そのものを保持する",
        ),
        PropertyDefinition(
            name="課金形態",
            property_type=PropertyType.SELECT,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            options=("月額ストック", "イニシャルスポット", "成果報酬"),
        ),
        PropertyDefinition(
            name="標準初期費用",
            property_type=PropertyType.NUMBER,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="見積の基準値",
        ),
        PropertyDefinition(
            name="標準月額費用",
            property_type=PropertyType.NUMBER,
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
        # 2026-08-10、ZohoデータとNotionデータのマージに際し金沢さんの要望で追加
        # （dual_property。参照先の案件管理DB/取引先マスターDB/チェーンDB側にも
        # 「サービス・商品」という逆参照プロパティが自動生成される）。
        PropertyDefinition(
            name="案件管理",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="案件管理DBへの紐付け（dual_property）",
            relation_target="project",
        ),
        PropertyDefinition(
            name="取引先マスター",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="取引先マスターDBへの紐付け（dual_property）",
            relation_target="client_master",
        ),
        PropertyDefinition(
            name="チェーン",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="チェーンDBへの紐付け（dual_property）",
            relation_target="chain",
        ),
        *common_internal_properties(),
    ),
)
