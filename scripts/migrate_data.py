#!/usr/bin/env python3
"""kintone / Zoho / CSV 既存データのクレンジングと一括インポート（09_開発ロードマップ T-11）。

04_項目マッピング末尾の移行手順を実装する:
  ①旧プロパティの取捨選別 → ②新DBプロパティ定義 → ③外部ID（kintone_ID/Zoho_ID）を
  キーにした一括インポート → ④リレーションの自動結合 → ⑤名寄せ結果の目視検証。

実データはkintoneの各アプリ（取引先マスタ／案件管理／アクション管理）からエクスポートした
CSVを入力とする（kintone APIキーが未取得の現状、CSV入力が唯一の現実的な入力経路のため。
将来kintone APIから直接取得する経路を追加する場合は、read_csv_rows() が返す
`list[dict[str, str]]` 形式を維持したまま呼び出し元をAPI取得に差し替えれば良い）。

使い方:
    # 実データが無くても構造・リレーション解決結果を確認できる
    python scripts/migrate_data.py --client-master-csv ... --project-csv ... \\
        --action-csv ... --dry-run

    # 実際にNotionへ作成する（事前に scripts/setup_notion_databases.py でDB作成済みであること）
    python scripts/migrate_data.py --client-master-csv ... --project-csv ... \\
        --action-csv ...

実行には環境変数 NOTION_API_KEY が必要（--dry-run 時は不要）。

■ 出力ファイルの取り扱い注意（BLOCKER6）: IDマッピングDB・各種レポートCSVには氏名・部署・
役職・携帯番号・メールアドレス等のPII（個人情報）が含まれる。デフォルト出力先は
リポジトリ直下の `migration_output/`（.gitignore登録済み）とし、誤ってコミットされない
ようにしている。`--id-mapping-db` / `--report-path` でリポジトリ外へ出力先を変更する場合は
取り扱いに注意すること。
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db_schema.registry import ALL_SCHEMAS, SCHEMAS_BY_KEY
from src.migration.migration_pipeline import (
    MigrationPlan,
    MigrationSummary,
    materialize,
    plan_migration,
    print_summary,
    write_dedupe_report_csv,
    write_unresolved_report_csv,
    write_unresolved_user_report_csv,
)
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.id_mapping import SQLiteIdMappingStore

logger = logging.getLogger(__name__)

# PIIを含むファイル（IDマッピングDB・各種レポートCSV）のデフォルト出力先。誤ってコミット
# されないよう、専用ディレクトリへ隔離した上で.gitignoreへ登録している（BLOCKER6）。
_MIGRATION_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "migration_output"
_DEFAULT_ID_MAPPING_DB_PATH = _MIGRATION_OUTPUT_DIR / "migration_id_mapping.db"
_DEFAULT_REPORT_PATH = _MIGRATION_OUTPUT_DIR / "migration_report.csv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client-master-csv", type=Path, required=True, help="kintone取引先マスタのエクスポートCSV"
    )
    parser.add_argument(
        "--project-csv", type=Path, required=True, help="kintone案件管理のエクスポートCSV"
    )
    parser.add_argument(
        "--action-csv", type=Path, required=True, help="kintoneアクション管理のエクスポートCSV"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Notion API・IDマッピングストアへ書き込まず、作成予定件数・名寄せ結果を表示するだけ",
    )
    parser.add_argument(
        "--id-mapping-db",
        type=Path,
        default=_DEFAULT_ID_MAPPING_DB_PATH,
        help="IDマッピングストア（SQLite）のファイルパス（PIIを含むためデフォルトはgitignore対象）",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=_DEFAULT_REPORT_PATH,
        help="名寄せ結果レポートCSVの出力先（PIIを含むためデフォルトはgitignore対象）",
    )
    return parser.parse_args(argv)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """kintoneエクスポートCSVを読み込む。

    kintone/ExcelのCSV出力はUTF-8 BOM付きになることが多いため utf-8-sig で読む
    （BOM無しCSVでも問題なく読める）。
    """
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_db_ids() -> dict[str, str]:
    """DBスキーマ定義（src.db_schema.registry.ALL_SCHEMAS）から db_key -> notion database_id
    を直接組み立てる。以前は scripts/.notion_db_ids.json キャッシュファイルを読み込んでいたが、
    全6DBが既に DatabaseSchema.notion_database_id を保持しているためキャッシュは不要になった
    （shirokuma-secレビュー: WARN）。notion_database_id が未設定のスキーマは除外する。
    """
    return {
        schema.key: schema.notion_database_id
        for schema in ALL_SCHEMAS
        if schema.notion_database_id is not None
    }


def build_notion_clients(db_ids: dict[str, str]) -> dict[str, HttpNotionClient]:
    missing = [key for key in SCHEMAS_BY_KEY if key not in db_ids]
    if missing:
        raise RuntimeError(
            f"Notion database_id が未登録のDBがあります: {missing}。"
            " 先に scripts/setup_notion_databases.py を実行するか、--dry-run で確認してください。"
        )
    return {key: HttpNotionClient(key, db_ids[key]) for key in SCHEMAS_BY_KEY}


def _related_report_path(report_path: Path, suffix: str) -> Path:
    """--report-path を基準に、未解決系レポートの出力パスを組み立てる
    （例: migration_report.csv -> migration_report_unresolved.csv）。"""
    return report_path.with_name(f"{report_path.stem}{suffix}{report_path.suffix}")


def write_reports(plan: MigrationPlan, report_path: Path) -> tuple[Path, Path]:
    """名寄せ・未解決リレーション・USER型未設定の各レポートCSVを書き出す。

    materialize()が例外で中断した場合でも呼び出せるよう、plan単体から書き出せる
    処理としてまとめている（BLOCKER8）。
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)
    unresolved_path = _related_report_path(report_path, "_unresolved")
    unresolved_user_path = _related_report_path(report_path, "_unresolved_users")
    write_dedupe_report_csv(plan.dedupe_report, report_path)
    write_unresolved_report_csv(plan.unresolved, unresolved_path)
    write_unresolved_user_report_csv(plan.unresolved_user_properties, unresolved_user_path)
    return unresolved_path, unresolved_user_path


