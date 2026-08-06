"""④ 案件管理DB（MSA-PJ-xxx）。

既存の稼働中Notionワークスペースに実在するDB（database_id=418adcbb-3714-4c90-
9c04-da0bfca4df09）のプロパティ構成を実データそのまま反映する。
"""

from __future__ import annotations

from src.db_schema.base import (
    DatabaseSchema,
    PropertyDefinition,
    PropertyType,
    RequirementLevel,
    SyncScope,
)

# 案件管理DB「営業ステータス」の実データ11値。分析ロジック（着地予測・勝率等）は
# ステータス文字列を直接見るのではなく、以下4区分マッピングを介して判定する。
# 命名パターンは既存のsrc/analytics/forecast.pyが使っていたものを踏襲している
# （forecast.py自体の参照先切り替えは本タスクの対象外。次のタスクで追従修正する）。

# 契約が確定した案件。
CONFIRMED_STATUSES = frozenset({"施設契約", "契約"})

# 解約により終了した案件。
CANCELLED_STATUSES = frozenset({"解約"})

# 失注により終了した案件。
LOST_STATUSES = frozenset({"失注"})

# 契約・失注・解約のいずれにも至っておらず、今後決着しうる進行中の案件。
ACTIVE_STATUSES = frozenset(
    {"リスケ", "アポ", "Dヨミ", "Cヨミ", "Bヨミ", "Aヨミ", "トライアル"}
)

# 後方互換のための単数形エイリアス（旧forecast.pyの命名: CONFIRMED_STATUS/LOST_STATUS/
# CANCELLED_STATUSは単一値を想定していたが、実データは「施設契約」「契約」のように
# 契約済みが2値存在するため、単数形は代表値のみを指す点に注意。集合全体の判定には
# 上記の複数形（*_STATUSES）を使うこと。
CONFIRMED_STATUS = "契約"
LOST_STATUS = "失注"
CANCELLED_STATUS = "解約"


def classify_status(status: str) -> str:
    """営業ステータス実データ値を4区分（契約済／失注／解約／進行中）へマッピングする。

    未知のステータス値が渡された場合はValueErrorを送出する（サイレントに
    「進行中」等へフォールバックすると分析結果が誤った方向に倒れるため）。
    """
    if status in CONFIRMED_STATUSES:
        return "契約済"
    if status in LOST_STATUSES:
        return "失注"
    if status in CANCELLED_STATUSES:
        return "解約"
    if status in ACTIVE_STATUSES:
        return "進行中"
    raise ValueError(f"unknown 営業ステータス value: {status!r}")


# 確度 select の並び順。A=確度が最も高い、D=確度が最も低い（S/A/B/Cではない点に注意）。
CONFIDENCE_LEVELS: tuple[str, ...] = ("A", "B", "C", "D")

