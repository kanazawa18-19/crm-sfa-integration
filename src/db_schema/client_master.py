"""① 取引先マスターDB（CLI-xxx）。

既存の稼働中Notionワークスペースに実在するDB（database_id=b8c17123-96a1-429e-82f1-
9d39595c9861）のプロパティ構成を、Notion API `GET /v1/databases/{id}` で取得した
実データそのまま反映する。仕様書03節が想定していた新規6DB設計ではなく、この実データ
構造を正として扱う。

■ 設計意図（【営業部】営業ステータス）: このプロパティはrollup型（案件管理DBの
営業ステータスから自動集計）であり、取引先側に直接書き込める営業ステータス列は
存在しない。つまり取引先の営業ステータスは、配下の案件（案件管理DB）の状況から
自動的に判断される設計であり、取引先マスター単体で手動編集する手段は無い。
"""

from __future__ import annotations

from src.db_schema.base import (
    DatabaseSchema,
    PropertyDefinition,
    PropertyType,
    RequirementLevel,
    SyncScope,
)

# 都道府県 select の実データは、標準47都道府県だけでなく郵便番号・市区町村名・
# 表記ゆれ・海外国名等が混在する。Notion API `GET /v1/databases/{id}` で取得した
# 実際のoptions一覧をそのまま反映する。
_PREFECTURE_OPTIONS: tuple[str, ...] = (
    "和歌山県", "三重県", "静岡県", "愛知県", "群馬", "岐阜県", "北海道", "大阪府",
    "滋賀県", "埼玉県", "茨城県", "東京都", "島根県", "広島県", "茨城", "青森",
    "東京", "兵庫県", "高知県", "沖縄県", "千葉", "千葉県", "京都府", "長野県",
    "宮城県", "山梨県", "神奈川県", "栃木県", "富山県", "愛媛県", "新潟県", "奈良県",
    "岡山県", "長崎県", "山形県", "鹿児島県", "福岡県", "福井県", "大分県", "秋田県",
    "福島県", "鳥取県", "岩手県", "熊本県", "山口県", "香川県", "群馬県", "石川県",
    "佐賀県", "青森県", "宮崎県", "徳島県", "981-0504", "山梨", "大阪", "京都市",
    "新潟", "カンボジア王国", "81", "鹿児島", "和歌山", "24", "兵庫県兵庫県",
    "福岡県福岡県", "栃木県那須郡那須町大字湯本212",
)

CLIENT_MASTER_SCHEMA = DatabaseSchema(
    key="client_master",
    display_name="取引先マスターDB",
    id_prefix="CLI-",
    kintone_key="取引先マスタ レコード番号",
    zoho_key="取引先",
    zoho_api_module="Accounts",
    spreadsheet_sheet_name="取引先マスター",
    notion_database_id="b8c17123-96a1-429e-82f1-9d39595c9861",
    properties=(
        PropertyDefinition(
            name="取引先名",
            property_type=PropertyType.TITLE,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description="法人名・施設名",
        ),
        PropertyDefinition(
            name="取引先ID",
            property_type=PropertyType.UNIQUE_ID,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
            description="CLI-xxx。読取専用・自動採番",
        ),
        PropertyDefinition(
            name="顧客種別",
            property_type=PropertyType.SELECT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            # kintone実データ移行(2026-08-09)で判明した実データの主要カテゴリ
            # （ホテル・旅館/ビューティー等）を選択肢に追加した。追加前は元々の8種類しか
            # 無く、実データ6.2万件の大半が「その他」にフォールバックしていた。
            options=(
                "宿泊施設",
                "決済業社",
                "ホテル運営コンサル",
                "パートナー",
                "クリニック・医院",
                "【競合】ホテルWEB支援",
                "システムベンダー",
                "WEBマーケティング会社",
                "ホテル・旅館",
                "ビューティー",
                "飲食店",
                "テイクアウト＆デリバリー",
                "グループ本部",
                "その他",
                "代理店・協力企業",
                "台湾",
                "組合・団体など",
                "運営会社名",
                "OTA・サイト事業者",
                "従業員",
                "三密代官",
                "ビットスリープ",
            ),
        ),
        PropertyDefinition(
            name="都道府県",
            property_type=PropertyType.SELECT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="実データには郵便番号・市区町村名等の表記ゆれが混在する",
            options=_PREFECTURE_OPTIONS,
        ),
        PropertyDefinition(
            name="郵便番号",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="住所",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        # kintone移行の実データ検証（2026-08-10）で判明: transform_client_master()は元々
        # "TEL"/"FAX"キーを返していたが、既存の「電話番号」はロールアップ（読取専用、
        # 案件/アクション経由の自動集計）でありNotion側に書き込み可能な電話・FAX列が
        # 存在せず、本番書き込み時に確実にKeyErrorで失敗するバグだった。業務判断により
        # 取引先マスターDBへ新規プロパティとして追加した（Notion API PATCHで作成済み）。
        PropertyDefinition(
            name="TEL",
            property_type=PropertyType.PHONE,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="FAX",
            property_type=PropertyType.PHONE,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="決算",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="予算組の時期",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="日付",
            property_type=PropertyType.DATE,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="備考",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
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
            name="【営業部】案件管理DB",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="案件管理DBへの紐付け（dual_property）",
            relation_target="project",
        ),
        PropertyDefinition(
            name="【営業部・パーソネル】アクション履歴DB",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="アクション履歴DBへの紐付け（dual_property）",
            relation_target="action",
        ),
        # --- 以下、読み取り専用（rollup/button/created_time/unique_id） ---
        PropertyDefinition(
            name="最終アクション日",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="メルアポ",
            property_type=PropertyType.BUTTON,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="テレアポ",
            property_type=PropertyType.BUTTON,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="アクション担当",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="決済者名",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="作成日時",
            property_type=PropertyType.CREATED_TIME,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="メールアドレス",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="本社所在地",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="【営業部】営業ステータス",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
            description="案件管理DBから自動集計。取引先側に直接書き込める列は無い",
        ),
        PropertyDefinition(
            name="案件作成",
            property_type=PropertyType.BUTTON,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="本社アプローチ状況",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="担当者",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="運営会社",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="担当者（アクション時）",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="先方担当者",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="共有メモ",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="【営業部】案件ベース_アクション履歴",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="【営業部】提案済みサービス",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="電話番号",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
    ),
)