def _partial_summary_from_plan(plan: MigrationPlan) -> MigrationSummary:
    """materialize()が例外で中断した場合、record.notion_keyが設定済みかどうかから
    ベストエフォートで進捗サマリーを組み立てる（BLOCKER8）。

    どこまで進んだかの目視確認が目的のため、既存スキップとの内訳は区別せず
    全て「作成」側へ計上する（厳密な内訳は次回実行時のIDマッピングストアで再確認できる）。
    """
    created = {db_key: sum(1 for r in records if r.notion_key is not None) for db_key, records in plan.prepared.items()}
    skipped_existing = {db_key: 0 for db_key in plan.prepared}
    return MigrationSummary(created=created, skipped_existing=skipped_existing)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)

    client_master_rows = read_csv_rows(args.client_master_csv)
    project_rows = read_csv_rows(args.project_csv)
    action_rows = read_csv_rows(args.action_csv)

    plan = plan_migration(client_master_rows, project_rows, action_rows)

    id_mapping_store = None
    notion_clients = None
    if not args.dry_run:
        args.id_mapping_db.parent.mkdir(parents=True, exist_ok=True)
        id_mapping_store = SQLiteIdMappingStore(str(args.id_mapping_db))
        notion_clients = build_notion_clients(load_db_ids())

    try:
        summary = materialize(
            plan,
            id_mapping_store=id_mapping_store,
            notion_clients=notion_clients,
            dry_run=args.dry_run,
        )
    except Exception:
        # materialize()が例外で中断しても、途中経過のサマリー・レポートを出力してから
        # 例外を再送出する（BLOCKER8: 失敗時に何の手掛かりも残らない事態を避ける）。
        logger.exception(
            "materialize() が例外で中断しました。ここまでの進捗でサマリー・レポートを出力します。"
        )
        print_summary(plan, _partial_summary_from_plan(plan), dry_run=args.dry_run)
        write_reports(plan, args.report_path)
        raise
    finally:
        if id_mapping_store is not None:
            id_mapping_store.close()

    print_summary(plan, summary, dry_run=args.dry_run)
    unresolved_path, unresolved_user_path = write_reports(plan, args.report_path)
    print(f"\n名寄せレポートを出力しました: {args.report_path}")
    print(f"未解決リレーションレポートを出力しました: {unresolved_path}")
    print(f"担当者未設定レポートを出力しました: {unresolved_user_path}")


if __name__ == "__main__":
    main()
