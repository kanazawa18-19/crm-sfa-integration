#!/usr/bin/env python3
"""案件管理DB「担当メンバー」（USER型）の未設定分を、Zoho Deals標準の`Owner`フィールド
から一括で自動割当するバックフィルスクリプト（2026-08-17、金沢さん依頼）。

■ 背景 -------------------------------------------------------------------------------------
以前は案件管理DBの「担当者名」（TEXT型、Zoho移行時に氏名文字列として先に移行済み）から
氏名を名寄せして自動割当する実装だったが、本番相当環境での検証の結果「担当者名」列は
実際には客先（ホテル側）の窓口担当者名であり、内部の営業担当者ではないと判明した
（dry-run結果: auto_assign_count=0、姓のみ・「様」付きの名前ばかりだった）。

正しい情報源はZoho CRMの標準`Owner`フィールド（日本語ラベル「案件の担当者」、
`config/zoho_field_mapping.json`の`Deals.Owner`で確認済み）だが、これまでのNotion移行処理
では一切参照されていなかった。本スクリプトはこのZoho Ownerを情報源として作り直したもの。

Notion page ID ↔ Zoho Deal ID の対応は、案件管理DBのページ自体には保持されていない
（`zoho_ID`は移行時のみ使う内部専用キーで、Notion側には書き込まれない）ため、本番の
双方向同期エンジンが使っている`IdMappingStore`（`src/sync_engine/id_mapping.py`）から
`list_by_db()`で取得する。

さらに、Notion側の氏名解決には別の制約がある。営業メンバーはNotionに**ゲストユーザー**
として登録されており、Notionワークスペース全ユーザー一覧API（`GET /v1/users`、
`NotionUserDirectory`が使うAPI）はゲストユーザーを一切返さない（Notion公式の既知の仕様）。
個別ID指定の`GET /v1/users/{user_id}`ならゲストでも解決できることを確認済みだが、本
スクリプトでは各DBのpeople型プロパティを横断的にスキャンして事前に発見・確認済みの
Zoho Owner 9人中4人分のNotionユーザーIDを`KNOWN_OWNERS`へハードコードして使う。残り5人
（杉本健介・寺田亘平・伊藤翼・末永琢磨・増田崚士）はNotionアカウントが非アクティブのため
解決不可能であり、この5人がZoho Owner担当の案件は「要レビュー」に振り分け、自動割当は
しない。

■ 安全方針（厳守） ---------------------------------------------------------------------------
数千件規模の本番Notion案件管理DBへの書き込みを伴うため、「誤った人物を割り当てるくらいなら
割り当てないほうが安全」という前提で設計している。
- --dry-runがデフォルト。実際にNotionへ書き込むには明示的に--executeを指定する必要がある
  （既存の`scripts/migrate_data.py`と同じdry-run優先パターン）。
- 自動割当するのは、Notion page ↔ Zoho Deal IDの対応が判明し、かつZoho Dealに`Owner`が
  設定されており、かつそのOwnerのメールアドレスが`KNOWN_OWNERS`に存在する（＝確認済みの
  4名のいずれか、大文字小文字・前後空白を無視して比較する）場合のみ。それ以外（IDマッピング
  未登録、Zoho Deal自体にOwner未設定、Owner解決不能な残り5名等）は全てレビュー用の一覧に
  出力するのみとする。
- 既に「担当メンバー」が設定済みの案件はスキップ（対象外）。
- TOCTOU対策: `plan_backfill()`でのプラン作成から実際に`--execute`で書き込むまでの間
  （Zoho全件ページング＋数百件のPATCH処理で時間がかかる）に、誰かが手動でNotion側に
  「担当メンバー」を設定した可能性がある。`execute_assignments()`は各ページを書き込む
  直前に`get_page()`で現在値を再確認し、既に空でなくなっていれば上書きせずスキップする
  （`ExecutionResult.skipped`参照）。
- 1件の書き込み失敗（Notion APIエラー等）で残り全件の処理が止まらないよう、
  `execute_assignments()`は1件ごとにtry/exceptで囲み、失敗した候補は
  `ExecutionResult.failed`へ積んで次の候補へ進む。

使い方:
    # dry-run（デフォルト、Notionへの書き込みは行わない）
    python scripts/backfill_project_assignees.py

    # 実際に自動割当候補をNotionへ書き込む（事前にdry-run結果を確認してから実行すること）
    python scripts/backfill_project_assignees.py --execute

実行には環境変数 NOTION_API_KEY（案件管理DBの読み書き）・Zoho認証情報
（ZOHO_CLIENT_ID/ZOHO_CLIENT_SECRET/ZOHO_REFRESH_TOKEN、Deals閲覧に使う）・
IDマッピングストア関連（SYNC_ID_MAPPING_BACKEND等、`build_id_mapping_store()`参照）が必要。

■ 出力ファイルの取り扱い注意: レポートCSVには案件名・Ownerメールアドレスが含まれる。
デフォルト出力先はリポジトリ直下の`migration_output/`（.gitignore登録済み、
`scripts/migrate_data.py`と同じ出力先）。

■ kintone連携への影響について（要リスク確認） -------------------------------------------------
「担当メンバー」プロパティは`SyncScope.ALL_TOOLS`（kintone/Zoho/スプレッドシート同期対象）
であり、本スクリプトのNotion書き込みは双方向同期のWebhook経由でkintoneへも伝播しうる。
kintone側には「担当メンバー」に対応する列が存在しないため、kintone起源の案件（IdMappingに
`kintone_id`が設定されている案件）でエラーになる懸念がある。Zoho起源の案件（`kintone_id`が
無い）は`_MultiDbKintoneSyncTarget`が`db_key`未解決時にNoneを返す（
`src/sync_engine/production_wiring.py`参照）ことで安全と確認済みだが、kintone起源の案件は
未確認のため、「同期先データ欠損リスクがある変更は禁止」という運用方針に従い、`plan_backfill()`
はkintone起源の案件（`notion_page_id_to_kintone_id`に含まれる案件）を自動割当候補から除外し、
`reason_category="kintone_origin_excluded"`として要レビューへ振り分ける（2026-08-17、
最終確認レビューWARN対応。以前は`AutoAssignCandidate.has_kintone_id`で件数を可視化するのみで
実際には自動割当・`--execute`時の書き込み対象に含めてしまっていたため、件数表示を見落とすと
未確認のkintone起源案件へそのまま書き込まれてしまう抜け穴があった）。kintone側の同期対応が
確認できたら、このカテゴリの案件をあらためて別途対応すること。
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.notion_display import page_to_display_dict
from src.db_schema.project import PROJECT_SCHEMA
from src.sync_engine.clients._http import raise_for_error
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.clients.zoho_client import HttpZohoClient, ZohoApiError
from src.sync_engine.id_mapping import IdMapping
from src.sync_engine.production_wiring import build_id_mapping_store
from src.sync_engine.zoho_watch_channel import DEFAULT_WATCH_API_BASE_URL, build_zoho_client_from_env

logger = logging.getLogger(__name__)

_MIGRATION_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "migration_output"
_DEFAULT_AUTO_ASSIGN_REPORT_PATH = _MIGRATION_OUTPUT_DIR / "backfill_project_assignees_auto_assign.csv"
_DEFAULT_NEEDS_REVIEW_REPORT_PATH = _MIGRATION_OUTPUT_DIR / "backfill_project_assignees_needs_review.csv"

PROP_担当メンバー = "担当メンバー"
PROP_案件名 = "案件名"

# コンソールへの詳細一覧表示は先頭何件までに絞るか（全件はCSVレポート参照）。
_CONSOLE_PREVIEW_LIMIT = 20


@dataclass(frozen=True)
class KnownOwner:
    """`KNOWN_OWNERS`の値。NotionユーザーIDと表示名を1組で保持する
    （2026-08-17、shirokuma-sec/obasan-qualityレビューWARN対応: 以前は
    `KNOWN_OWNER_EMAIL_TO_NOTION_USER_ID`/`_KNOWN_OWNER_DISPLAY_NAMES`の2辞書に分かれており、
    片方だけ更新すると気付きにくかったため単一の辞書へ統合した）。
    """

    notion_user_id: str
    display_name: str


# 案件管理DB・チェーンDB・連絡先DBの既存people型プロパティを横断的にスキャンして発見・
# 確認済みのZoho Owner 9人中4人分のNotionユーザーID（モジュールdocstring参照）。
# 確認済みのためそのままハードコードしてよい。キー（メールアドレス）はZoho APIレスポンスとの
# 突合時に大文字小文字・前後空白を無視して比較する（`_normalize_email`/`plan_backfill`参照）。
KNOWN_OWNERS: dict[str, KnownOwner] = {
    "kunikata@cnctor.jp": KnownOwner("0fa87cdd-c868-4483-a394-8736b7e65d62", "國方勇樹"),
    "ono.sh@cnctor.jp": KnownOwner("fc7acd3f-2277-4538-a70c-7c92e4d9813e", "大野駿太郎"),
    "kanazawa@cnctor.jp": KnownOwner("5a59d895-0ea2-4597-9bf5-161d98c39101", "金沢裕貴"),
    "hiramoto@cnctor.jp": KnownOwner("8eb946ee-27c6-4919-bb41-216f16da923f", "平本來輝"),
}

# NeedsReviewEntry.reason_categoryが取りうる値。理由文字列（自由文）を正規表現等で
# 後から分類するのではなく、分類したうえで人間可読の`reason`を別途持たせる
# （2026-08-17、obasan-qualityレビューWARN対応）。
ReasonCategory = Literal[
    "id_mapping_missing", "owner_not_set", "owner_unresolved", "kintone_origin_excluded"
]

_REASON_CATEGORY_LABELS: dict[ReasonCategory, str] = {
    "id_mapping_missing": "IDマッピング未登録",
    "owner_not_set": "Zoho Owner未設定",
    "owner_unresolved": "Zoho Owner解決不能",
    "kintone_origin_excluded": "kintone起源のため対象外",
}


def _normalize_email(email: str) -> str:
    """メールアドレスの大文字小文字・前後空白を無視して比較するための正規化。"""
    return email.strip().lower()


def build_notion_page_id_to_zoho_deal_id(mappings: list[IdMapping]) -> dict[str, str]:
    """`IdMappingStore.list_by_db(PROJECT_SCHEMA.key)`の戻り値から、Notion page ID ->
    Zoho Deal ID の対応表を構築する（zoho_idが無いマッピングは除外）。
    """
    return {m.notion_key: m.zoho_id for m in mappings if m.zoho_id}


def build_notion_page_id_to_kintone_id(mappings: list[IdMapping]) -> dict[str, str]:
    """`IdMappingStore.list_by_db(PROJECT_SCHEMA.key)`の戻り値から、Notion page ID ->
    kintoneレコード番号の対応表を構築する（kintone_idが無い＝Zoho起源のマッピングは除外）。
    kintone起源の案件を自動割当候補から除外する判定に使う
    （モジュールdocstring「■ kintone連携への影響について」参照）。
    """
    return {m.notion_key: m.kintone_id for m in mappings if m.kintone_id}


def fetch_zoho_deal_owner_emails(client: HttpZohoClient) -> dict[str, str]:
    """Zoho Deals APIを全件ページングし、Zoho Deal ID -> Ownerメールアドレスの対応表を
    構築する（Ownerが未設定、またはメールアドレスが取得できないDealは対応表に含めない）。
    """
    owner_email_by_deal_id: dict[str, str] = {}
    page = 1
    per_page = 200
    while True:
        url = f"{DEFAULT_WATCH_API_BASE_URL}/Deals?fields=Owner,id&per_page={per_page}&page={page}"
        response = client.request("GET", url)
        if response.status_code == 204:
            break
        raise_for_error(response, ZohoApiError)
        body = response.json()
        for deal in body.get("data") or []:
            deal_id = deal.get("id")
            owner = deal.get("Owner") or {}
            email = owner.get("email")
            if deal_id and email:
                owner_email_by_deal_id[str(deal_id)] = email
        info = body.get("info") or {}
        if not info.get("more_records"):
            break
        page += 1
    return owner_email_by_deal_id


@dataclass(frozen=True)
class AutoAssignCandidate:
    page_id: str
    project_name: str
    zoho_deal_id: str
    owner_email: str
    resolved_user_id: str
    resolved_user_name: str


@dataclass(frozen=True)
class NeedsReviewEntry:
    page_id: str
    project_name: str
    reason: str
    reason_category: ReasonCategory


@dataclass(frozen=True)
class BackfillPlan:
    auto_assign: list[AutoAssignCandidate]
    needs_review: list[NeedsReviewEntry]


def plan_backfill(
    pages: list[dict[str, Any]],
    notion_page_id_to_zoho_deal_id: dict[str, str],
    zoho_deal_owner_emails: dict[str, str],
    notion_page_id_to_kintone_id: dict[str, str] | None = None,
    *,
    known_owners: dict[str, KnownOwner] = KNOWN_OWNERS,
) -> BackfillPlan:
    """案件管理DBの生ページ一覧（`HttpNotionClient.query_all_pages()`の戻り値）から、
    自動割当候補とレビュー行きを分類する（I/O無しの純粋関数）。

    対象は「担当メンバー」が空の案件のみ（既に設定済みの案件はスキップし、どちらの一覧にも
    含めない）。`known_owners`のキー（メールアドレス）はZoho APIレスポンスの`Owner.email`と
    大文字小文字・前後空白を無視して比較する（`_normalize_email`参照）。
    """
    notion_page_id_to_kintone_id = notion_page_id_to_kintone_id or {}
    normalized_known_owners = {
        _normalize_email(email): owner for email, owner in known_owners.items()
    }

    auto_assign: list[AutoAssignCandidate] = []
    needs_review: list[NeedsReviewEntry] = []

    for page in pages:
        record, _skipped = page_to_display_dict(page, PROJECT_SCHEMA)
        assignees = record.get(PROP_担当メンバー) or []
        if assignees:
            continue  # 既に担当メンバーが設定済み、対象外

        project_name = record.get(PROP_案件名) or ""
        page_id = record["notion_page_id"]

        zoho_deal_id = notion_page_id_to_zoho_deal_id.get(page_id)
        if zoho_deal_id is None:
            needs_review.append(
                NeedsReviewEntry(
                    page_id=page_id,
                    project_name=project_name,
                    reason="対応するZoho Deal IDが見つかりません（IDマッピング未登録）",
                    reason_category="id_mapping_missing",
                )
            )
            continue

        owner_email = zoho_deal_owner_emails.get(zoho_deal_id)
        if not owner_email:
            needs_review.append(
                NeedsReviewEntry(
                    page_id=page_id,
                    project_name=project_name,
                    reason=f"Zoho Deal({zoho_deal_id})にOwnerが設定されていません",
                    reason_category="owner_not_set",
                )
            )
            continue

        known_owner = normalized_known_owners.get(_normalize_email(owner_email))
        if known_owner is None:
            needs_review.append(
                NeedsReviewEntry(
                    page_id=page_id,
                    project_name=project_name,
                    reason=f"Zoho Owner({owner_email})に対応するNotionユーザーが未解決です"
                    "（Notionアカウント非アクティブ等）",
                    reason_category="owner_unresolved",
                )
            )
            continue

        if page_id in notion_page_id_to_kintone_id:
            # kintone起源の案件はNotion書き込みがWebhook経由でkintoneへ伝播した場合の挙動が
            # 未確認のため、「同期先データ欠損リスクがある変更は禁止」の方針に従い自動割当
            # から除外する（モジュールdocstring「■ kintone連携への影響について」参照）。
            needs_review.append(
                NeedsReviewEntry(
                    page_id=page_id,
                    project_name=project_name,
                    reason="kintone起源案件のため今回は自動割当対象外です"
                    "（Webhook経由の同期先未確認のため）",
                    reason_category="kintone_origin_excluded",
                )
            )
            continue

        auto_assign.append(
            AutoAssignCandidate(
                page_id=page_id,
                project_name=project_name,
                zoho_deal_id=zoho_deal_id,
                owner_email=owner_email,
                resolved_user_id=known_owner.notion_user_id,
                resolved_user_name=known_owner.display_name,
            )
        )

    return BackfillPlan(auto_assign=auto_assign, needs_review=needs_review)


def print_summary(plan: BackfillPlan, *, total_pages: int, dry_run: bool) -> None:
    verb = "割当予定" if dry_run else "割当"
    target_count = len(plan.auto_assign) + len(plan.needs_review)
    print(f"\n=== 担当メンバー・バックフィル結果サマリー（{'dry-run' if dry_run else '本番実行'}） ===")
    print(f"  担当メンバー未設定の案件（Notion側フィルタ済み）: {total_pages}件")
    print(f"  対象（判定対象）: {target_count}件")
    print(f"  自動{verb}: {len(plan.auto_assign)}件")
    print(f"  レビュー行き（自動判定できず）: {len(plan.needs_review)}件")

    if plan.needs_review:
        category_counts = Counter(r.reason_category for r in plan.needs_review)
        print("  レビュー行きの理由別内訳:")
        for category, label in _REASON_CATEGORY_LABELS.items():
            count = category_counts.get(category, 0)
            if count:
                print(f"    {label}: {count}件")

    if plan.auto_assign:
        print(f"\n--- 自動{verb}の内訳（先頭{_CONSOLE_PREVIEW_LIMIT}件、全件はCSVレポートを参照） ---")
        for c in plan.auto_assign[:_CONSOLE_PREVIEW_LIMIT]:
            print(
                f"  [{c.project_name}] page_id={c.page_id} Owner={c.owner_email} "
                f"-> {c.resolved_user_name} ({c.resolved_user_id})"
            )

    if plan.needs_review:
        print(f"\n--- レビュー行きの内訳（先頭{_CONSOLE_PREVIEW_LIMIT}件、全件はCSVレポートを参照） ---")
        for r in plan.needs_review[:_CONSOLE_PREVIEW_LIMIT]:
            print(f"  [{r.project_name}] page_id={r.page_id}: {r.reason}")


def write_auto_assign_csv(entries: list[AutoAssignCandidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "page_id",
                "project_name",
                "zoho_deal_id",
                "owner_email",
                "resolved_user_id",
                "resolved_user_name",
            ]
        )
        for entry in entries:
            writer.writerow(
                [
                    entry.page_id,
                    entry.project_name,
                    entry.zoho_deal_id,
                    entry.owner_email,
                    entry.resolved_user_id,
                    entry.resolved_user_name,
                ]
            )


def write_needs_review_csv(entries: list[NeedsReviewEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["page_id", "project_name", "reason_category", "reason"])
        for entry in entries:
            writer.writerow(
                [entry.page_id, entry.project_name, entry.reason_category, entry.reason]
            )


@dataclass(frozen=True)
class FailedAssignment:
    candidate: AutoAssignCandidate
    error: str


@dataclass(frozen=True)
class ExecutionResult:
    succeeded: list[AutoAssignCandidate]
    skipped: list[AutoAssignCandidate]
    failed: list[FailedAssignment]


def execute_assignments(
    project_client: HttpNotionClient, auto_assign: list[AutoAssignCandidate]
) -> ExecutionResult:
    """自動割当候補を実際にNotionへ書き込む（--execute指定時のみ呼ばれる経路）。

    書き込み直前に`get_page()`で現在の「担当メンバー」を再確認し、既に空でなくなっていれば
    上書きせずスキップする（TOCTOU対策、モジュールdocstring「■ 安全方針」参照）。
    1件ごとにtry/exceptで囲み、失敗した候補があっても残りの候補の処理を継続する。

    `HttpNotionClient.update_page()`経由の書き込みには監査ログ記録が既にフックされている
    （`src/audit_log/`）ため、ここでの追加対応は不要。
    """
    total = len(auto_assign)
    succeeded: list[AutoAssignCandidate] = []
    skipped: list[AutoAssignCandidate] = []
    failed: list[FailedAssignment] = []

    for i, candidate in enumerate(auto_assign, start=1):
        try:
            current = project_client.get_page(candidate.page_id)
        except Exception as exc:  # noqa: BLE001 - 1件の失敗で残り全件を止めないため意図的に広く捕捉
            logger.error(
                "[%d/%d] %s (page_id=%s) の書き込み直前再確認に失敗しました: %s",
                i,
                total,
                candidate.project_name,
                candidate.page_id,
                exc,
            )
            failed.append(FailedAssignment(candidate=candidate, error=str(exc)))
            continue

        if current is None:
            logger.warning(
                "[%d/%d] %s (page_id=%s) は書き込み直前の再確認でページが見つからなかった"
                "ためスキップしました（削除された可能性があります）",
                i,
                total,
                candidate.project_name,
                candidate.page_id,
            )
            skipped.append(candidate)
            continue

        current_assignees = current.get(PROP_担当メンバー) or []
        if current_assignees:
            logger.warning(
                "[%d/%d] %s (page_id=%s) は書き込み直前の再確認で既に担当メンバーが設定済み"
                "だったためスキップしました（手動設定と競合した可能性があります）",
                i,
                total,
                candidate.project_name,
                candidate.page_id,
            )
            skipped.append(candidate)
            continue

        try:
            project_client.update_page(candidate.page_id, {PROP_担当メンバー: [candidate.resolved_user_id]})
        except Exception as exc:  # noqa: BLE001 - 1件の失敗で残り全件を止めないため意図的に広く捕捉
            logger.error(
                "[%d/%d] %s (page_id=%s) への割当に失敗しました: %s",
                i,
                total,
                candidate.project_name,
                candidate.page_id,
                exc,
            )
            failed.append(FailedAssignment(candidate=candidate, error=str(exc)))
            continue

        logger.info(
            "[%d/%d] %s へ %s (%s) を割当しました",
            i,
            total,
            candidate.project_name,
            candidate.resolved_user_name,
            candidate.resolved_user_id,
        )
        succeeded.append(candidate)

    return ExecutionResult(succeeded=succeeded, skipped=skipped, failed=failed)


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

    id_mapping_store = build_id_mapping_store()
    mappings = id_mapping_store.list_by_db(PROJECT_SCHEMA.key)
    notion_page_id_to_zoho_deal_id = build_notion_page_id_to_zoho_deal_id(mappings)
    notion_page_id_to_kintone_id = build_notion_page_id_to_kintone_id(mappings)

    zoho_client = build_zoho_client_from_env()
    zoho_deal_owner_emails = fetch_zoho_deal_owner_emails(zoho_client)

    project_client = HttpNotionClient(PROJECT_SCHEMA.key, PROJECT_SCHEMA.notion_database_id)
    pages = project_client.query_all_pages(
        filter={"property": PROP_担当メンバー, "people": {"is_empty": True}}
    )

    plan = plan_backfill(
        pages,
        notion_page_id_to_zoho_deal_id,
        zoho_deal_owner_emails,
        notion_page_id_to_kintone_id,
    )

    print_summary(plan, total_pages=len(pages), dry_run=not args.execute)
    write_auto_assign_csv(plan.auto_assign, args.auto_assign_report_path)
    write_needs_review_csv(plan.needs_review, args.needs_review_report_path)
    print(f"\n自動割当候補レポートを出力しました: {args.auto_assign_report_path}")
    print(f"レビュー行きレポートを出力しました: {args.needs_review_report_path}")

    if not args.execute:
        print("\n--dry-run のため書き込みは行っていません。内容を確認の上、--execute で実行してください。")
        return

    print(f"\n{len(plan.auto_assign)}件の担当メンバーをNotionへ書き込みます...")
    result = execute_assignments(project_client, plan.auto_assign)
    print(
        f"\n書き込み完了: 成功{len(result.succeeded)}件 / "
        f"スキップ{len(result.skipped)}件（直前の再確認で既に設定済み・削除済み等） / "
        f"失敗{len(result.failed)}件"
    )
    if result.failed:
        print("--- 失敗した案件（要再実行または個別確認） ---")
        for f in result.failed:
            print(f"  [{f.candidate.project_name}] page_id={f.candidate.page_id}: {f.error}")


if __name__ == "__main__":
    main()
