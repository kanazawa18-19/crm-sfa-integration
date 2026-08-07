from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from src.api.task_service import build_tasks, reset_cache


@pytest.fixture(autouse=True)
def _reset_task_cache() -> None:
    # build_tasks()は常にTaskDataSource経由でTTLキャッシュ（_cached）を通るため、
    # notion_clientを差し替えるテスト間でキャッシュが汚染されないよう毎回リセットする。
    reset_cache()
    yield
    reset_cache()


def _page(
    *,
    page_id: str = "task-1",
    title: str = "サンプルタスク",
    status: str | None = "未着手",
    due_date: str | None = None,
    assignees: list[dict[str, Any]] | None = None,
    ball: list[dict[str, Any]] | None = None,
    category: list[str] | None = None,
    tags: list[str] | None = None,
    project_relation: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "名前": {"type": "title", "title": [{"plain_text": title}]},
            "ステータス": (
                {"type": "status", "status": {"name": status}}
                if status is not None
                else {"type": "status", "status": None}
            ),
            "期限": {"type": "date", "date": ({"start": due_date} if due_date else None)},
            "担当者": {
                "type": "people",
                "people": [
                    {"id": p.get("id"), "name": p.get("name")} for p in (assignees or [])
                ],
            },
            "ボール": {
                "type": "people",
                "people": [{"id": p.get("id"), "name": p.get("name")} for p in (ball or [])],
            },
            "タスクカテゴリ": {
                "type": "multi_select",
                "multi_select": [{"name": c} for c in (category or [])],
            },
            "タグ": {"type": "multi_select", "multi_select": [{"name": t} for t in (tags or [])]},
            "🤝 案件管理": {
                "type": "relation",
                "relation": [{"id": r} for r in (project_relation or [])],
            },
        },
    }


class _FakeNotionClient:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def query_all_pages(self) -> list[dict[str, Any]]:
        return self._pages


# --- 完了タスクの除外 -----------------------------------------------------------------------


def test_build_tasks_excludes_completed_tasks() -> None:
    pages = [
        _page(page_id="t1", status="完了"),
        _page(page_id="t2", status="未着手"),
    ]
    result = build_tasks(as_of=date(2026, 8, 5), notion_client=_FakeNotionClient(pages))

    assert [t["notion_page_id"] for t in result["tasks"]] == ["t2"]
    assert result["total_count"] == 1


# --- 期限超過の判定 -------------------------------------------------------------------------


def test_build_tasks_marks_past_due_date_as_overdue() -> None:
    pages = [_page(page_id="t1", due_date="2023-08-21")]
    result = build_tasks(as_of=date(2026, 8, 5), notion_client=_FakeNotionClient(pages))

    assert result["tasks"][0]["is_overdue"] is True
    assert result["overdue_count"] == 1


def test_build_tasks_marks_future_due_date_as_not_overdue() -> None:
    pages = [_page(page_id="t1", due_date="2027-01-01")]
    result = build_tasks(as_of=date(2026, 8, 5), notion_client=_FakeNotionClient(pages))

    assert result["tasks"][0]["is_overdue"] is False
    assert result["overdue_count"] == 0


def test_build_tasks_marks_today_due_date_as_not_overdue() -> None:
    pages = [_page(page_id="t1", due_date="2026-08-05")]
    result = build_tasks(as_of=date(2026, 8, 5), notion_client=_FakeNotionClient(pages))

    assert result["tasks"][0]["is_overdue"] is False


def test_build_tasks_missing_due_date_is_not_overdue() -> None:
    pages = [_page(page_id="t1", due_date=None)]
    result = build_tasks(as_of=date(2026, 8, 5), notion_client=_FakeNotionClient(pages))

    assert result["tasks"][0]["is_overdue"] is False
    assert result["tasks"][0]["due_date"] is None


# --- ソート順: 期限超過 → 期限あり(未超過) → 期限なし ------------------------------------------


def test_build_tasks_sorts_overdue_first_then_upcoming_then_no_due_date() -> None:
    pages = [
        _page(page_id="no-due", due_date=None),
        _page(page_id="upcoming-late", due_date="2027-02-01"),
        _page(page_id="overdue-late", due_date="2023-08-21"),
        _page(page_id="upcoming-early", due_date="2026-09-01"),
        _page(page_id="overdue-early", due_date="2020-01-01"),
    ]
    result = build_tasks(as_of=date(2026, 8, 5), notion_client=_FakeNotionClient(pages))

    assert [t["notion_page_id"] for t in result["tasks"]] == [
        "overdue-early",
        "overdue-late",
        "upcoming-early",
        "upcoming-late",
        "no-due",
    ]


# --- タイトル切り詰め ------------------------------------------------------------------------


def test_build_tasks_truncates_long_title() -> None:
    long_title = "あ" * 100
    pages = [_page(page_id="t1", title=long_title)]
    result = build_tasks(as_of=date(2026, 8, 5), notion_client=_FakeNotionClient(pages))

    summary = result["tasks"][0]["title_summary"]
    assert len(summary) == 41  # 40文字 + 省略記号
    assert summary.startswith("あ" * 40)


def test_build_tasks_keeps_short_title_unchanged() -> None:
    pages = [_page(page_id="t1", title="短いタスク")]
    result = build_tasks(as_of=date(2026, 8, 5), notion_client=_FakeNotionClient(pages))

    assert result["tasks"][0]["title_summary"] == "短いタスク"


# --- 担当者・ボール・カテゴリ・タグ・案件紐付け ---------------------------------------------------


def test_build_tasks_parses_assignees_ball_category_tags_and_project_link() -> None:
    pages = [
        _page(
            page_id="t1",
            assignees=[{"id": "u1", "name": "田中太郎"}],
            ball=[{"id": "u2", "name": "鈴木花子"}],
            category=["案件対応"],
            tags=["緊急"],
            project_relation=["proj-1"],
        )
    ]
    result = build_tasks(as_of=date(2026, 8, 5), notion_client=_FakeNotionClient(pages))

    task = result["tasks"][0]
    assert task["assignees"] == ["田中太郎"]
    assert task["ball"] == ["鈴木花子"]
    assert task["category"] == ["案件対応"]
    assert task["tags"] == ["緊急"]
    assert task["has_project_link"] is True


def test_build_tasks_has_project_link_false_when_relation_empty() -> None:
    pages = [_page(page_id="t1", project_relation=[])]
    result = build_tasks(as_of=date(2026, 8, 5), notion_client=_FakeNotionClient(pages))

    assert result["tasks"][0]["has_project_link"] is False


def test_build_tasks_assignee_falls_back_to_id_when_name_missing() -> None:
    pages = [_page(page_id="t1", assignees=[{"id": "u1", "name": None}])]
    result = build_tasks(as_of=date(2026, 8, 5), notion_client=_FakeNotionClient(pages))

    assert result["tasks"][0]["assignees"] == ["u1"]


# --- as_of省略時のデフォルト -----------------------------------------------------------------


def test_build_tasks_uses_today_when_as_of_omitted() -> None:
    result = build_tasks(notion_client=_FakeNotionClient([]))

    assert "as_of" in result
    assert result["tasks"] == []
    assert result["total_count"] == 0
