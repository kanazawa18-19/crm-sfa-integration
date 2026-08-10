"""kintone/Zoho両ソースを1回のplan_migration()呼び出しで扱う統合ロジックのテスト。

2026-08-10、金沢さん方針「kintoneもZohoも一気に、確実性重視・データ欠損より重複の方が
マシ」により、①取引先マスターは既存Notion+同一実行内の他ソースとの重複を防ぐよう
plan_migration()を拡張した。本ファイルはその新規ロジック（_resolve_or_create_client等）
を検証する。連絡先・案件・アクション・サービス・商品・チェーンはZoho側を「常に新規作成」
とする方針（既存Notionとの厳密な突合は行わない）のため、その動作も併せて検証する。
"""

from __future__ import annotations

from src.db_schema.registry import SCHEMAS_BY_KEY
from src.migration.migration_pipeline import PreparedRecord, plan_migration
from src.migration.notion_dedupe import ClientMasterSnapshot, build_client_match_index


def _find(records: list[PreparedRecord], kintone_id: str) -> PreparedRecord:
    return next(r for r in records if r.kintone_id == kintone_id)


# --- ①取引先マスター: kintone/Zoho間の重複作成防止 --------------------------------------


def test_kintone_and_zoho_same_company_name_creates_only_one_client_record() -> None:
    """同じ会社名がkintone・Zoho双方のCSVに存在する場合、Notion側には1件しか作成しない
    （先に処理したkintone側の1件を、Zoho側が再利用する）。"""
    kintone_rows = [{"レコード番号": "1001", "顧客名（法人・個人・施設）": "株式会社サンプル"}]
    zoho_rows = [{"データID": "zcrm_1", "取引先名": "株式会社サンプル"}]

    plan = plan_migration(kintone_rows, [], [], zoho_client_master_rows=zoho_rows)

    assert len(plan.prepared["client_master"]) == 1
    assert plan.prepared["client_master"][0].kintone_id == "1001"


def test_zoho_only_duplicate_names_within_csv_are_deduped() -> None:
    """Zoho側は同一CSV内の同名重複も①の共有レジストリで名寄せする
    （kintoneと異なりZohoは新規統合のため、同一ソース内でも重複させない）。"""
    zoho_rows = [
        {"データID": "zcrm_1", "取引先名": "株式会社サンプル"},
        {"データID": "zcrm_2", "取引先名": "株式会社サンプル"},
    ]

    plan = plan_migration([], [], [], zoho_client_master_rows=zoho_rows)

    assert len(plan.prepared["client_master"]) == 1
    assert plan.prepared["client_master"][0].kintone_id == "zcrm_1"


def test_kintone_duplicate_names_within_csv_still_creates_separate_records() -> None:
    """kintone側は既存動作（同一CSV内の同名重複は最初の行のみをリレーション解決の正とし、
    各行はそのまま個別に新規作成する）を維持する（今回のkintone/Zoho統合では変更しない）。"""
    kintone_rows = [
        {"レコード番号": "1001", "顧客名（法人・個人・施設）": "株式会社サンプル"},
        {"レコード番号": "1002", "顧客名（法人・個人・施設）": "株式会社サンプル"},
    ]

    plan = plan_migration(kintone_rows, [], [])

    assert len(plan.prepared["client_master"]) == 2


def test_zoho_client_matches_existing_notion_and_does_not_create_new_page() -> None:
    """既存Notion取引先マスターと会社名が一致する場合、新規作成せず既存ページのidを
    そのままnotion_keyとして持つ参照専用レコードを使う。"""
    existing_index = build_client_match_index(
        [
            ClientMasterSnapshot(
                page_id="existing-page-1",
                title="株式会社サンプル",
                postal_code=None,
                prefecture=None,
                address=None,
            )
        ]
    )
    zoho_rows = [{"データID": "zcrm_1", "取引先名": "株式会社サンプル"}]

    plan = plan_migration(
        [], [], [], existing_client_index=existing_index, zoho_client_master_rows=zoho_rows
    )

    assert plan.prepared["client_master"] == []
    assert plan.needs_review_clients == []


def test_zoho_client_postal_code_conflict_creates_new_and_flags_for_review() -> None:
    """会社名は一致するが郵便番号が食い違う場合、誤結合を避けるため新規作成しつつ
    needs_review_clientsへ記録する（金沢さん方針: データ欠損より重複の方がマシ）。"""
    existing_index = build_client_match_index(
        [
            ClientMasterSnapshot(
                page_id="existing-page-1",
                title="株式会社サンプル",
                postal_code="100-0001",
                prefecture=None,
                address=None,
            )
        ]
    )
    zoho_rows = [{"データID": "zcrm_1", "取引先名": "株式会社サンプル", "郵便番号": "530-0001"}]

    plan = plan_migration(
        [], [], [], existing_client_index=existing_index, zoho_client_master_rows=zoho_rows
    )

    assert len(plan.prepared["client_master"]) == 1
    assert len(plan.needs_review_clients) == 1
    assert plan.needs_review_clients[0].source == "zoho"
    assert plan.needs_review_clients[0].name == "株式会社サンプル"


