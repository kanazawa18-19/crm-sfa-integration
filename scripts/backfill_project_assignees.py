#!/usr/bin/env python3
"""案件管理DB「担当メンバー」（USER型）の未設定分を、「担当者名」（TEXT型、Zoho移行時に
氏名文字列として先に移行済み）から一括で自動割当するバックフィルスクリプト
（2026-08-17、金沢さん依頼）。

■ 背景 -------------------------------------------------------------------------------------
kintone/Zoho移行時、「担当メンバー」（USER型）は氏名→NotionユーザーIDの対応表が無いため
未解決のまま作成された（詳細は`src/migration/migration_pipeline.py`モジュールdocstring
「■ 既知の設計判断・データギャップ」参照）。

- kintone由来の案件: kintone側に対応する担当者列が無く、「担当者名」自体も空。
  このバッチでは対応不可能（スコープ外）。
- Zoho由来の案件: `src/migration/zoho_project.py`が既にZoho CSVの担当者名を
  「担当者名」（TEXT型）へ移行済みのため、多くの案件で氏名の文字列が既に入っている。

本スクリプトはNotionワークスペースのユーザー一覧（`NotionUserDirectory`、`GET /v1/users`）
から氏名→NotionユーザーIDの逆引きマップを構築し、「担当メンバー」が空 かつ 「担当者名」が
非空の案件を対象に、氏名が1名かつワークスペース内で一意に確定する場合のみ自動割当する。

■ 安全方針（厳守） ---------------------------------------------------------------------------
数千件規模の本番Notion案件管理DBへの書き込みを伴うため、「誤った人物を割り当てるくらいなら
割り当てないほうが安全」という前提で設計している。
- --dry-runがデフォルト。実際にNotionへ書き込むには明示的に--executeを指定する必要がある
  （既存の`scripts/migrate_data.py`と同じdry-run優先パターン）。
- 名前解決は完全一致のみ（全角半角統一・前後空白除去程度の正規化のみ許容、過度な曖昧一致・
  部分一致はしない）。候補が複数（同姓同名等）・候補が0件（ワークスペースに該当ユーザーが
  見つからない）の場合は自動割当せず、レビュー用の一覧に出力するのみとする
  （既存の`src/migration/migration_pipeline.py`の`needs_review_clients`と同じ設計思想）。
- 「担当者名」に複数人分の氏名が含まれる場合（カンマ・読点区切り等）も、確定1名にならない
  ためレビュー行きとする。

使い方:
    # dry-run（デフォルト、Notionへの書き込みは行わない）
    python scripts/backfill_project_assignees.py

    # 実際に自動割当候補をNotionへ書き込む（事前にdry-run結果を確認してから実行すること）
    python scripts/backfill_project_assignees.py --execute

実行には環境変数 NOTION_API_KEY が必要（案件管理DBの読み書き・ワークスペースユーザー一覧の
読み取りに使う。ユーザー一覧取得にはNotion Integrationに「ユーザー情報の読み取り」権限が
必要、詳細は`src/api/user_directory.py`参照）。

■ 出力ファイルの取り扱い注意: レポートCSVには案件名・担当者名が含まれる。デフォルト出力先は
リポジトリ直下の`migration_output/`（.gitignore登録済み、`scripts/migrate_data.py`と同じ
出力先）。
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.notion_display import page_to_display_dict
from src.api.user_directory import NotionUserDirectory
from src.db_schema.project import PROJECT_SCHEMA
from src.migration._utils import parse_multi_value
from src.sync_engine.clients.notion_client import HttpNotionClient

logger = logging.getLogger(__name__)

_MIGRATION_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "migration_output"
_DEFAULT_AUTO_ASSIGN_REPORT_PATH = _MIGRATION_OUTPUT_DIR / "backfill_project_assignees_auto_assign.csv"
_DEFAULT_NEEDS_REVIEW_REPORT_PATH = _MIGRATION_OUTPUT_DIR / "backfill_project_assignees_needs_review.csv"

PROP_担当メンバー = "担当メンバー"
PROP_担当者名 = "担当者名"
PROP_案件名 = "案件名"

# コンソールへの詳細一覧表示は先頭何件までに絞るか（全件はCSVレポート参照）。
_CONSOLE_PREVIEW_LIMIT = 20


def normalize_name(name: str) -> str:
    """全角半角統一・前後空白除去のみを行う軽い正規化（完全一致照合用）。

    `src/migration/zoho_client_master.normalize_company_name_strong()`と同じ考え方
    （NFKC正規化）を氏名にも適用する。過度な曖昧一致・部分一致は行わない。
    """
    return unicodedata.normalize("NFKC", name).strip()


def build_name_to_user_ids(user_directory: NotionUserDirectory) -> dict[str, list[str]]:
    """Notionワークスペースの全ユーザーから、正規化した氏名 -> NotionユーザーIDリストの
    逆引きマップを構築する。同姓同名は複数idのリストになり、後続の解決処理で自動割当
    対象から除外される。
    """
    mapping: dict[str, list[str]] = {}
    for user_id, name in user_directory.all_names_by_id().items():
        mapping.setdefault(normalize_name(name), []).append(user_id)
    return mapping


@dataclass(frozen=True)
class AutoAssignCandidate:
    page_id: str
    project_name: str
    raw_assignee_name: str
    resolved_user_id: str
    resolved_user_name: str


@dataclass(frozen=True)
class NeedsReviewEntry:
    page_id: str
    project_name: str
    raw_assignee_name: str
    reason: str


@dataclass(frozen=True)
class BackfillPlan:
    auto_assign: list[AutoAssignCandidate]
    needs_review: list[NeedsReviewEntry]


def plan_backfill(
    pages: list[dict[str, Any]],
    name_to_user_ids: dict[str, list[str]],
) -> BackfillPlan:
    """案件管理DBの生ページ一覧（`HttpNotionClient.query_all_pages()`の戻り値）から、
    自動割当候補とレビュー行きを分類する（I/O無しの純粋関数）。

    対象は「担当メンバー」が空 かつ 「担当者名」が非空の案件のみ。それ以外
    （既に担当メンバー設定済み、担当者名も空でkintone由来など判断材料が無い）はスキップし
    どちらの一覧にも含めない。
    """
    auto_assign: list[AutoAssignCandidate] = []
    needs_review: list[NeedsReviewEntry] = []

    for page in pages:
        record, _skipped = page_to_display_dict(page, PROJECT_SCHEMA)
        assignees = record.get(PROP_担当メンバー) or []
        if assignees:
            continue  # 既に担当メンバーが設定済み、対象外

        raw_name = record.get(PROP_担当者名)
        if not raw_name or not raw_name.strip():
            continue  # 担当者名も空、このバッチでは判断材料が無いため対象外（kintone由来等）

        project_name = record.get(PROP_案件名) or ""
        page_id = record["notion_page_id"]

        names = parse_multi_value(raw_name)
        if len(names) != 1:
            needs_review.append(
                NeedsReviewEntry(
                    page_id=page_id,
                    project_name=project_name,
                    raw_assignee_name=raw_name,
                    reason=f"担当者名から{len(names)}名分の氏名を検知しました（複数名、または解析不能）",
                )
            )
            continue

        candidates = name_to_user_ids.get(normalize_name(names[0]), [])
        if not candidates:
            needs_review.append(
                NeedsReviewEntry(
                    page_id=page_id,
                    project_name=project_name,
                    raw_assignee_name=raw_name,
                    reason="ワークスペースに該当するユーザーが見つかりません",
                )
            )
        elif len(candidates) > 1:
            needs_review.append(
                NeedsReviewEntry(
                    page_id=page_id,
                    project_name=project_name,
                    raw_assignee_name=raw_name,
                    reason=f"同姓同名の候補が{len(candidates)}名見つかりました（自動確定できません）",
                )
            )
        else:
            auto_assign.append(
                AutoAssignCandidate(
                    page_id=page_id,
                    project_name=project_name,
                    raw_assignee_name=raw_name,
                    resolved_user_id=candidates[0],
                    resolved_user_name=names[0],
                )
            )

    return BackfillPlan(auto_assign=auto_assign, needs_review=needs_review)


def print_summary(plan: BackfillPlan, *, total_pages: int, dry_run: bool) -> None:
    verb = "割当予定" if dry_run else "割当"
    target_count = len(plan.auto_assign) + len(plan.needs_review)
    print(f"\n=== 担当メンバー・バックフィル結果サマリー（{'dry-run' if dry_run else '本番実行'}） ===")
    print(f"  案件管理DB全件: {total_pages}件")
    print(f"  対象（担当メンバー未設定 かつ 担当者名あり）: {target_count}件")
    print(f"  自動{verb}: {len(plan.auto_assign)}件")
    print(f"  レビュー行き（自動判定できず）: {len(plan.needs_review)}件")

    if plan.auto_assign:
        print(f"\n--- 自動{verb}の内訳（先頭{_CONSOLE_PREVIEW_LIMIT}件、全件はCSVレポートを参照） ---")
        for c in plan.auto_assign[:_CONSOLE_PREVIEW_LIMIT]:
            print(
                f"  [{c.project_name}] 担当者名={c.raw_assignee_name!r} "
                f"-> {c.resolved_user_name} ({c.resolved_user_id})"
            )

    if plan.needs_review:
        print(f"\n--- レビュー行きの内訳（先頭{_CONSOLE_PREVIEW_LIMIT}件、全件はCSVレポートを参照） ---")
        for r in plan.needs_review[:_CONSOLE_PREVIEW_LIMIT]:
            print(f"  [{r.project_name}] 担当者名={r.raw_assignee_name!r}: {r.reason}")


def write_auto_assign_csv(entries: list[AutoAssignCandidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["page_id", "project_name", "raw_assignee_name", "resolved_user_id", "resolved_user_name"]
        )
        for entry in entries:
            writer.writerow(
                [
                    entry.page_id,
                    entry.project_name,
                    entry.raw_assignee_name,
                    entry.resolved_user_id,
                    entry.resolved_user_name,
                ]
            )


def write_needs_review_csv(entries: list[NeedsReviewEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["page_id", "project_name", "raw_assignee_name", "reason"])
        for entry in entries:
            writer.writerow([entry.page_id, entry.project_name, entry.raw_assignee_name, entry.reason])


def execute_assignments(project_client: HttpNotionClient, auto_assign: list[AutoAssignCandidate]) -> None:
    """自動割当候補を実際にNotionへ書き込む（--execute指定時のみ呼ばれる経路）。

    `HttpNotionClient.update_page()`経由の書き込みには監査ログ記録が既にフックされている
    （`src/audit_log/`）ため、ここでの追加対応は不要。
    """
    total = len(auto_assign)
    for i, candidate in enumerate(auto_assign, start=1):
        project_client.update_page(candidate.page_id, {PROP_担当メンバー: [candidate.resolved_user_id]})
        logger.info(
            "[%d/%d] %s へ %s (%s) を割当しました",
            i,
            total,
            candidate.project_name,
            candidate.resolved_user_name,
            candidate.resolved_user_id,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="実際にNotionへ自動割当候補を書き込む（未指定時はdry-runのみで書き込まない）",
    )
    parser.add_argument(
        "--auto-assign-report-path",
        type=Path,
        default=_DEFAULT_AUTO_ASSIGN_REPORT_PATH,
        help="自動割当候補一覧CSVの出力先",
    )
    parser.add_argument(
        "--needs-review-report-path",
        type=Path,
        default=_DEFAULT_NEEDS_REVIEW_REPORT_PATH,
        help="レビュー行き一覧CSVの出力先",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)

    user_directory = NotionUserDirectory()
    name_to_user_ids = build_name_to_user_ids(user_directory)

    project_client = HttpNotionClient(PROJECT_SCHEMA.key, PROJECT_SCHEMA.notion_database_id)
    pages = project_client.query_all_pages()

    plan = plan_backfill(pages, name_to_user_ids)

    print_summary(plan, total_pages=len(pages), dry_run=not args.execute)
    write_auto_assign_csv(plan.auto_assign, args.auto_assign_report_path)
    write_needs_review_csv(plan.needs_review, args.needs_review_report_path)
    print(f"\n自動割当候補レポートを出力しました: {args.auto_assign_report_path}")
    print(f"レビュー行きレポートを出力しました: {args.needs_review_report_path}")

    if not args.execute:
        print("\n--dry-run のため書き込みは行っていません。内容を確認の上、--execute で実行してください。")
        return

    print(f"\n{len(plan.auto_assign)}件の担当メンバーをNotionへ書き込みます...")
    execute_assignments(project_client, plan.auto_assign)
    print("\n書き込み完了。")


if __name__ == "__main__":
    main()
