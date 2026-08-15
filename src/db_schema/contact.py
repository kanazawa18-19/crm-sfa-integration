"""③ 連絡先DB（CNT-xxx、新規独立）。

既存4DBと異なり、Notion側は現時点でtitleプロパティ「名前」のみを持つ空DB
（database_id=3b4d8ea8-d4f3-808d-9853-d9cdd3de39ae）として存在する。
titleは仕様書03節が想定していた「連絡先ID（自動採番）」ではなく、既存のNotion側
「名前」をそのまま使う（Notion側で既にtitleとして存在するため、名前を変更すると
Notion API側で不整合が起きる可能性がある）。氏名は「名前」titleが担うため、
仕様書03節の「氏名」テキストプロパティは重複となり新設しない。

その他のプロパティは仕様書03節の定義をベースに、既存4DBの命名慣習（relationの
参照先プロパティ名は「取引先マスター」等）に合わせて設計する。
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

CONTACT_SCHEMA = DatabaseSchema(
    key="contact",
    display_name="連絡先DB",
    id_prefix="CNT-",
    kintone_key="担当者名1〜3（分割）",
    zoho_key="連絡先（リード）",
    zoho_api_module="Contacts",
    spreadsheet_sheet_name="連絡先",
    notion_database_id="3b4d8ea8-d4f3-808d-9853-d9cdd3de39ae",
    properties=(
        PropertyDefinition(
            name="名前",
            property_type=PropertyType.TITLE,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description="Notion側で既存のtitleプロパティ。氏名そのものを保持する",
        ),
        PropertyDefinition(
            name="取引先マスター",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description=(
                "所属企業。ただし"
                "webhook_handlers/lead_inquiry_webhook.py経由のレコードのみ、会社名が"
                "取引先マスターと完全一致しなかった場合に意図的に空のまま作成される"
                "（無数の重複取引先マスター作成を避けるための割り切り、2026-08-14）。"
                "この経路由来のレコードで空なのは仕様通りであり、データ品質バッチ等で"
                "誤ってバグ扱いしないこと。"
            ),
            relation_target="client_master",
        ),
        # 2026-08-10、ZohoデータとNotionデータのマージに際し金沢さんの要望で追加
        # （dual_property。参照先の案件管理DB/アクション履歴DB/チェーンDB側にも
        # 「連絡先」という逆参照プロパティが自動生成される）。
        PropertyDefinition(
            name="案件管理",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="案件管理DBへの紐付け（dual_property）",
            relation_target="project",
        ),
        PropertyDefinition(
            name="アクション履歴",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="アクション履歴DBへの紐付け（dual_property）",
            relation_target="action",
        ),
        PropertyDefinition(
            name="チェーン",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="チェーンDBへの紐付け（dual_property）",
            relation_target="chain",
        ),
        PropertyDefinition(
            name="部署",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="Eightからの更新対象",
        ),
        PropertyDefinition(
            name="役職",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="Eightからの更新対象",
        ),
        PropertyDefinition(
            name="メールアドレス",
            property_type=PropertyType.EMAIL,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="名寄せの一意キーとして使用",
        ),
        PropertyDefinition(
            name="携帯番号",
            property_type=PropertyType.PHONE,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="直通TEL",
            property_type=PropertyType.PHONE,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="Eight連携ID",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.NOTION_ONLY,
            description="Eight連携で自動投入",
        ),
        PropertyDefinition(
            name="名刺交換日",
            property_type=PropertyType.DATE,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.NOTION_ONLY,
            description="Eight連携で自動投入",
        ),
        PropertyDefinition(
            name="名刺交換者",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.NOTION_ONLY,
            description="Eight連携で自動投入",
        ),
        PropertyDefinition(
            name="人事異動フラグ",
            property_type=PropertyType.CHECKBOX,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.NOTION_ONLY,
            description="Eightで部署・役職の変更を検知した際にON",
        ),
        # 2026-08-13、web-engagement-tool（オンサイトエンゲージメントツール）との
        # CRM-SFA連携に向けて追加。リードのホットリード化をNotion側に反映するための
        # 受け皿（実際の同期ロジックは`src/sync_engine/webhook_handlers/web_engagement_webhook.py`
        # で実装済み。outbound方向の連携は`src/lead_sync/`を参照。全体像は
        # `docs/web_engagement_tool_integration_note.md`参照）。
        PropertyDefinition(
            name="リードスコア",
            property_type=PropertyType.NUMBER,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.NOTION_ONLY,
            description="web-engagement-tool側のリードスコア（0〜100程度の整数）",
        ),
        PropertyDefinition(
            name="ホットリード化日時",
            property_type=PropertyType.DATETIME,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.NOTION_ONLY,
            description="スコアが閾値を超えてホットリード化した日時",
        ),
        PropertyDefinition(
            name="Web接客ツールURL",
            property_type=PropertyType.URL,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.NOTION_ONLY,
            description="web-engagement-tool側のリード詳細画面へのリンク",
        ),
        # 2026-08-14、金沢さんの承認で追加（「連絡先DBの担当者プロパティは作ってもいいよ」）。
        # 案件管理DBの「担当メンバー」（PropertyType.USER）と同じ命名・型に揃え、Notion側の
        # 連絡先データベース(3b4d8ea8-d4f3-808d-9853-d9cdd3de39ae)にも実際にpeopleプロパティ
        # として追加済み。どのツール間でどう連携させるか（自動割り当てロジック等）は未設計
        # のため、現時点ではNotion側の値をそのまま保持するだけの箱として追加する。
        PropertyDefinition(
            name="担当メンバー",
            property_type=PropertyType.USER,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="連絡先の担当営業（Notion people）。案件管理DBの「担当メンバー」と同義",
        ),
        # 2026-08-16、Gmail連携移管（src/gmail_sync/）に伴い追加。生のメール送受信ログは
        # Postgres側のEmailLogテーブルに持ち、Notion側にはAPIレート制限・件数の都合上
        # ロールアップ（最終日時のみ）を書き込む。
        PropertyDefinition(
            name="最終メール日時",
            property_type=PropertyType.DATETIME,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.NOTION_ONLY,
            description="この連絡先との直近のメール送受信日時（gmail_syncが自動更新）",
        ),
        *common_internal_properties(),
    ),
)