# --- ②チェーン: kintone/Zoho間の重複作成防止 -----------------------------------------


def test_kintone_and_zoho_same_chain_name_creates_only_one_chain_record() -> None:
    kintone_rows = [
        {
            "レコード番号": "1001",
            "顧客名（法人・個人・施設）": "サンプル1号店",
            "本部名": "サンプルチェーン本部",
        }
    ]
    zoho_rows = [{"データID": "zcrm_c1", "チェーン名・グループ名": "サンプルチェーン本部"}]

    plan = plan_migration(kintone_rows, [], [], zoho_chain_rows=zoho_rows)

    assert len(plan.prepared["chain"]) == 1


# --- ③連絡先: Zoho行が①で解決済みの取引先マスターへ正しく紐付く ----------------------------


def test_zoho_contact_resolves_to_client_created_by_kintone_row() -> None:
    kintone_rows = [{"レコード番号": "1001", "顧客名（法人・個人・施設）": "株式会社サンプル"}]
    zoho_contact_rows = [
        {"データID": "zcrm_ct1", "氏名": "田中太郎", "【Eight】会社名": "株式会社サンプル"}
    ]

    plan = plan_migration(kintone_rows, [], [], zoho_contact_rows=zoho_contact_rows)

    contact = _find(plan.prepared["contact"], "zcrm_ct1")
    client = _find(plan.prepared["client_master"], "1001")
    assert contact.properties["取引先マスター"] == [client]


# --- ④案件・⑥アクション: Zohoのzoho_id/Notionページ直リンクによる取引先解決 --------------


def test_zoho_project_resolves_client_via_zoho_id_hint() -> None:
    zoho_client_rows = [{"データID": "zcrm_cl1", "取引先名": "株式会社サンプル"}]
    zoho_project_rows = [
        {"データID": "zcrm_pj1", "案件名": "サンプル導入案件", "取引先名.id": "zcrm_cl1"}
    ]

    plan = plan_migration(
        [], [], [], zoho_client_master_rows=zoho_client_rows, zoho_project_rows=zoho_project_rows
    )

    project = _find(plan.prepared["project"], "zcrm_pj1")
    client = _find(plan.prepared["client_master"], "zcrm_cl1")
    assert project.properties["取引先マスター"] == [client]


def test_zoho_action_resolves_client_via_embedded_notion_page_id() -> None:
    """アクションの「【Notion】取引先マスター」に埋め込まれた既存Notionページへの
    直リンクは、①でこの実行中に扱っていない（＝取引先マスターCSVに存在しない）会社でも
    参照専用レコードとして解決できる。"""
    zoho_action_rows = [
        {
            "データID": "zcrm_act1",
            "アクション名": "テレアポ",
            "【Notion】取引先マスター": "サンプルホテル (https://www.notion.so/5fbae3fd718f49e98eeb83aa10c880ea?pvs=21)",
        }
    ]

    plan = plan_migration([], [], [], zoho_action_rows=zoho_action_rows)

    action = _find(plan.prepared["action"], "zcrm_act1")
    client_refs = action.properties["👨‍👩‍👧‍👦 取引先マスター"]
    assert len(client_refs) == 1
    assert client_refs[0].notion_key == "5fbae3fd718f49e98eeb83aa10c880ea"


# --- ⑤サービス・商品: Zoho実データを直接登録 -----------------------------------------


def test_zoho_product_row_is_registered_with_real_cost_data() -> None:
    zoho_product_rows = [
        {"データID": "zcrm_prd1", "サービス・商品名": "リピッテホテル", "初期費用": "29800", "月額費用": "8800"}
    ]

    plan = plan_migration([], [], [], zoho_product_rows=zoho_product_rows)

    product = _find(plan.prepared["product"], "zcrm_prd1")
    assert product.properties["名前"] == "リピッテホテル"
    assert product.properties["標準初期費用"] == 29800.0


# --- 回帰: Zoho経由で作成された全レコードもNotionプロパティとして有効であること ----------------


def test_all_zoho_sourced_prepared_record_properties_are_valid_notion_properties() -> None:
    """kintone側で既存のtest_all_prepared_record_properties_are_valid_notion_properties
    （tests/migration/test_migrate_data.py）と同じ回帰チェックを、Zoho全6エンティティ分の
    最小データで検証する。"""
    plan = plan_migration(
        [],
        [],
        [],
        zoho_client_master_rows=[{"データID": "1", "取引先名": "A"}],
        zoho_contact_rows=[{"データID": "2", "氏名": "B"}],
        zoho_project_rows=[{"データID": "3", "案件名": "C"}],
        zoho_action_rows=[{"データID": "4", "アクション名": "D"}],
        zoho_product_rows=[{"データID": "5", "サービス・商品名": "E"}],
        zoho_chain_rows=[{"データID": "6", "チェーン名・グループ名": "F"}],
    )
    for db_key, records in plan.prepared.items():
        schema = SCHEMAS_BY_KEY[db_key]
        for record in records:
            for prop_name in record.properties:
                schema.get_property(prop_name)  # 存在しなければKeyErrorが送出される