PROJECT_SCHEMA = DatabaseSchema(
    key="project",
    display_name="案件管理DB",
    id_prefix="MSA-PJ-",
    kintone_key="案件管理 レコード番号",
    zoho_key="案件",
    zoho_api_module="Deals",
    spreadsheet_sheet_name="案件管理",
    notion_database_id="418adcbb-3714-4c90-9c04-da0bfca4df09",
    properties=(
        PropertyDefinition(
            name="案件名",
            property_type=PropertyType.TITLE,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="案件ID",
            property_type=PropertyType.UNIQUE_ID,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
            description="MSA-PJ-xxx。読取専用・自動採番",
        ),
        PropertyDefinition(
            name="営業ステータス",
            property_type=PropertyType.STATUS,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description="classify_status()で4区分（契約済／失注／解約／進行中）へマッピングする",
            options=(
                "施設契約",
                "解約",
                "リスケ",
                "失注",
                "アポ",
                "Dヨミ",
                "Cヨミ",
                "Bヨミ",
                "Aヨミ",
                "トライアル",
                "契約",
            ),
        ),
        PropertyDefinition(
            name="確度",
            property_type=PropertyType.SELECT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="A=確度高い〜D=確度低い（S/A/B/Cではない）",
            options=CONFIDENCE_LEVELS,
        ),
        PropertyDefinition(
            name="ファーストタッチ",
            property_type=PropertyType.MULTI_SELECT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            options=(
                "テレアポ",
                "メルアポ",
                "展示会",
                "紹介",
                "お問合せ",
                "横展開・追加提案",
                "メルマガ（CRMプロジェクト）",
                "引継ぎ",
                "ホワイトペーパー",
                "個別相談（先方から営業個人へ）",
                "パートナー",
            ),
        ),
        PropertyDefinition(
            name="提案サービス",
            property_type=PropertyType.MULTI_SELECT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            options=(
                "リピッテ",
                "メイリー",
                "ホテルラボ St＋",
                "ホテルラボ St",
                "ホテルラボ Ri＋",
                "ホテルラボ Ri",
                "ホテルラボ In",
                "LevGo（クリエイティブラボ）",
                "ILCA（三密代官、HOTEL DX）",
                "WEB制作（楽天CP・自社HP）",
                "Growth Cube（オルト（alt））",
                "ホテルラボ レビュー（口コミ返信代行）",
                "ノバシテ",
                "ホテルラボ WEBサポート",
                "レセプション",
                "その他",
                "ビールオーダー",
                "パーソネル",
                "フルスコ",
                "レベニューマネジメント",
                "デザ丸",
            ),
        ),
        PropertyDefinition(
            name="サイトコントローラー",
            property_type=PropertyType.MULTI_SELECT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            options=("リンカーン", "手間いらず", "ねっぱん", "らく通", "Beds24", "エアホスト", "なし", "不明"),
        ),
        PropertyDefinition(
            name="例外スイッチ（途中解約・複数サービス提案など）",
            property_type=PropertyType.CHECKBOX,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="かつやさん",
            property_type=PropertyType.CHECKBOX,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="問合せ",
            property_type=PropertyType.CHECKBOX,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="担当者名",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="決裁者名",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="ネックポイント",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="失注理由",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="次回アクション",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="メモ",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="テキスト",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="メールアドレス",
            property_type=PropertyType.EMAIL,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="電話番号",
            property_type=PropertyType.PHONE,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="初期費用",
            property_type=PropertyType.NUMBER,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="スポット売上の計算元",
        ),
        PropertyDefinition(
            name="月額費用",
            property_type=PropertyType.NUMBER,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="MRR計算の元",
        ),
        PropertyDefinition(
            name="【例外】粗利",
            property_type=PropertyType.NUMBER,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="例外スイッチON時の手動粗利上書き値",
        ),
        PropertyDefinition(
            name="サービス数（施設数）",
            property_type=PropertyType.NUMBER,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="ショット",
            property_type=PropertyType.NUMBER,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="契約日 / 予想契約日",
            property_type=PropertyType.DATE,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="売上計上月・課金開始予定日の判定基準",
        ),
        PropertyDefinition(
            name="次回アクション日",
            property_type=PropertyType.DATE,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="失注日",
            property_type=PropertyType.DATE,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="再アプローチ日",
            property_type=PropertyType.DATE,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="担当メンバー",
            property_type=PropertyType.USER,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="日報のメンバー別集計キー",
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
        PropertyDefinition(
            name="アクション履歴",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="アクション履歴DBへの紐付け（dual_property）",
            relation_target="action",
        ),
        PropertyDefinition(
            name="申込書・契約書",
            property_type=PropertyType.FILES,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.NOTION_ONLY,
            description="Any-to-Any同期のスコープ外（ファイル同期は非対応）",
        ),
        PropertyDefinition(
            name="見積書",
            property_type=PropertyType.FILES,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.NOTION_ONLY,
            description="Any-to-Any同期のスコープ外（ファイル同期は非対応）",
        ),
        # --- 以下、読み取り専用（formula/rollup/created_time/last_edited_time） ---
        PropertyDefinition(
            name="失注から90日オーバー",
            property_type=PropertyType.FORMULA,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="契約スピード",
            property_type=PropertyType.FORMULA,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="失注経過日数（日）",
            property_type=PropertyType.FORMULA,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="初期フィー",
            property_type=PropertyType.FORMULA,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="フィー率",
            property_type=PropertyType.FORMULA,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="経過日数",
            property_type=PropertyType.FORMULA,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="粗利",
            property_type=PropertyType.FORMULA,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="個人粗利",
            property_type=PropertyType.FORMULA,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="予算組のタイミング",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="アクション日",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="決算月",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="チェーン本社",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="アクションログ",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="最終更新日時",
            property_type=PropertyType.LAST_EDITED_TIME,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="作成日時",
            property_type=PropertyType.CREATED_TIME,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
    ),
)
