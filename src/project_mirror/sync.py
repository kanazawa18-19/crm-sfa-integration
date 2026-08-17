"""案件管理DB（Notion）→ ProjectMirror（Postgres）への同期処理本体（2026-08-17）。

データの正本は引き続きNotionであり、本モジュールは以下2つのエントリポイントを提供する。

- `sync_project_to_mirror()`: Notion Webhook経由の1件更新用
  （`src/sync_engine/webhook_handlers/notion_webhook.py`の`calendar_sync`/`lead_sync`と同じ
  拡張点パターン）。
- `refresh_all_projects()`: 初回バックフィル（`scripts/backfill_project_mirror.py`）・夜間
  reconciliation cronの両方から使う、案件管理DB全件のフル同期。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Protocol

from src.api.notion_display import project_page_to_mirror_record
from src.db_schema.project import PROJECT_SCHEMA
from src.project_mirror.db import (
    release_refresh_lock,
    try_acquire_refresh_lock,
    upsert_project,
    upsert_projects_and_sweep,
)
from src.sync_engine.webhook_handlers._common import parse_iso_datetime

logger = logging.getLogger(__name__)


class ProjectMirrorNotionClient(Protocol):
    """本モジュールが要求するNotionクライアントの最小インターフェース。"""

    def get_raw_page(self, page_id: str) -> Mapping[str, Any]: ...

    def query_all_pages(self) -> list[dict[str, Any]]: ...


def _page_to_mirror_row(page: Mapping[str, Any], *, user_directory: Any) -> dict[str, Any]:
    record, skipped = project_page_to_mirror_record(page, user_directory)
    if skipped:
        logger.warning(
            "project_mirror: db_key=%r スキーマに存在しない未定義プロパティをスキップしました: %s",
            PROJECT_SCHEMA.key,
            sorted(skipped),
        )
    last_edited_time = page.get("last_edited_time")
    return {
        "notion_page_id": record["notion_page_id"],
        "data": record,
        "last_edited_at": parse_iso_datetime(last_edited_time) if last_edited_time else None,
    }


def sync_project_to_mirror(
    properties: Mapping[str, Any],
    page_id: str,
    *,
    notion_client: ProjectMirrorNotionClient,
    user_directory: Any,
) -> None:
    """`notion_webhook.handler_with_proxy`の第3の副作用コールバック（db_key="project"の
    SyncEventについて呼ばれる）。

    `properties`（`SyncEvent.properties`相当）は`calendar_sync`/`lead_sync`と型を揃えるためだけ
    に受け取り、実際には使わない（書き込み可能プロパティのみに絞られておりFORMULA/ROLLUPが
    欠落するため）。`notion_client.get_raw_page(page_id)`でページ全体を再取得して変換する。

    例外はこの関数では握りつぶさない（`handler_with_proxy()`側が`calendar_sync`/`lead_sync`と
    同じtry/exceptで「Webhook全体としては失敗させない」判断を行う設計に合わせる）。
    """
    page = notion_client.get_raw_page(page_id)
    row = _page_to_mirror_row(page, user_directory=user_directory)
    upsert_project(row)


def refresh_all_projects(
    *, notion_client: ProjectMirrorNotionClient, user_directory: Any
) -> dict[str, Any]:
    """案件管理DB全件をミラーへ反映する（初回バックフィル・夜間reconciliation共通）。

    `notion_client.query_all_pages()`で全件取得してから変換し、`upsert_projects_and_sweep()`を
    1回呼ぶ。全件取得が完了するまでDB書き込みを開始しない（取得の途中で失敗した場合に、
    中途半端な件数でミラーをsweepしてしまう事故を避けるため）。

    実行開始時にPostgresアドバイザリロック（`pg_try_advisory_lock`）の取得を試み、既に別
    プロセスが実行中の場合は即座にスキップする（shirokuma-secレビューWARN対応、2026-08-17。
    夜間reconciliation cronと手動バックフィルスクリプトが偶発的に重なると、後から完了した
    方が古い実行の`syncedAt`で新しいデータを上書き・sweepしてしまう恐れがあるため）。
    """
    lock_conn = try_acquire_refresh_lock()
    if lock_conn is None:
        logger.warning(
            "refresh_all_projects: 既に別プロセスが実行中と判断したためスキップします"
            "（pg_try_advisory_lockの取得に失敗）"
        )
        return {"synced_count": 0, "deleted_count": 0, "skipped": "already_running"}
    try:
        pages = notion_client.query_all_pages()
        rows = [_page_to_mirror_row(page, user_directory=user_directory) for page in pages]
        deleted_count = upsert_projects_and_sweep(rows)
        return {"synced_count": len(rows), "deleted_count": deleted_count}
    finally:
        release_refresh_lock(lock_conn)
