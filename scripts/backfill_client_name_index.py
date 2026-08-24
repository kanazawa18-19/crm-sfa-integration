#!/usr/bin/env python3
"""取引先マスターDB(Notion)の正規化取引先名→Notion page IDインデックス(ClientNameIndex)を
初回バックフィルするスクリプト(2026-08-25)。

`src/relation_sync/sync.py`の`refresh_all_client_names()`（初回バックフィル・夜間
reconciliation cron共通のフル同期処理）を呼ぶだけの薄いCLIラッパー
（`scripts/backfill_project_mirror.py`と同じパターン）。読み取り専用(Notion API)+
自インデックスへのUPSERT/DELETEのみで、他システムへの書き込み・Notion本番データの破壊
リスクが無いため、`scripts/backfill_project_assignees.py`のようなdry-run優先パターンは
採用していない。

`RELATION_SYNC_ENABLED`環境変数のチェックは行わない（`src/sync_engine/production_wiring.py`の
`build_client_name_index_sync_callable`はWebhook/夜間cronの自動発火を制御するためのガードで
あり、本スクリプトのように人が明示的に実行するバックフィルには適用しない設計
（`scripts/backfill_project_mirror.py`が`PROJECT_MIRROR_SYNC_ENABLED`を見ないのと同じ）。

使い方:
    python scripts/backfill_client_name_index.py

実行には環境変数 NOTION_API_KEY（取引先マスターDBの読み取り）・DATABASE_URL
（ClientNameIndexの書き込み先Neon Postgres）が必要。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db_schema.client_master import CLIENT_MASTER_SCHEMA
from src.relation_sync.sync import refresh_all_client_names
from src.sync_engine.clients.notion_client import HttpNotionClient

# 取引先マスターDBは約1万件規模（2026-08-10のZoho/Notion突合調査時点で9,914件、
# `src/migration/notion_dedupe.py`のモジュールdocstring参照）。synced_countがこの件数を
# 大幅に下回る場合、権限不足・フィルタ誤設定等でNotion API側から一部しか取得できていない
# 失敗を疑うべきであり、成功調のメッセージではなく警告として出力する
# （`scripts/backfill_project_mirror.py`と同じ方針）。
_MIN_EXPECTED_SYNCED_COUNT = 100


def main() -> None:
    notion_client = HttpNotionClient(
        CLIENT_MASTER_SCHEMA.key, CLIENT_MASTER_SCHEMA.notion_database_id
    )

    print("取引先マスターDB全件をClientNameIndexへバックフィルします...")
    result = refresh_all_client_names(notion_client=notion_client)

    if result.get("skipped"):
        # 既に別プロセス（夜間reconciliation cron等）が実行中でロックを取得できなかった場合、
        # または部分取得の疑いによりsweepを中止した場合
        # （src/relation_sync/db.pyのtry_acquire_refresh_lock、src/relation_sync/sync.pyの
        # _MIN_SYNC_RATIOガード参照）。
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
