from __future__ import annotations

from typing import Any

import pytest

from scripts.backfill_project_assignees import (
    build_name_to_user_ids,
    normalize_name,
    plan_backfill,
    print_summary,
)


def _page(
    page_id: str,
    *,
    project_name: str = "サンプル案件",
    assignees: list[dict[str, Any]] | None = None,
    assignee_name: str | None = None,
) -> dict[str, Any]:
    """案件管理DBの生Notionページオブジェクトを模す（担当メンバー=people型、
    担当者名=rich_text型、案件名=title型のみを含む最小構成）。"""
    return {
        "id": page_id,
        "properties": {
            "案件名": {"type": "title", "title": [{"plain_text": project_name}]},
            "担当メンバー": {"type": "people", "people": assignees or []},
            "担当者名": {
                "type": "rich_text",
                "rich_text": (
                    [{"plain_text": assignee_name}] if assignee_name is not None else []
                ),
            },
        },
    }


class _FakeUserDirectory:
    def __init__(self, names_by_id: dict[str, str]) -> None:
        self._names_by_id = names_by_id

    def all_names_by_id(self) -> dict[str, str]:
        return dict(self._names_by_id)


# --- normalize_name --------------------------------------------------------------------------


def test_normalize_name_strips_surrounding_whitespace() -> None:
    assert normalize_name("  田中太郎  ") == "田中太郎"


def test_normalize_name_unifies_fullwidth_and_halfwidth() -> None:
    # 全角英数のTanaka Taroと半角英数のTanaka Taroが同一キーになる（NFKC正規化）。
    assert normalize_name("Ｔａｎａｋａ") == normalize_name("Tanaka")


# --- build_name_to_user_ids -------------------------------------------------------------------


def test_build_name_to_user_ids_maps_normalized_name_to_id() -> None:
    directory = _FakeUserDirectory({"user-1": "田中太郎"})

    mapping = build_name_to_user_ids(directory)

    assert mapping[normalize_name("田中太郎")] == ["user-1"]


def test_build_name_to_user_ids_groups_homonyms_into_same_key() -> None:
    directory = _FakeUserDirectory({"user-1": "田中太郎", "user-2": "田中太郎"})

    mapping = build_name_to_user_ids(directory)

    assert sorted(mapping[normalize_name("田中太郎")]) == ["user-1", "user-2"]


# --- plan_backfill: 対象抽出 ------------------------------------------------------------------


def test_plan_backfill_skips_pages_with_existing_assignee() -> None:
    pages = [
        _page(
            "p1",
            assignees=[{"id": "user-1", "name": "田中太郎"}],
            assignee_name="田中太郎",
        )
    ]
    name_to_user_ids = {normalize_name("田中太郎"): ["user-1"]}

    plan = plan_backfill(pages, name_to_user_ids)

    assert plan.auto_assign == []
    assert plan.needs_review == []


def test_plan_backfill_skips_pages_with_no_assignee_name() -> None:
    pages = [_page("p1", assignee_name=None)]

    plan = plan_backfill(pages, {})

    assert plan.auto_assign == []
    assert plan.needs_review == []


def test_plan_backfill_skips_pages_with_whitespace_only_assignee_name() -> None:
    pages = [_page("p1", assignee_name="   ")]

    plan = plan_backfill(pages, {})

    assert plan.auto_assign == []
    assert plan.needs_review == []


# --- plan_backfill: 完全一致・確定1名 -----------------------------------------------------------


def test_plan_backfill_auto_assigns_when_single_unambiguous_match() -> None:
    pages = [_page("p1", project_name="A社案件", assignee_name="田中太郎")]
    name_to_user_ids = {normalize_name("田中太郎"): ["user-1"]}

    plan = plan_backfill(pages, name_to_user_ids)

    assert len(plan.auto_assign) == 1
    candidate = plan.auto_assign[0]
    assert candidate.page_id == "p1"
    assert candidate.project_name == "A社案件"
    assert candidate.resolved_user_id == "user-1"
    assert candidate.resolved_user_name == "田中太郎"
    assert plan.needs_review == []


def test_plan_backfill_auto_assigns_with_fullwidth_halfwidth_normalization() -> None:
    pages = [_page("p1", assignee_name="Ｔａｎａｋａ")]
    name_to_user_ids = {normalize_name("Tanaka"): ["user-1"]}

    plan = plan_backfill(pages, name_to_user_ids)

    assert len(plan.auto_assign) == 1
    assert plan.auto_assign[0].resolved_user_id == "user-1"


