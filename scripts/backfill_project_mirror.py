#!/usr/bin/env python3
"""案件管理DB(Notion)のPostgresミラー(ProjectMirror)を初回バックフィルするスクリプト
(2026-08-17)。

`src/project_mirror/sync.py`の`refresh_all_projects()`（初回バックフィル・夜間
reconciliation cron共通のフル同期処理）を呼ぶだけの薄いCLIラッパー。読み取り専用
(Notion API)+自ミラーへのUPSERT/DELETEのみで、他システムへの書き込み・Notion本番データの
破壊リスクが無いため、`scripts/backfill_project_assignees.py`のようなdry-run優先パターンは
採用していない。

使い方:
    python scripts/backfill_project_mirror.py

実行には環境変数 NOTION_API_KEY（案件管理DBの読み取り）・DATABASE_URL（ProjectMirrorの
書き込み先Neon Postgres）が必要。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.user_directory import NotionUserDirectory
from src.db_schema.project import PROJECT_SCHEMA
from src.project_mirror.sync import refresh_all_projects
from src.sync_engine.clients.notion_client import HttpNotionClient

# 案件管理DBは約1万件規模（モジュールdocstring参照）。synced_countがこの件数を下回る場合、
# 権限不足・フィルタ誤設定等でNotion API側から一部しか取得できていない失敗を疑うべきであり、
# 成功調のメッセージではなく警告として出力する（obasan-qualityレビューWARN対応、2026-08-17）。
_MIN_EXPECTED_SYNCED_COUNT = 100


def main() -> None:
    notion_client = HttpNotionClient(PROJECT_SCHEMA.key, PROJECT_SCHEMA.notion_database_id)
    user_directory = NotionUserDirectory()

    print("案件管理DB全件をProjectMirrorへバックフィルします...")
    result = refresh_all_projects(notion_client=notion_client, user_directory=user_directory)

    if result.get("skipped"):
        # 既に別プロセス（夜間reconciliation cron等）が実行中でロックを取得できなかった場合
        # （src/project_mirror/db.pyのtry_acquire_refresh_lock参照）。
        print(f"スキップしました: {result['skipped']}（既に別プロセスが実行中の可能性があります）")
        return

    synced_count = result["synced_count"]
    deleted_count = result["deleted_count"]
    if synced_count < _MIN_EXPECTED_SYNCED_COUNT:
        print(
            f"警告: synced_count={synced_count} deleted_count={deleted_count} "
            f"(想定件数{_MIN_EXPECTED_SYNCED_COUNT}件を下回っています。"
            "NOTION_API_KEYの権限不足・データベースID誤り等でNotion側から正しく全件取得できて"
            "いない可能性があります。成功として扱わず、原因を確認してください)"
        )
        return

    print(f"完了しました: synced_count={synced_count} deleted_count={deleted_count}")


if __name__ == "__main__":
    main()
