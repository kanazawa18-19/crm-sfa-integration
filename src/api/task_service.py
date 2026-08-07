"""タスク一覧（Notion「todo」DB）のオーケストレーション層。

「todo」DB（database_id=`86f4b46c-714b-48e4-9327-62f7ef73887f`）は既存のAny-to-Any同期の
対象範囲を広げないため`src/db_schema/registry.py`には登録しない（`src/calendar_sync/`を
独立させた設計判断と同じ考え方）。そのため`src/api/dashboard_service.py`が使う
`page_to_display_dict`（スキーマ必須）は使わず、本モジュール専用の軽量なパーサーで
必要なプロパティのみを読む。

Notion APIから全タスクページを取得する処理には`HttpNotionClient.query_all_pages()`を
そのまま使う（`db_key`引数は形式的な値を渡すのみで、`query_all_pages()`は内部で
`self._schema`（`get_schema(db_key)`経由）を一切参照しないため、schema未登録でも
問題なく動作する。`create_page`/`update_page`のみが`self._schema`を参照するが、
本モジュールはそれらを呼ばない）。
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from src.api.notion_display import parse_notion_property_for_display
from src.api.user_directory import NotionUserDirectory
from src.sync_engine.clients.notion_client import HttpNotionClient

_JST = timezone(timedelta(hours=9))

TASK_DB_ID = "86f4b46c-714b-48e4-9327-62f7ef73887f"

# 「todo」DBのプロパティ名（実データ調査結果に準拠）。
PROP_名前 = "名前"
PROP_ステータス = "ステータス"
PROP_期限 = "期限"
PROP_担当者 = "担当者"
PROP_ボール = "ボール"
PROP_タスクカテゴリ = "タスクカテゴリ"
PROP_タグ = "タグ"
PROP_案件管理 = "🤝 案件管理"

STATUS_完了 = "完了"

TITLE_SUMMARY_MAX_LENGTH = 40

_CACHE_TTL_ENV_VAR = "DASHBOARD_CACHE_TTL_SECONDS"
_DEFAULT_CACHE_TTL_SECONDS = 60.0

# `dashboard_service._module_cache`とは独立したtask_service専用のプロセス内・TTLベースの
# 簡易キャッシュ。`notion_client`を明示的に注入するテストはこのキャッシュを経由しない。
_module_cache: dict[str, tuple[float, Any]] = {}


def _cache_ttl_seconds() -> float:
    raw = os.environ.get(_CACHE_TTL_ENV_VAR)
    if not raw:
        return _DEFAULT_CACHE_TTL_SECONDS
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_CACHE_TTL_SECONDS


def _cached(key: str, fetch: Callable[[], Any]) -> Any:
    now = time.monotonic()
    cached = _module_cache.get(key)
    if cached is not None and now - cached[0] < _cache_ttl_seconds():
        return cached[1]
    value = fetch()
    _module_cache[key] = (now, value)
    return value


def reset_cache() -> None:
    """モジュールレベルキャッシュを明示的にクリアする（テスト用）。"""
    _module_cache.clear()


def _today_jst() -> date:
    return datetime.now(_JST).date()


def _parse_property(properties: dict[str, Any], name: str) -> Any:
    value = properties.get(name)
    if value is None:
        return None
    return parse_notion_property_for_display(value)


def _resolve_person_name(person: Any, user_directory: Any) -> str | None:
    """`parse_notion_property_for_display`が返す`{"id":..., "name":...}`形式1件を表示名へ変換する。

    `dashboard_service._resolve_person_name`と同じ考え方（ページに埋め込まれた`name`を
    最優先し、欠落時のみ`NotionUserDirectory`でID解決、それでも解決できなければ
    生UUIDではなく人間が読めるプレースホルダーにする）。
    """
    if not isinstance(person, dict):
        return None
    name = person.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    person_id = person.get("id")
    if not person_id:
        return None
    resolved = user_directory.resolve(str(person_id))
    if resolved == str(person_id):
        return f"不明なメンバー（{str(person_id)[:8]}）"
    return resolved


def _person_display_names(people: Any, user_directory: Any) -> list[str]:
    if not isinstance(people, list):
        return []
    names = [_resolve_person_name(person, user_directory) for person in people]
    return [name for name in names if name]


def _parse_due_date(value: Any) -> date | None:
    """`date`プロパティ表示値（"2026-08-05"や"2026-08-05T09:00:00.000+09:00"等）から
    日付部分のみを取り出す。"""
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _truncate_title(title: str | None) -> str:
    if not title:
        return ""
    stripped = title.strip()
    if len(stripped) <= TITLE_SUMMARY_MAX_LENGTH:
        return stripped
    return stripped[:TITLE_SUMMARY_MAX_LENGTH] + "…"


def _page_to_task_dict(page: dict[str, Any], user_directory: Any) -> dict[str, Any]:
    properties = page.get("properties") or {}
    return {
        "notion_page_id": page["id"],
        "title": _parse_property(properties, PROP_名前),
        "status": _parse_property(properties, PROP_ステータス),
        "due_date": _parse_due_date(_parse_property(properties, PROP_期限)),
        "assignees": _person_display_names(
            _parse_property(properties, PROP_担当者), user_directory
        ),
        "ball": _person_display_names(_parse_property(properties, PROP_ボール), user_directory),
        "category": _parse_property(properties, PROP_タスクカテゴリ) or [],
        "tags": _parse_property(properties, PROP_タグ) or [],
        "has_project_link": bool(_parse_property(properties, PROP_案件管理)),
    }


class TaskDataSource:
    """「todo」DBのNotionページを表示用dictへ変換して取得するデータソース。

    `notion_client`（`query_all_pages()`を持つオブジェクト）・`user_directory`
    （`resolve`を持つオブジェクト）を注入できる（未指定時は実際のNotion APIを叩く
    `HttpNotionClient`/`NotionUserDirectory`を使う）。テストではフェイク実装に差し替える。
    """

    def __init__(
        self, *, notion_client: Any | None = None, user_directory: Any | None = None
    ) -> None:
        self._notion_client = notion_client or HttpNotionClient("task", TASK_DB_ID)
        self._user_directory = user_directory or NotionUserDirectory()

    def get_tasks(self) -> list[dict[str, Any]]:
        return _cached("tasks", self._fetch_tasks)

    def _fetch_tasks(self) -> list[dict[str, Any]]:
        pages = self._notion_client.query_all_pages()
        return [_page_to_task_dict(page, self._user_directory) for page in pages]


def _sort_key(task: dict[str, Any]) -> tuple[int, str]:
    """期限超過→期限あり（未超過）→期限なし、の順に並べ、各グループ内は期限昇順にする。"""
    due_date = task["due_date"]
    if due_date is None:
        return (2, "")
    if task["is_overdue"]:
        return (0, due_date.isoformat())
    return (1, due_date.isoformat())


def build_tasks(
    *,
    as_of: date | None = None,
    notion_client: Any | None = None,
    user_directory: Any | None = None,
) -> dict[str, Any]:
    """未完了タスクを、期限超過を先頭にしたソート順で返す。"""
    resolved_as_of = as_of or _today_jst()
    source = TaskDataSource(notion_client=notion_client, user_directory=user_directory)
    tasks = [task for task in source.get_tasks() if task["status"] != STATUS_完了]

    for task in tasks:
        due_date = task["due_date"]
        task["is_overdue"] = due_date is not None and due_date < resolved_as_of

    tasks.sort(key=_sort_key)

    result_tasks = [
        {
            "notion_page_id": task["notion_page_id"],
            "title_summary": _truncate_title(task["title"]),
            "status": task["status"],
            "due_date": task["due_date"].isoformat() if task["due_date"] else None,
            "is_overdue": task["is_overdue"],
            "assignees": task["assignees"],
            "ball": task["ball"],
            "category": task["category"],
            "tags": task["tags"],
            "has_project_link": task["has_project_link"],
        }
        for task in tasks
    ]

    return {
        "as_of": resolved_as_of.isoformat(),
        "tasks": result_tasks,
        "overdue_count": sum(1 for task in result_tasks if task["is_overdue"]),
        "total_count": len(result_tasks),
    }
