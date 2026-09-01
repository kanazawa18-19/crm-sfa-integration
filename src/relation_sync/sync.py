"""取引先マスターDB（Notion）→ ClientNameIndex（Postgres）への同期処理本体（2026-08-25）。

データの正本は引き続きNotionであり、本モジュールは`src/project_mirror/sync.py`と同じ
2つのエントリポイントを提供する。

- `sync_client_name_to_index()`: Notion Webhook経由の1件更新用。
- `refresh_all_client_names()`: 初回バックフィル・夜間reconciliation cronの両方から使う、
  取引先マスターDB全件のフル同期。

いずれも`src/relation_sync/resolve.py`（kintone等からのリレーション解決）が参照する
`ClientNameIndex`テーブルを最新化するためだけのものであり、双方向同期パイプライン
（Dispatcher/SyncEvent/IdMappingStore）には一切関与しない。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Protocol

import requests

from src.migration.zoho_client_master import normalize_company_name_strong
import dataclasses

from src.sync_engine.clients._notion_paging import query_keyset_slice
from src.sync_engine.sync_cursor import SyncCursor, clear_cursor, load_cursor, save_cursor
from src.relation_sync.db import (
    get_client_name_count,
    release_refresh_lock,
    try_acquire_refresh_lock,
    upsert_client_name,
    sweep_client_names,
    upsert_client_names,
    upsert_client_names_and_sweep,
)
from src.sync_engine.clients.notion_client import parse_notion_property_value

logger = logging.getLogger(__name__)

# 新規取得件数が既存インデックス件数のこの割合を下回った場合、部分取得の疑いが強いとして
# sweepを中止する(project_mirror/sync.pyの_MIN_SYNC_RATIOと同じ安全装置)。
_MIN_SYNC_RATIO = 0.5

# 取引先マスターDBのtitleプロパティ名(src/db_schema/client_master.py参照)。
_TITLE_PROPERTY_NAME = "取引先名"


class ClientMasterNotionClient(Protocol):
    """本モジュールが要求するNotionクライアントの最小インターフェース
    (`src.project_mirror.sync.ProjectMirrorNotionClient`と同じ形)。"""

    def get_raw_page(self, page_id: str) -> Mapping[str, Any]: ...

    def query_all_pages(self) -> list[dict[str, Any]]: ...

    def query_raw(self, body: dict[str, Any]) -> dict[str, Any]:
        """Database Query を1回だけ叩く（分割実行から使う、2026-09-01）。"""
        ...


def _page_to_index_row(page: Mapping[str, Any]) -> dict[str, Any] | None:
    """取引先マスターDBの1ページ(Notion API生JSON)を、ClientNameIndexの1行へ変換する。

    titleプロパティ("取引先名")が空の場合はNoneを返す(`src.migration.notion_dedupe.
    fetch_client_master_snapshots()`と同じ扱い。titleは必須プロパティのため通常は
    発生しないが、万一空だった場合に空文字列を正規化キーとしてインデックス化しても
    検索用途としては意味を持たないため)。
    """
    props = page.get("properties") or {}
    title_prop = props.get(_TITLE_PROPERTY_NAME)
    title = parse_notion_property_value(title_prop) if title_prop else None
    if not title:
        logger.warning(
            "relation_sync: page_id=%r の取引先名(title)が空のためClientNameIndexへの反映を"
            "スキップします",
            page.get("id"),
        )
        return None
    return {
        "notion_page_id": page["id"],
        "normalized_name": normalize_company_name_strong(title),
        "raw_name": title,
    }


def sync_client_name_to_index(
    properties: Mapping[str, Any], page_id: str, *, notion_client: ClientMasterNotionClient
) -> None:
    """取引先マスターDBの1ページをClientNameIndexへ反映する(Notion Webhook経由)。

    `properties`（`SyncEvent.properties`相当）は`sync_project_to_mirror()`と型を揃えるためだけ
    に受け取り、実際には使わない（`notion_webhook.handler_with_proxy`の`project_mirror_sync`と
    同じ`Callable[[Mapping[str, Any], str], Any]`シグネチャに合わせている）。
    `notion_client.get_raw_page(page_id)`でページ全体を取得して変換する。
    例外はこの関数では握りつぶさない(呼び出し元がWebhook全体を失敗させない判断を行う)。
    """
    page = notion_client.get_raw_page(page_id)
    row = _page_to_index_row(page)
    if row is None:
        return
    upsert_client_name(row)


def refresh_all_client_names(
    *, notion_client: ClientMasterNotionClient
) -> dict[str, Any]:
    """取引先マスターDB全件をインデックスへ反映する(初回バックフィル・夜間reconciliation共通)。

    `notion_client.query_all_pages()`で全件取得してから変換し、
    `upsert_client_names_and_sweep()`を1回呼ぶ。全件取得が完了するまでDB書き込みを開始しない
    (`refresh_all_projects()`と同じ設計)。

    実行開始時にPostgresアドバイザリロックの取得を試み、既に別プロセスが実行中の場合は
    即座にスキップする(project_mirror/sync.pyのrefresh_all_projects()と同じ多重実行防止)。
    """
    lock_conn = try_acquire_refresh_lock()
    if lock_conn is None:
        logger.warning(
            "refresh_all_client_names: 既に別プロセスが実行中と判断したためスキップします"
            "（pg_try_advisory_lockの取得に失敗）"
        )
        return {"synced_count": 0, "deleted_count": 0, "skipped": "already_running"}
    try:
        pages = notion_client.query_all_pages()
        rows = [row for row in (_page_to_index_row(page) for page in pages) if row is not None]

        # `query_all_pages()`側のページング中断（部分取得）に対する保護
        # (project_mirror/sync.pyのrefresh_all_projects()と同じ安全装置)。
        current_count = get_client_name_count()
        if current_count >= 20 and len(rows) < current_count * _MIN_SYNC_RATIO:
            message = (
                f"refresh_all_client_names: 新規取得件数({len(rows)}件)が既存インデックス件数"
                f"({current_count}件)より大幅に少ないため、部分取得の疑いがありsweepを"
                "中止しました（既存データは変更していません）。"
            )
            logger.error(message)
            _notify_slack_alert(message)
            return {
                "synced_count": len(rows),
                "deleted_count": 0,
                "skipped": "suspected_partial_fetch",
            }

        deleted_count = upsert_client_names_and_sweep(rows)
        return {"synced_count": len(rows), "deleted_count": deleted_count}
    finally:
        release_refresh_lock(lock_conn)


#: このしおりの名前（`SyncCursor`テーブルのキー）。
CURSOR_NAME = "client_name_index"

#: 1回の実行に使ってよい秒数。Vercelの実行上限は300秒なので余裕を持たせる。
DEFAULT_TIME_BUDGET_SECONDS = 200.0

#: 1周で取る件数。中断の粒度になる（小さいほど時間予算を守りやすい）。
_ROUND_LIMIT = 2_000


def refresh_client_names_incrementally(
    *,
    notion_client: ClientMasterNotionClient,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
) -> dict[str, Any]:
    """取引先マスターを**何回かに分けて**インデックスへ反映する（2026-09-01）。

    ■ なぜ分けるのか

    取引先マスターDBは **102,799件** あり、全件取得だけで約18分かかる。
    Vercelの実行上限は300秒なので、1回では終わらない。
    **「1万件で静かに切れる」を直したら、今度は「時間切れで何もしない」になる。**

    そこで時間予算で区切って中断し、しおり（`SyncCursor`）に「どこまで取ったか」を
    残す。次の実行が続きから再開し、何回かで一巡する。

    ■ 掃除は一巡し終えたときだけ

    掃除は「今回見なかった行を消す」やり方。途中で掃除すると**まだ見ていないだけの行を
    消してしまう**（ProjectMirrorを全消失させた事故と同じ形）。
    一巡を始めた時刻を覚えておき、一巡し終えたときだけそれより古い行を消す。
    """
    lock_conn = try_acquire_refresh_lock()
    if lock_conn is None:
        logger.warning(
            "refresh_client_names_incrementally: 既に別プロセスが実行中と判断したため"
            "スキップします（pg_try_advisory_lockの取得に失敗）"
        )
        return {"synced_count": 0, "deleted_count": 0, "skipped": "already_running"}
    try:
        cursor = load_cursor(CURSOR_NAME)

        def _post(body: dict[str, Any]) -> dict[str, Any]:
            return notion_client.query_raw(body)

        slice_ = query_keyset_slice(
            _post,
            watermark=cursor.watermark,
            round_limit=_ROUND_LIMIT,
            time_budget_seconds=time_budget_seconds,
            label=CURSOR_NAME,
        )
        rows = [
            row for row in (_page_to_index_row(page) for page in slice_.pages) if row is not None
        ]
        upsert_client_names(rows, synced_at=cursor.pass_started_at)

        if not slice_.completed:
            save_cursor(dataclasses.replace(cursor, watermark=slice_.watermark))
            logger.info(
                "refresh_client_names_incrementally: 途中まで取り込みました"
                "（今回%d件。次回このしおりから続けます）",
                len(rows),
            )
            return {"synced_count": len(rows), "deleted_count": 0, "completed": False}

        # 一巡し終えた。ここで初めて掃除する。
        deleted_count = sweep_client_names(before=cursor.pass_started_at)
        clear_cursor(CURSOR_NAME)
        logger.info(
            "refresh_client_names_incrementally: 一巡し終えました（今回%d件 / 掃除%d件）",
            len(rows),
            deleted_count,
        )
        return {"synced_count": len(rows), "deleted_count": deleted_count, "completed": True}
    finally:
        release_refresh_lock(lock_conn)


def _notify_slack_alert(message: str) -> None:
    """`src/project_mirror/sync.py`の`_notify_slack_alert()`と同じ`SLACK_WEBHOOK_URL_ALERT`
    (運用アラートチャンネル)へ通知する。送信失敗はログのみで握りつぶす。"""
    url = os.environ.get("SLACK_WEBHOOK_URL_ALERT")
    if not url:
        return
    try:
        requests.post(url, json={"text": message}, timeout=10)
    except Exception:
        logger.exception("refresh_all_client_names: failed to post alert to slack")
