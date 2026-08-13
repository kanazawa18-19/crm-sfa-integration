#!/usr/bin/env python3
"""事業計画スプレッドシート連携設定（`src/reports/revenue_target_settings.py`）専用の、
新規Notion database「事業計画連携設定」を作成するワンショットセットアップスクリプト。

`scripts/setup_notion_databases.py`は既存DBへの`PATCH`（プロパティ追加）のみを行う
スコープのスクリプトであり、新規DB作成（`POST /v1/databases`）は行わない。本スクリプトは
それとは別に、`RevenueTargetSettingsStore`が読み書きする唯一のレコード用DBを新規作成する。

■ 実行前に必要なもの
1. `NOTION_API_KEY`（Integrationが親ページへのアクセス権を持っていること）。
2. `--parent-page-id`（またはPARENT_PAGE_ID環境変数）: 新規DBを配置する親ページのID。
   Notion APIでのdatabase作成には親ページが必須のため、事前にNotion側で適当な場所に
   空ページ（例:「システム設定」）を1つ用意し、そのページIDを渡すこと
   （どこに置くべきかは本番Notionワークスペースの構成を把握している人間の判断が必要なため、
   本スクリプトでは決め打ちしない）。

■ このスクリプトが作るもの
タイトル「事業計画連携設定」のdatabaseを1つ。プロパティは以下の4つ
（`RevenueTargetSettingsStore`が読み書きする形と対応）。
- key (title): 固定値"revenue_target_sheet_pointer"の1行のみを想定
- spreadsheet_id (rich_text)
- mrr_sheet_name (rich_text)
- unit_count_sheet_name (rich_text)
- updated_at (date)

使い方:
    python scripts/setup_revenue_target_settings_db.py --dry-run --parent-page-id <PAGE_ID>
    python scripts/setup_revenue_target_settings_db.py --parent-page-id <PAGE_ID>

実行後、出力されたdatabase_idを環境変数
`REVENUE_TARGET_SETTINGS_NOTION_DATABASE_ID`に設定すること（config/.env等）。

■ 本番実行についての注記（重要）
このスクリプトは実装時点では**実行していない**。本番Notionワークスペースのどこに
このDBを置くべきかは金沢さんの判断が必要な事項であり、実装担当（イヌ）が代わりに
判断・実行すべきことではないため、スクリプトの用意までに留めている。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2022-06-28"
_REQUEST_TIMEOUT_SECONDS = 30

DATABASE_TITLE = "事業計画連携設定"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parent-page-id",
        default=os.environ.get("PARENT_PAGE_ID"),
        help="新規DBを配置する親ページのID（PARENT_PAGE_ID環境変数でも指定可）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Notion APIを呼び出さず、作成予定のペイロードを表示するだけ",
    )
    return parser.parse_args(argv)


def build_create_database_payload(parent_page_id: str) -> dict[str, Any]:
    return {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": DATABASE_TITLE}}],
        "properties": {
            "key": {"title": {}},
            "spreadsheet_id": {"rich_text": {}},
            "mrr_sheet_name": {"rich_text": {}},
            "unit_count_sheet_name": {"rich_text": {}},
            "updated_at": {"date": {}},
        },
    }


def _notion_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def create_database(api_key: str, parent_page_id: str) -> dict[str, Any]:
    import requests

    payload = build_create_database_payload(parent_page_id)
    response = requests.post(
        f"{NOTION_API_BASE}/databases",
        headers=_notion_headers(api_key),
        json=payload,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if not args.parent_page_id:
        print(
            "ERROR: --parent-page-id（またはPARENT_PAGE_ID環境変数）が必要です。"
            " 本モジュールdocstring参照。",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.dry_run:
        print(f"=== dry-run: 作成予定のdatabase（親ページ: {args.parent_page_id}） ===\n")
        print(build_create_database_payload(args.parent_page_id))
        return

    api_key = os.environ.get("NOTION_API_KEY")
    if not api_key:
        print(
            "ERROR: 環境変数 NOTION_API_KEY が設定されていません。"
            " config/.env に NOTION_API_KEY を設定してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    result = create_database(api_key, args.parent_page_id)
    database_id = result.get("id")
    print(f"作成しました: {DATABASE_TITLE} (database_id={database_id})")
    print(
        "\n次に、環境変数 REVENUE_TARGET_SETTINGS_NOTION_DATABASE_ID に"
        f" {database_id} を設定してください。"
    )


if __name__ == "__main__":
    main()