def test_plan_backfill_auto_assigns_with_surrounding_whitespace_normalization() -> None:
    pages = [_page("p1", assignee_name="  田中太郎  ")]
    name_to_user_ids = {normalize_name("田中太郎"): ["user-1"]}

    plan = plan_backfill(pages, name_to_user_ids)

    assert len(plan.auto_assign) == 1
    assert plan.auto_assign[0].resolved_user_id == "user-1"


# --- plan_backfill: 候補0件・複数件はレビュー行き --------------------------------------------------


def test_plan_backfill_needs_review_when_no_candidate_found() -> None:
    pages = [_page("p1", assignee_name="存在しない氏名")]

    plan = plan_backfill(pages, {})

    assert plan.auto_assign == []
    assert len(plan.needs_review) == 1
    assert "見つかりません" in plan.needs_review[0].reason


def test_plan_backfill_needs_review_when_multiple_candidates_found() -> None:
    pages = [_page("p1", assignee_name="田中太郎")]
    name_to_user_ids = {normalize_name("田中太郎"): ["user-1", "user-2"]}

    plan = plan_backfill(pages, name_to_user_ids)

    assert plan.auto_assign == []
    assert len(plan.needs_review) == 1
    assert "同姓同名" in plan.needs_review[0].reason


# --- plan_backfill: 担当者名に複数人分の氏名が入っているケース ------------------------------------


def test_plan_backfill_needs_review_when_assignee_name_contains_multiple_names() -> None:
    pages = [_page("p1", assignee_name="田中太郎、鈴木花子")]
    name_to_user_ids = {
        normalize_name("田中太郎"): ["user-1"],
        normalize_name("鈴木花子"): ["user-2"],
    }

    plan = plan_backfill(pages, name_to_user_ids)

    assert plan.auto_assign == []
    assert len(plan.needs_review) == 1
    assert "複数名" in plan.needs_review[0].reason


@pytest.mark.parametrize("delimiter", [",", "，", "、"])
def test_plan_backfill_treats_comma_and_related_delimiters_as_multiple_names(delimiter: str) -> None:
    pages = [_page("p1", assignee_name=f"田中太郎{delimiter}鈴木花子")]

    plan = plan_backfill(pages, {})

    assert plan.auto_assign == []
    assert len(plan.needs_review) == 1


# --- plan_backfill: 複数ページ混在 --------------------------------------------------------------


def test_plan_backfill_classifies_multiple_pages_independently() -> None:
    pages = [
        _page("p1", assignee_name="田中太郎"),  # 自動割当
        _page("p2", assignee_name="存在しない氏名"),  # レビュー行き（候補0件）
        _page("p3", assignees=[{"id": "user-2", "name": "鈴木花子"}]),  # 既に設定済み、対象外
        _page("p4", assignee_name=None),  # 担当者名も空、対象外
    ]
    name_to_user_ids = {normalize_name("田中太郎"): ["user-1"]}

    plan = plan_backfill(pages, name_to_user_ids)

    assert [c.page_id for c in plan.auto_assign] == ["p1"]
    assert [r.page_id for r in plan.needs_review] == ["p2"]


# --- print_summary: dry-run出力フォーマット -------------------------------------------------------


def test_print_summary_dry_run_shows_would_assign_verb(capsys: pytest.CaptureFixture[str]) -> None:
    pages = [_page("p1", assignee_name="田中太郎")]
    name_to_user_ids = {normalize_name("田中太郎"): ["user-1"]}
    plan = plan_backfill(pages, name_to_user_ids)

    print_summary(plan, total_pages=1, dry_run=True)

    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "自動割当予定: 1件" in out
    assert "案件管理DB全件: 1件" in out
    assert "対象（担当メンバー未設定 かつ 担当者名あり）: 1件" in out


def test_print_summary_execute_shows_assigned_verb(capsys: pytest.CaptureFixture[str]) -> None:
    pages = [_page("p1", assignee_name="田中太郎")]
    name_to_user_ids = {normalize_name("田中太郎"): ["user-1"]}
    plan = plan_backfill(pages, name_to_user_ids)

    print_summary(plan, total_pages=1, dry_run=False)

    out = capsys.readouterr().out
    assert "本番実行" in out
    assert "自動割当: 1件" in out


def test_print_summary_reports_needs_review_count_and_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pages = [_page("p1", project_name="B社案件", assignee_name="存在しない氏名")]
    plan = plan_backfill(pages, {})

    print_summary(plan, total_pages=1, dry_run=True)

    out = capsys.readouterr().out
    assert "レビュー行き（自動判定できず）: 1件" in out
    assert "B社案件" in out
    assert "見つかりません" in out
