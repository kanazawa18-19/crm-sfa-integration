#!/usr/bin/env python3
"""kintone / Zoho / CSV 既存データのクレンジングと一括インポート（09_開発ロードマップ T-11）。

04_項目マッピング末尾の移行手順を実装する:
  ①旧プロパティの取捨選別 → ②新DBプロパティ定義 → ③外部ID（kintone_ID/Zoho_ID）を
  キーにした一括インポート → ④リレーションの自動結合 → ⑤名寄せ結果の目視検証。

実データはkintoneの各アプリ（取引先マスタ／案件管理／アクション管理）およびZoho CRMの
各モジュール（取引先／連絡先／案件／アクション／サービス・商品／チェーン）からエクスポート
したCSVを入力とする（kintone APIキーが未取得の現状、CSV入力が唯一の現実的な入力経路の
ため。将来APIから直接取得する経路を追加する場合は、read_csv_rows()/read_zoho_csv_rows()が
返す `list[dict[str, str]]` 形式を維持したまま呼び出し元をAPI取得に差し替えれば良い）。

kintone・Zohoいずれか一方のみ、または両方同時に指定できる（2026-08-10、金沢さん方針
「kintoneもZohoも一気に」により、両方同時指定時は1回の実行で同一会社の重複作成を防ぐ。
詳細は src/migration/migration_pipeline.py の plan_migration() docstring参照）。

使い方:
    # 実データが無くても構造・リレーション解決結果を確認できる（kintoneのみの例）
    python scripts/migrate_data.py --client-master-csv ... --project-csv ... \\
        --action-csv ... --dry-run

    # kintone・Zoho両方を1回の実行で（推奨。同一会社の重複作成を防げる）
    python scripts/migrate_data.py \\
        --client-master-csv ... --project-csv ... --action-csv ... \\
        --zoho-client-master-csv ... --zoho-contact-csv ... --zoho-project-csv ... \\
        --zoho-action-csv ... --zoho-product-csv ... --zoho-chain-csv ... \\
        --dry-run

    # Zoho側のみを単独実行する（例: kintone分は投入済みで、Zoho分だけ再実行したい場合）
    python scripts/migrate_data.py --zoho-client-master-csv ... --zoho-project-csv ... \\
        --dry-run

    # 実際にNotionへ作成する（事前に scripts/setup_notion_databases.py でDB作成済みであること）
    python scripts/migrate_data.py --client-master-csv ... --project-csv ... \\
        --action-csv ...

実行には環境変数 NOTION_API_KEY が必要（--dry-run時は本番書き込みには不要だが、既存Notion
取引先マスターとの名寄せ突合プレビューのため、設定されていれば--dry-run時も読み取り専用で
使う。--no-existing-client-matchでこの突合自体を無効化できる）。

■ 出力ファイルの取り扱い注意（BLOCKER6）: IDマッピングDB・各種レポートCSVには氏名・部署・
役職・携帯番号・メールアドレス等のPII（個人情報）が含まれる。デフォルト出力先は
リポジトリ直下の `migration_output/`（.gitignore登録済み）とし、誤ってコミットされない
ようにしている。`--id-mapping-db` / `--report-path` でリポジトリ外へ出力先を変更する場合は
取り扱いに注意すること。
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db_schema.registry import ALL_SCHEMAS, SCHEMAS_BY_KEY
from src.migration.kintone_client_master import remap_duplicate_contact_columns
from src.migration.migration_pipeline import (
    MigrationPlan,
    MigrationSummary,
    materialize,
    plan_migration,
    print_summary,
    write_dedupe_report_csv,
    write_needs_review_clients_report_csv,
    write_unresolved_report_csv,
    write_unresolved_user_report_csv,
)
from src.migration.notion_dedupe import (
    ClientMatchIndex,
    build_client_match_index,
    fetch_client_master_snapshots,
)
from src.sync_engine.clients._http import ApiError
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
        "--client-master-csv", type=Path, default=None, help="kintone取引先マスタのエクスポートCSV"
    )
    parser.add_argument(
        "--project-csv", type=Path, default=None, help="kintone案件管理のエクスポートCSV"
    )
    parser.add_argument(
        "--action-csv", type=Path, default=None, help="kintoneアクション管理のエクスポートCSV"
    )
    parser.add_argument(
        "--zoho-client-master-csv", type=Path, default=None, help="Zoho「取引先」のエクスポートCSV"
    )
    parser.add_argument(
        "--zoho-contact-csv", type=Path, default=None, help="Zoho「連絡先」のエクスポートCSV"
    )
    parser.add_argument(
        "--zoho-project-csv", type=Path, default=None, help="Zoho「案件」のエクスポートCSV"
    )
    parser.add_argument(
        "--zoho-action-csv", type=Path, default=None, help="Zoho「アクション」のエクスポートCSV"
    )
    parser.add_argument(
        "--zoho-product-csv", type=Path, default=None, help="Zoho「サービス・商品」のエクスポートCSV"
    )
    parser.add_argument(
        "--zoho-chain-csv", type=Path, default=None, help="Zoho「チェーン」のエクスポートCSV"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Notion API・IDマッピングストアへ書き込まず、作成予定件数・名寄せ結果を表示するだけ",
    )
    parser.add_argument(
        "--no-existing-client-match",
        action="store_true",
        help=(
            "既存Notion取引先マスターとの名寄せ突合（NOTION_API_KEYでの読み取りAPI呼び出しが"
            "発生する）を行わず、常に新規作成する。指定しない場合、NOTION_API_KEYが設定されて"
            "いれば--dry-run時も含めて自動的に突合する（プレビュー精度を上げるための読み取り"
            "専用アクセスのため、dry-runでも安全に実行できる）"
        ),
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
    args = parser.parse_args(argv)

    kintone_any = args.client_master_csv or args.project_csv or args.action_csv
    zoho_any = (
        args.zoho_client_master_csv
        or args.zoho_contact_csv
        or args.zoho_project_csv
        or args.zoho_action_csv
        or args.zoho_product_csv
        or args.zoho_chain_csv
    )
    if not kintone_any and not zoho_any:
        parser.error(
            "kintone側（--client-master-csv等）またはZoho側（--zoho-client-master-csv等）の"
            "CSVを少なくとも1つ指定してください"
        )
    return args


_CSV_ENCODING_CANDIDATES = ("utf-8-sig", "cp932")


def _decode_csv_text(path: Path) -> str:
    """kintoneエクスポートCSVをデコードする。

    kintoneのCSVエクスポートは文字コードを選べる仕様で、UTF-8（BOM付き）とShift-JIS
    （実データではcp932として読めるものが確認できた）のどちらもあり得るため、
    utf-8-sigを試し、デコードできなければcp932にフォールバックする（実データ確認済み:
    kintone標準エクスポートのデフォルトはcp932だった）。
    """
    raw_bytes = path.read_bytes()
    last_error: UnicodeDecodeError | None = None
    for encoding in _CSV_ENCODING_CANDIDATES:
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    assert last_error is not None
    raise last_error


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """kintoneエクスポートCSVを読み込む（通常版、列名の重複が無い前提）。"""
    text = _decode_csv_text(path)
    return list(csv.DictReader(io.StringIO(text, newline="")))


def read_client_master_csv_rows(path: Path) -> list[dict[str, str]]:
    """取引先マスタCSVを読み込む。

    担当者情報（担当者名・部署・役職・携帯番号・メールアドレス）が担当者1〜3人分、
    同名列としてkintoneから重複エクスポートされる仕様のため、通常のcsv.DictReaderでは
    読めない（重複列名は最後の値のみが残り、1・2人目の情報が失われる）。
    `kintone_client_master.remap_duplicate_contact_columns`で列インデックスに基づき
    一意なキーへ変換してから読み込む。
    """
    text = _decode_csv_text(path)
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader)
    return [remap_duplicate_contact_columns(header, row) for row in reader]


def read_zoho_csv_rows(path: Path) -> list[dict[str, str]]:
    """Zoho CRMのエクスポートCSVを読み込む。

    Zohoのエクスポートは実データ確認済みでUTF-8（BOM無し）のため、kintone側のような
    cp932フォールバックは不要（kintone用の_decode_csv_text()とは意図的に別関数にしている）。
    列の重複（kintone取引先マスタのような担当者1〜3人分の重複列）も無いため、
    通常のcsv.DictReaderで読める。
    """
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_existing_client_index(no_existing_client_match: bool) -> ClientMatchIndex | None:
    """既存Notion取引先マスターとの名寄せ用インデックスを構築する（読み取り専用API呼び出し）。

    NOTION_API_KEYが未設定、--no-existing-client-matchが指定された場合、またはNotion API
    呼び出し自体が失敗した場合（キー失効・対象DBへのインテグレーション未接続・一時的な
    5xx等）はNoneを返し、plan_migration()側は常に新規作成する従来動作にフォールバックする
    （安全側のデフォルト: 突合に失敗しても移行そのものは止めない。shirokuma-secレビュー
    BLOCKER対応: 当初fetch_client_master_snapshots()の呼び出しがtry/exceptの外にあり、
    NotionApiError・requests例外が未処理のままmain()全体をクラッシュさせていた
    ＝部分結果すら出力されずに落ちる、という設計意図と矛盾する挙動があったため修正した）。
    """
    if no_existing_client_match:
        logger.info("--no-existing-client-match が指定されたため、既存Notionとの名寄せ突合をスキップします")
        return None
    db_ids = load_db_ids()
    client_master_db_id = db_ids.get("client_master")
    if client_master_db_id is None:
        logger.warning("取引先マスターDBのnotion_database_idが未設定のため、既存Notionとの名寄せ突合をスキップします")
        return None
    try:
        client = HttpNotionClient("client_master", client_master_db_id)
    except ValueError:
        logger.warning(
            "NOTION_API_KEYが未設定のため、既存Notionとの名寄せ突合をスキップします"
            "（常に新規作成する従来動作にフォールバック）"
        )
        return None
    try:
        snapshots = fetch_client_master_snapshots(client)
    except (ApiError, requests.exceptions.RequestException) as exc:
        logger.warning(
            "既存Notion取引先マスターの取得に失敗したため、名寄せ突合をスキップします"
            "（常に新規作成する従来動作にフォールバック）: %s",
            exc,
        )
        return None
    logger.info("既存Notion取引先マスター %d件を取得し、名寄せ突合インデックスを構築しました", len(snapshots))
    return build_client_match_index(snapshots)


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


def write_reports(plan: MigrationPlan, report_path: Path) -> tuple[Path, Path, Path]:
    """名寄せ・未解決リレーション・USER型未設定・取引先要レビューの各レポートCSVを書き出す。

    materialize()が例外で中断した場合でも呼び出せるよう、plan単体から書き出せる
    処理としてまとめている（BLOCKER8）。
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)
    unresolved_path = _related_report_path(report_path, "_unresolved")
    unresolved_user_path = _related_report_path(report_path, "_unresolved_users")
    needs_review_clients_path = _related_report_path(report_path, "_needs_review_clients")
    write_dedupe_report_csv(plan.dedupe_report, report_path)
    write_unresolved_report_csv(plan.unresolved, unresolved_path)
    write_unresolved_user_report_csv(plan.unresolved_user_properties, unresolved_user_path)
    write_needs_review_clients_report_csv(plan.needs_review_clients, needs_review_clients_path)
    return unresolved_path, unresolved_user_path, needs_review_clients_path


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

    client_master_rows = (
        read_client_master_csv_rows(args.client_master_csv) if args.client_master_csv else []
    )
    project_rows = read_csv_rows(args.project_csv) if args.project_csv else []
    action_rows = read_csv_rows(args.action_csv) if args.action_csv else []

    zoho_client_master_rows = (
        read_zoho_csv_rows(args.zoho_client_master_csv) if args.zoho_client_master_csv else []
    )
    zoho_contact_rows = read_zoho_csv_rows(args.zoho_contact_csv) if args.zoho_contact_csv else []
    zoho_project_rows = read_zoho_csv_rows(args.zoho_project_csv) if args.zoho_project_csv else []
    zoho_action_rows = read_zoho_csv_rows(args.zoho_action_csv) if args.zoho_action_csv else []
    zoho_product_rows = read_zoho_csv_rows(args.zoho_product_csv) if args.zoho_product_csv else []
    zoho_chain_rows = read_zoho_csv_rows(args.zoho_chain_csv) if args.zoho_chain_csv else []

    existing_client_index = load_existing_client_index(args.no_existing_client_match)

    plan = plan_migration(
        client_master_rows,
        project_rows,
        action_rows,
        existing_client_index=existing_client_index,
        zoho_client_master_rows=zoho_client_master_rows,
        zoho_contact_rows=zoho_contact_rows,
        zoho_project_rows=zoho_project_rows,
        zoho_action_rows=zoho_action_rows,
        zoho_product_rows=zoho_product_rows,
        zoho_chain_rows=zoho_chain_rows,
    )

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
    unresolved_path, unresolved_user_path, needs_review_clients_path = write_reports(
        plan, args.report_path
    )
    print(f"\n名寄せレポートを出力しました: {args.report_path}")
    print(f"未解決リレーションレポートを出力しました: {unresolved_path}")
    print(f"担当者未設定レポートを出力しました: {unresolved_user_path}")
    print(f"取引先マスター要レビューレポートを出力しました: {needs_review_clients_path}")


if __name__ == "__main__":
    main()
