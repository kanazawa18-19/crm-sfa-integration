"""scripts/migrate_data.py・src/migration/migration_pipeline.py の統合テスト。

tests/migration/fixtures/ の小さなkintoneエクスポートCSV（数件）を入力に、
①取引先マスター→②チェーン→③連絡先→④案件管理/⑤サービス・商品→⑥アクション管理の
順でリレーションが解決されること、dry-run/冪等性/未解決時継続/名寄せレポートを検証する。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts.migrate_data import (
    _DEFAULT_ID_MAPPING_DB_PATH,
    _DEFAULT_REPORT_PATH,
    build_notion_clients,
    load_db_ids,
    main,
    read_csv_rows,
)
from src.db_schema.base import Tool
from src.db_schema.registry import SCHEMAS_BY_KEY
from src.migration.migration_pipeline import (
    MigrationPlan,
    MigrationSummary,
    PreparedRecord,
    UnresolvedRelation,
    materialize,
    plan_migration,
    print_summary,
    resolved_properties,
    write_dedupe_report_csv,
    write_unresolved_report_csv,
    write_unresolved_user_report_csv,
)
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class FakeNotionClient:
    """create_pageの呼び出しを記録し、DB別に連番のpage_idを返すテスト用ダブル。"""

    def __init__(self, db_key: str) -> None:
        self.db_key = db_key
        self.created: list[dict[str, Any]] = []
        self._counter = 0

    def create_page(self, properties: dict[str, Any]) -> str:
        self._counter += 1
        self.created.append(properties)
        return f"{self.db_key}-page-{self._counter}"


def fake_notion_clients() -> dict[str, FakeNotionClient]:
    return {key: FakeNotionClient(key) for key in SCHEMAS_BY_KEY}


@pytest.fixture
def plan() -> MigrationPlan:
    client_master_rows = read_csv_rows(FIXTURES_DIR / "client_master.csv")
    project_rows = read_csv_rows(FIXTURES_DIR / "project.csv")
    action_rows = read_csv_rows(FIXTURES_DIR / "action.csv")
    return plan_migration(client_master_rows, project_rows, action_rows)


def _find(records: list[PreparedRecord], kintone_id: str) -> PreparedRecord:
    return next(r for r in records if r.kintone_id == kintone_id)


# --- ①→②→③→④→⑤→⑥ のリレーション解決 --------------------------------------


def test_client_and_chain_relation_resolved(plan: MigrationPlan) -> None:
    client = _find(plan.prepared["client_master"], "1001")
    chain = plan.prepared["chain"][0]

    assert chain.properties["チェーン名"] == "サンプルチェーン本部"
    assert client.properties["チェーン"] == [chain]


def test_duplicate_client_name_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """WARN10: fixtureの1001/1002は同名「株式会社サンプル」。名寄せ対象外だが、無言だと
    実データ調査が困難なため重複を検知したら警告ログを残す。"""
    with caplog.at_level(logging.WARNING):
        client_master_rows = read_csv_rows(FIXTURES_DIR / "client_master.csv")
        plan_migration(client_master_rows, [], [])

    assert any("duplicate" in r.message and "株式会社サンプル" in r.message for r in caplog.records)


def test_project_client_and_service_relations_resolved(plan: MigrationPlan) -> None:
    client = _find(plan.prepared["client_master"], "1001")
    project = _find(plan.prepared["project"], "3001")

    assert project.properties["取引先マスター"] == [client]
    service_names = {p.properties["サービス名"] for p in project.properties["提案サービス"]}
    assert service_names == {"リピッテ", "メイリー"}
    # ショット起点のサービスのため課金形態はイニシャルスポットで暫定登録される。
    assert all(p.properties["課金形態"] == "イニシャルスポット" for p in project.properties["提案サービス"])


def test_client_sales_status_derived_from_linked_project_status(plan: MigrationPlan) -> None:
    """BLOCKER1: 取引先マスターの営業ステータス（必須）は、紐づく案件管理の営業ステータスから
    導出される。1001は3001(契約済)に紐づくため「契約」、案件が無い1003は「未アプローチ」。
    """
    contracted_client = _find(plan.prepared["client_master"], "1001")
    no_project_client = _find(plan.prepared["client_master"], "1003")

    assert contracted_client.properties["営業ステータス"] == "契約"
    assert no_project_client.properties["営業ステータス"] == "未アプローチ"


def test_action_relations_resolved_against_client_project_contact(plan: MigrationPlan) -> None:
    client = _find(plan.prepared["client_master"], "1001")
    project = _find(plan.prepared["project"], "3001")
    action = _find(plan.prepared["action"], "4001")

    assert action.properties["取引先マスター"] == [client]
    assert action.properties["案件管理"] == [project]
    assert [c.properties["氏名"] for c in action.properties["先方担当者"]] == ["山田太郎"]


def test_resolved_properties_omits_user_type_properties(plan: MigrationPlan) -> None:
    """BLOCKER3: 担当営業（action, USER型必須）・担当メンバー（project, USER型必須）は
    氏名→NotionユーザーIDの対応表が無いため解決できず、キー自体が欠落した状態で
    create_page が呼ばれる現状挙動を固定化する回帰テスト。"""
    action_record = _find(plan.prepared["action"], "4001")
    project_record = _find(plan.prepared["project"], "3001")

    action_resolved = resolved_properties(action_record)
    project_resolved = resolved_properties(project_record)

    assert "担当営業" not in action_resolved
    assert "担当メンバー" not in project_resolved

    # 未設定になった件数・対象レコードはレポート側で可視化される（BLOCKER4/5）。
    assert any(
        u.db_key == "action" and u.property_name == "担当営業" and u.kintone_id == "4001"
        for u in plan.unresolved_user_properties
    )
    assert any(
        u.db_key == "project" and u.property_name == "担当メンバー" and u.kintone_id == "3001"
        for u in plan.unresolved_user_properties
    )


def test_action_contact_cross_company_fallback_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WARN7: (氏名, 取引先名) で見つからず氏名のみで別取引先の連絡先へフォールバック
    解決した場合、誤結合の可能性があるため警告ログを残す。"""
    client_master_rows = [
        {
            "レコード番号": "2001",
            "顧客名（法人・個人・施設）": "株式会社A",
            "担当者名1": "田中一郎",
        },
        {"レコード番号": "2002", "顧客名（法人・個人・施設）": "株式会社B"},
    ]
    action_rows = [
        {
            "レコード番号": "5001",
            "アクション内容": "テレアポ",
            "対応者": "営業太郎",
            "担当者名": "田中一郎",
            "施設名（会社名）": "株式会社B",
        }
    ]

    with caplog.at_level(logging.WARNING):
        plan = plan_migration(client_master_rows, [], action_rows)

    action = _find(plan.prepared["action"], "5001")
    assert [c.properties["氏名"] for c in action.properties["先方担当者"]] == ["田中一郎"]
    assert any("フォールバック解決" in r.message for r in caplog.records)


