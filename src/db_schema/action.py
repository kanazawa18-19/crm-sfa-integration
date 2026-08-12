"""⑥ アクション履歴DB（SA-AC-xxx）。

既存の稼働中Notionワークスペースに実在するDB（database_id=d1e4a612-560e-4eb9-
8212-053f3901790a）のプロパティ構成を実データそのまま反映する。

■ titleプロパティに関する注意: 「商談回数・電話回数・メール回数（何回目）」が
title。仕様書03節が想定していた「アクション名」ではなく、この自由記述フィールド
自体がタイトルかつ実質のアクション内容記述である（表記ゆれが激しい: 【電話】N回目、
【商談】N回目、テレアポ↓（担当者名）等）。

■ 「アクション種別」に関する注意: 当初は無理に列を新設せずtitleの自由記述のまま
扱う方針だったが、kintone実データ移行（migrationパッケージ）でアクション種別
（テレアポ/訪問商談/オンライン商談等）を構造化して保持する必要が生じたため、
select型で新規追加した（2026-08-09、実データ確認済みの選択肢: テレアポ/訪問商談/
オンライン商談/メール/問い合わせメール/飛び込み/自動メール/その他）。

■ 「案件名」プロパティに関する注意: プロパティ名は"案件名"だが実体はrelation
（案件管理DBへの紐付け）であり、titleではない点に注意。

■ 「先方担当者」に関する注意: リレーションではなく自由記述テキスト。連絡先DBへの
正式なリレーションは存在しない。
"""

from __future__ import annotations

from src.db_schema.base import (
    DatabaseSchema,
    PropertyDefinition,
    PropertyType,
    RequirementLevel,
    SyncScope,
)

ACTION_SCHEMA = DatabaseSchema(
    key="action",
    display_name="アクション履歴DB",
    id_prefix="SA-AC-",
    kintone_key="アクション管理 レコード番号",
    zoho_key="アクション",
    # 2026-08-12、CustomModule2の実フィールド一覧をZoho本番APIから取得し、
    # transform_zoho_action()（src/migration/zoho_action.py）が実際に読んでいる列と
    # 突き合わせて確認済み（Name→アクション名、Owner→アクションの担当者、
    # field7→アクション種別が明確に対応）。chain.pyのCustomModule1誤りの件と同様、
    # 未検証のまま放置すると同種の事故につながるため、Zoho標準モジュール
    # （Tasks/Calls/Events等）ではなくCustomModule2が正しいアクション履歴用モジュールで
    # あることを、この確認をもって確定値として扱う。
    zoho_api_module="CustomModule2",
    spreadsheet_sheet_name="アクション管理",
    notion_database_id="d1e4a612-560e-4eb9-8212-053f3901790a",
    properties=(
        PropertyDefinition(
            name="商談回数・電話回数・メール回数（何回目）",
            property_type=PropertyType.TITLE,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description="実質のアクション内容の自由記述。表記ゆれが激しい",
        ),
        PropertyDefinition(
            name="アクション種別",
            property_type=PropertyType.SELECT,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
            description="kintone実データ移行のため2026-08-09に新規追加したselect項目",
            options=(
                "テレアポ",
                "訪問商談",
                "オンライン商談",
                "メール",
                "問い合わせメール",
                "飛び込み",
                "自動メール",
                "その他",
            ),
        ),
        PropertyDefinition(
            name="営業部アクションID",
            property_type=PropertyType.UNIQUE_ID,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
            description="SA-AC-xxx。読取専用・自動採番",
        ),
        PropertyDefinition(
            name="アクション日",
            property_type=PropertyType.DATE,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="接触回数の時系列判定に使用",
        ),
        PropertyDefinition(
            name="導入フローとスケジュール",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        PropertyDefinition(
            name="履歴メモ",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
        ),
        # 2026-08-10、金沢さんの指摘により新規作成。Zoho実データには「Notta」
        # 「録画・音声ファイル」という、いずれもNotta.ai（議事録・文字起こしサービス）の
        # URLが入った列が存在するが、Notion側に対応するプロパティが無かった。
        PropertyDefinition(
            name="議事録・録画リンク",
            property_type=PropertyType.URL,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="Notta.ai等の議事録・録画URL",
        ),
        # 2026-08-10、Zoho添付ファイル移行に際し新規作成。Zoho「手当情報アップロード」
        # （商談手当計算用の領収書等の画像、762件）を紐付けるためのファイルプロパティ。
        PropertyDefinition(
            name="手当情報アップロード",
            property_type=PropertyType.FILES,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.NOTION_ONLY,
            description="Any-to-Any同期のスコープ外（ファイル同期は非対応）",
        ),
        PropertyDefinition(
            name="先方担当者",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="リレーションではなく自由記述テキスト",
        ),
        PropertyDefinition(
            name="案件名",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="プロパティ名は案件名だが実体はrelation（案件管理DBへの紐付け）",
            relation_target="project",
        ),
        PropertyDefinition(
            name="👯‍♀️ チェーンリスト",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="チェーンDBへの紐付け（dual_property）",
            relation_target="chain",
        ),
        PropertyDefinition(
            name="👨‍👩‍👧‍👦 取引先マスター",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="取引先マスターDBへの紐付け（dual_property）",
            relation_target="client_master",
        ),
        # 2026-08-10、連絡先DB側にdual_propertyリレーションを追加した際に自動生成された
        # 逆参照プロパティ。
        PropertyDefinition(
            name="連絡先",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.ALL_TOOLS,
            description="連絡先DBからのdual_property逆参照",
            relation_target="contact",
        ),
        # --- 以下、読み取り専用（rollup/created_time/created_by） ---
        PropertyDefinition(
            name="決済者",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="担当営業",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="案件 担当者名",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="提案サービス",
            property_type=PropertyType.ROLLUP,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
        PropertyDefinition(
            name="営業ステータス",
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
            name="作成者",
            property_type=PropertyType.CREATED_BY,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
        ),
    ),
)