def test_action_next_action_date_reflected_on_project(plan: MigrationPlan) -> None:
    project = _find(plan.prepared["project"], "3001")

    assert project.properties["次回アクション日"] == "2026-08-20"


def test_action_proposed_service_registers_product_without_duplicating(plan: MigrationPlan) -> None:
    """アクション管理の「提案サービス」はサービス・商品DBへの登録のみ行い、
    既に案件管理側で登録済みの名前は重複登録しない。"""
    product_names = [p.properties["サービス名"] for p in plan.prepared["product"]]

    assert product_names == ["リピッテ", "メイリー"]


# --- ④ リレーション未解決時も処理を継続する --------------------------------------


def test_unresolved_relations_do_not_stop_processing_and_are_logged(
    plan: MigrationPlan, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        # fixtureのplanは既に構築済みだが、警告ログの再現のため同一入力で再実行する。
        client_master_rows = read_csv_rows(FIXTURES_DIR / "client_master.csv")
        project_rows = read_csv_rows(FIXTURES_DIR / "project.csv")
        action_rows = read_csv_rows(FIXTURES_DIR / "action.csv")
        plan_migration(client_master_rows, project_rows, action_rows)

    unresolved_relations = {(u.db_key, u.relation_name, u.raw_value) for u in plan.unresolved}
    assert ("project", "取引先マスター", "存在しない取引先") in unresolved_relations
    assert ("action", "案件管理", "9999") in unresolved_relations
    assert ("action", "先方担当者", "不明太郎") in unresolved_relations

    # 未解決でもレコード自体は作成対象として残り、リレーションのみ空になる。
    unresolved_project = _find(plan.prepared["project"], "3002")
    assert unresolved_project.properties["取引先マスター"] == []

    assert any("存在しない取引先" in record.message for record in caplog.records)


def test_transform_error_is_skipped_with_warning_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    """営業ステータス等の不正値によるValueErrorも、1件のデータ不整合で全体を止めない。"""
    bad_project_rows = [
        {
            "レコード番号": "9001",
            "施設名（会社名）": "株式会社サンプル",
            "契約進捗状況": "謎のステータス",
        }
    ]

    with caplog.at_level(logging.WARNING):
        result = plan_migration([], bad_project_rows, [])

    assert result.prepared["project"] == []
    assert len(result.skipped_transform_errors) == 1
    assert "9001" in result.skipped_transform_errors[0]


# --- ⑤ 名寄せ結果の目視検証レポート -----------------------------------------------


def test_dedupe_report_contains_merged_contact_sources(plan: MigrationPlan) -> None:
    entries = [e for e in plan.dedupe_report if e.db_key == "contact"]
    assert len(entries) == 1

    entry = entries[0]
    assert entry.dedupe_key == "email:yamada@example.com"
    assert len(entry.sources) == 2
    assert entry.merged["氏名"] == "山田太郎"
    # 部署は1002側、役職は1001側から補完される（既存値は上書きしない）。
    assert entry.merged["部署"] == "営業部"
    assert entry.merged["役職"] == "部長"

    merged_contact = _find(plan.prepared["contact"], "contact:email:yamada@example.com")
    assert merged_contact.properties["部署"] == "営業部"
    assert merged_contact.properties["役職"] == "部長"


# --- dry-run: Notion API・IDマッピングストアへ一切書き込まない ------------------------


def test_dry_run_never_touches_notion_or_id_mapping_store(plan: MigrationPlan) -> None:
    mock_store = MagicMock()
    mock_clients = {key: MagicMock() for key in SCHEMAS_BY_KEY}

    summary = materialize(
        plan, id_mapping_store=mock_store, notion_clients=mock_clients, dry_run=True
    )

    mock_store.find_by_external_id.assert_not_called()
    mock_store.upsert.assert_not_called()
    for client in mock_clients.values():
        client.create_page.assert_not_called()

    assert summary.created["client_master"] == len(plan.prepared["client_master"])
    assert summary.created["project"] == len(plan.prepared["project"])
    assert summary.created["action"] == len(plan.prepared["action"])


def test_dry_run_via_cli_prints_summary_without_api_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    report_path = tmp_path / "migration_report.csv"

    main(
        [
            "--client-master-csv",
            str(FIXTURES_DIR / "client_master.csv"),
            "--project-csv",
            str(FIXTURES_DIR / "project.csv"),
            "--action-csv",
            str(FIXTURES_DIR / "action.csv"),
            "--dry-run",
            "--report-path",
            str(report_path),
        ]
    )

    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "取引先マスターDB" in out
    assert "名寄せ結果" in out
    assert report_path.exists()


# --- 冪等性: 既存マッピングがある場合は重複作成しない ------------------------------------


def test_materialize_skips_creation_when_id_mapping_already_exists() -> None:
    client_master_rows = read_csv_rows(FIXTURES_DIR / "client_master.csv")
    small_plan = plan_migration(client_master_rows, [], [])
    store = SQLiteIdMappingStore(":memory:")
    try:
        store.upsert(
            IdMapping(notion_key="existing-page-id", db_key="client_master", kintone_id="1001")
        )
        clients = fake_notion_clients()

        summary = materialize(small_plan, id_mapping_store=store, notion_clients=clients, dry_run=False)

        record = _find(small_plan.prepared["client_master"], "1001")
        assert record.notion_key == "existing-page-id"
        assert clients["client_master"].created == [] or all(
            "existing-page-id" not in str(payload) for payload in clients["client_master"].created
        )
        assert summary.skipped_existing["client_master"] == 1
        # 1002/1003は未登録なので新規作成される。
        assert summary.created["client_master"] == 2
    finally:
        store.close()


def test_materialize_running_twice_does_not_create_duplicates() -> None:
    """同一inputで2回materializeすると、2回目は全件スキップされる（再実行の冪等性）。"""
    client_master_rows = read_csv_rows(FIXTURES_DIR / "client_master.csv")
    store = SQLiteIdMappingStore(":memory:")
    try:
        plan_1 = plan_migration(client_master_rows, [], [])
        clients = fake_notion_clients()
        materialize(plan_1, id_mapping_store=store, notion_clients=clients, dry_run=False)
        total_created_first_run = len(clients["client_master"].created)

        plan_2 = plan_migration(client_master_rows, [], [])
        summary_2 = materialize(plan_2, id_mapping_store=store, notion_clients=clients, dry_run=False)

        assert total_created_first_run == 3
        assert summary_2.created["client_master"] == 0
        assert summary_2.skipped_existing["client_master"] == 3
        assert len(clients["client_master"].created) == total_created_first_run
    finally:
        store.close()


# --- materialize: リレーションが実際のnotion_key（page_id）へ解決されること -------------


def test_materialize_wires_real_page_ids_into_relation_properties() -> None:
    client_master_rows = read_csv_rows(FIXTURES_DIR / "client_master.csv")
    project_rows = read_csv_rows(FIXTURES_DIR / "project.csv")
    full_plan = plan_migration(client_master_rows, project_rows, [])
    store = SQLiteIdMappingStore(":memory:")
    try:
        clients = fake_notion_clients()

        materialize(full_plan, id_mapping_store=store, notion_clients=clients, dry_run=False)

        client_record = _find(full_plan.prepared["client_master"], "1001")
        project_record = _find(full_plan.prepared["project"], "3001")
        assert project_record.notion_key is not None
        assert client_record.notion_key is not None

        sent_properties = clients["project"].created[0]
        assert sent_properties["取引先マスター"] == [client_record.notion_key]
        assert "kintone_ID" not in sent_properties  # PROJECT_SCHEMAに無いプロパティは送らない
    finally:
        store.close()


def test_materialize_wires_real_page_ids_into_action_relation_properties() -> None:
    """BLOCKER2: アクション管理の取引先マスター・案件管理・先方担当者の3つのリレーションが、
    PreparedRecord参照レベルだけでなく、materialize()が実際にNotion APIへ送るペイロード
    （page_idレベル）まで正しく解決されていることを検証する。"""
    client_master_rows = read_csv_rows(FIXTURES_DIR / "client_master.csv")
    project_rows = read_csv_rows(FIXTURES_DIR / "project.csv")
    action_rows = read_csv_rows(FIXTURES_DIR / "action.csv")
    full_plan = plan_migration(client_master_rows, project_rows, action_rows)
    store = SQLiteIdMappingStore(":memory:")
    try:
        clients = fake_notion_clients()

        materialize(full_plan, id_mapping_store=store, notion_clients=clients, dry_run=False)

        client_record = _find(full_plan.prepared["client_master"], "1001")
        project_record = _find(full_plan.prepared["project"], "3001")
        contact_record = _find(full_plan.prepared["contact"], "contact:email:yamada@example.com")
        action_record = _find(full_plan.prepared["action"], "4001")

        for record in (client_record, project_record, contact_record, action_record):
            assert record.notion_key is not None

        sent_properties = next(
            payload
            for payload in clients["action"].created
            if payload["kintone_Act_ID"] == "4001"
        )
        assert sent_properties["取引先マスター"] == [client_record.notion_key]
        assert sent_properties["案件管理"] == [project_record.notion_key]
        assert sent_properties["先方担当者"] == [contact_record.notion_key]
    finally:
        store.close()


def test_resolved_properties_converts_prepared_record_refs_to_notion_keys() -> None:
    target = PreparedRecord(db_key="client_master", kintone_id="1", properties={}, notion_key="page-abc")
    record = PreparedRecord(
        db_key="project",
        kintone_id="2",
        properties={"取引先マスター": [target], "案件名": "テスト案件"},
    )

    resolved = resolved_properties(record)

    assert resolved == {"取引先マスター": ["page-abc"], "案件名": "テスト案件"}


# --- load_db_ids: shirokuma-secレビューWARN対応（.notion_db_ids.jsonキャッシュ廃止）--------


def test_load_db_ids_resolves_all_schemas_from_registry_without_cache_file() -> None:
    """.notion_db_ids.jsonキャッシュファイルを読まず、ALL_SCHEMASのnotion_database_idから
    直接db_key -> database_idの対応表を組み立てられることを検証する。"""
    db_ids = load_db_ids()

    assert set(db_ids.keys()) == set(SCHEMAS_BY_KEY.keys())
    for key, schema in SCHEMAS_BY_KEY.items():
        assert db_ids[key] == schema.notion_database_id

    # 全DBのnotion_database_idが設定済みのため、build_notion_clients()もそのままDB不足エラー
    # にならずに通る（NOTION_API_KEY未設定時のHttpNotionClient初期化エラーはここでの
    # 検証対象外のため、build_notion_clients()内でmissingになっていないことのみ確認する）。
    missing = [key for key in SCHEMAS_BY_KEY if key not in db_ids]
    assert missing == []


# --- build_notion_clients: DB未作成時のエラー -----------------------------------------


def test_build_notion_clients_raises_when_database_id_missing() -> None:
    with pytest.raises(RuntimeError):
        build_notion_clients({})


# --- BLOCKER4/5: 未解決リレーション・USER型未設定の可視化 ------------------------------


def test_write_unresolved_report_csv_contains_all_entries(
    plan: MigrationPlan, tmp_path: Path
) -> None:
    path = tmp_path / "unresolved.csv"

    write_unresolved_report_csv(plan.unresolved, path)

    content = path.read_text(encoding="utf-8-sig")
    assert "db_key,kintone_id,relation_name,raw_value" in content
    assert "存在しない取引先" in content
    assert "9999" in content
    assert "不明太郎" in content
    assert content.count("\n") - 1 == len(plan.unresolved)


def test_write_unresolved_user_report_csv_contains_action_and_project_entries(
    plan: MigrationPlan, tmp_path: Path
) -> None:
    path = tmp_path / "unresolved_users.csv"

    write_unresolved_user_report_csv(plan.unresolved_user_properties, path)

    content = path.read_text(encoding="utf-8-sig")
    assert "db_key,kintone_id,property_name,raw_value" in content
    assert "action,4001,担当営業,営業太郎" in content
    assert "project,3001,担当メンバー," in content


def test_print_summary_warns_when_unresolved_rate_exceeds_threshold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BLOCKER4/5: 未解決率がしきい値を超えるリレーションがあれば、print_summaryの
    先頭付近で目立つ警告を出す（全件未解決のまま気づかれないリスクへの対応）。"""
    client_master_rows = [{"レコード番号": "9001", "顧客名（法人・個人・施設）": "実在しない取引先"}]
    project_rows = [
        {
            "レコード番号": str(3000 + i),
            "施設名（会社名）": f"存在しない取引先{i}",
            "契約進捗状況": "初回接触",
        }
        for i in range(5)
    ]
    plan = plan_migration(client_master_rows, project_rows, [])
    summary = materialize(plan, id_mapping_store=None, notion_clients=None, dry_run=True)

    print_summary(plan, summary, dry_run=True)

    out = capsys.readouterr().out
    assert "警告" in out
    assert "project" in out
    assert "取引先マスター" in out


def _plan_with_unresolved_rate(unresolved_count: int, attempts: int) -> MigrationPlan:
    """print_summaryのしきい値判定（rate = unresolved_count/attempts）を境界値単位で
    検証するため、CSV由来のリレーション解決を経由せずMigrationPlanを直接組み立てる。"""
    unresolved = [
        UnresolvedRelation(
            db_key="project", kintone_id=str(i), relation_name="取引先マスター", raw_value="不明"
        )
        for i in range(unresolved_count)
    ]
    return MigrationPlan(
        prepared={"project": []},
        unresolved=unresolved,
        relation_attempts={("project", "取引先マスター"): attempts},
    )


def test_print_summary_does_not_warn_when_unresolved_rate_equals_threshold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """境界値: 未解決率がちょうどしきい値（30%）の場合、`>`（厳密な超過）判定のため
    警告は出ない。"""
    plan = _plan_with_unresolved_rate(unresolved_count=30, attempts=100)
    summary = MigrationSummary(
        created={key: 0 for key in SCHEMAS_BY_KEY}, skipped_existing={key: 0 for key in SCHEMAS_BY_KEY}
    )

    print_summary(plan, summary, dry_run=True)

    out = capsys.readouterr().out
    assert "警告" not in out


def test_print_summary_does_not_warn_when_unresolved_rate_is_29_percent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """境界値: しきい値未満（29%）では警告が出ない。"""
    plan = _plan_with_unresolved_rate(unresolved_count=29, attempts=100)
    summary = MigrationSummary(
        created={key: 0 for key in SCHEMAS_BY_KEY}, skipped_existing={key: 0 for key in SCHEMAS_BY_KEY}
    )

    print_summary(plan, summary, dry_run=True)

    out = capsys.readouterr().out
    assert "警告" not in out


def test_print_summary_warns_when_unresolved_rate_is_31_percent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """境界値: しきい値を超える（31%）場合は警告が出る。"""
    plan = _plan_with_unresolved_rate(unresolved_count=31, attempts=100)
    summary = MigrationSummary(
        created={key: 0 for key in SCHEMAS_BY_KEY}, skipped_existing={key: 0 for key in SCHEMAS_BY_KEY}
    )

    print_summary(plan, summary, dry_run=True)

    out = capsys.readouterr().out
    assert "警告" in out


def test_print_summary_does_not_warn_for_typical_low_unresolved_rate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """しきい値未満（0%〜30%未満）の一般的なケースでは警告が出ないことを確認する。"""
    plan = _plan_with_unresolved_rate(unresolved_count=10, attempts=100)
    summary = MigrationSummary(
        created={key: 0 for key in SCHEMAS_BY_KEY}, skipped_existing={key: 0 for key in SCHEMAS_BY_KEY}
    )

    print_summary(plan, summary, dry_run=True)

    out = capsys.readouterr().out
    assert "警告" not in out


def test_print_summary_lists_summary_before_raw_dedupe_details(
    plan: MigrationPlan, capsys: pytest.CaptureFixture[str]
) -> None:
    """WARN6: 未解決・USER型未設定の集計サマリーが、名寄せの生データより先に表示される。"""
    summary = materialize(plan, id_mapping_store=None, notion_clients=None, dry_run=True)

    print_summary(plan, summary, dry_run=True)

    out = capsys.readouterr().out
    assert out.index("リレーション未解決サマリー") < out.index("名寄せ結果")
    assert out.index("USER型未設定サマリー") < out.index("名寄せ結果")


# --- WARN9: 名寄せレポートCSVの可読性 --------------------------------------------------


def test_write_dedupe_report_csv_uses_human_readable_key_value_format(
    plan: MigrationPlan, tmp_path: Path
) -> None:
    path = tmp_path / "dedupe.csv"

    write_dedupe_report_csv(plan.dedupe_report, path)

    content = path.read_text(encoding="utf-8-sig")
    assert "部署=営業部" in content
    assert "役職=部長" in content
    # 生JSON形式（波括弧+ダブルクォートキー）ではないことを確認する。
    assert '"氏名"' not in content


# --- BLOCKER6: PIIを含む出力先のデフォルトパス ------------------------------------------


def test_default_output_paths_are_under_gitignored_migration_output_dir() -> None:
    assert _DEFAULT_ID_MAPPING_DB_PATH.parent.name == "migration_output"
    assert _DEFAULT_REPORT_PATH.parent.name == "migration_output"

    gitignore_path = Path(__file__).resolve().parents[2] / ".gitignore"
    assert "migration_output/" in gitignore_path.read_text(encoding="utf-8")


# --- BLOCKER8: materialize()が例外で中断しても途中経過のレポートを出力する -------------------


def test_main_outputs_reports_even_when_materialize_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    report_path = tmp_path / "migration_report.csv"
    id_mapping_db = tmp_path / "migration_id_mapping.db"

    import scripts.migrate_data as migrate_data_module

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Notion API boom")

    monkeypatch.setattr(migrate_data_module, "materialize", _boom)
    monkeypatch.setattr(
        migrate_data_module, "build_notion_clients", lambda db_ids: {key: MagicMock() for key in SCHEMAS_BY_KEY}
    )
    monkeypatch.setattr(migrate_data_module, "load_db_ids", lambda: {key: "db-id" for key in SCHEMAS_BY_KEY})

    with pytest.raises(RuntimeError, match="Notion API boom"):
        migrate_data_module.main(
            [
                "--client-master-csv",
                str(FIXTURES_DIR / "client_master.csv"),
                "--project-csv",
                str(FIXTURES_DIR / "project.csv"),
                "--action-csv",
                str(FIXTURES_DIR / "action.csv"),
                "--report-path",
                str(report_path),
                "--id-mapping-db",
                str(id_mapping_db),
            ]
        )

    out = capsys.readouterr().out
    assert "移行結果サマリー" in out
    assert report_path.exists()
    assert (tmp_path / "migration_report_unresolved.csv").exists()
    assert (tmp_path / "migration_report_unresolved_users.csv").exists()
