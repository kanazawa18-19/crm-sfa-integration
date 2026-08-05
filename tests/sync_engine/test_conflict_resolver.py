"""05_同期・競合制御【コンフリクト判定フロー】の全分岐を検証する。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.db_schema.base import Tool
from src.sync_engine.conflict_resolver import (
    ResolutionAction,
    ToolValue,
    is_important_property,
    load_important_properties,
    resolve_conflict,
)

T0 = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)


def _tv(tool: Tool, value: object, *, minutes_after_t0: int) -> ToolValue:
    return ToolValue(tool=tool, value=value, updated_at=T0 + timedelta(minutes=minutes_after_t0))


# --- 1. 各ツールの値が一致しているか？ -> YES: 処理なし ------------------------------


def test_no_op_when_all_values_equal() -> None:
    candidates = [
        _tv(Tool.NOTION, "提案中", minutes_after_t0=0),
        _tv(Tool.KINTONE, "提案中", minutes_after_t0=10),
        _tv(Tool.SPREADSHEET, "提案中", minutes_after_t0=20),
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", candidates, db_key="project", detected_at=T0
    )

    assert result.action == ResolutionAction.NO_OP
    assert result.target_tools == frozenset()
    assert result.rejected == ()
    assert not result.notify_slack


# --- 2. 一方が空欄か？ -> YES ----------------------------------------------------------


def test_propagate_value_when_value_addition_is_newer() -> None:
    """片方が空欄・値を追記した側が新しい -> 他方へ値を補完する。"""
    candidates = [
        _tv(Tool.NOTION, "", minutes_after_t0=0),
        _tv(Tool.KINTONE, "500000", minutes_after_t0=10),
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "初期費用（イニシャル）", candidates, db_key="project", detected_at=T0
    )

    assert result.action == ResolutionAction.PROPAGATE_VALUE
    assert result.resolved_value == "500000"
    assert result.target_tools == frozenset({Tool.NOTION})


def test_propagate_value_treats_none_as_empty() -> None:
    candidates = [
        _tv(Tool.NOTION, None, minutes_after_t0=0),
        _tv(Tool.SPREADSHEET, "テスト値", minutes_after_t0=5),
    ]

    result = resolve_conflict("CLI-001", "取引先名", candidates, db_key="client_master", detected_at=T0)

    assert result.action == ResolutionAction.PROPAGATE_VALUE
    assert result.resolved_value == "テスト値"


def test_propagate_delete_when_emptying_is_newer() -> None:
    """片方が空欄・空欄化した側が新しい -> 他方も空欄化する。"""
    candidates = [
        _tv(Tool.NOTION, "旧値", minutes_after_t0=0),
        _tv(Tool.KINTONE, "", minutes_after_t0=10),
    ]

    result = resolve_conflict("CLI-001", "取引先名", candidates, db_key="client_master", detected_at=T0)

    assert result.action == ResolutionAction.PROPAGATE_DELETE
    assert result.resolved_value is None
    assert result.target_tools == frozenset({Tool.NOTION})


def test_propagate_value_wins_tie_break_over_empty_when_updated_at_ties() -> None:
    """BLOCKER1回帰テスト: updated_atが同時刻でタイした場合、空欄側が誤って「新しい」と
    判定されて値が消失してはいけない（値ありの候補を優先する）。

    Notion側レコードが取得できず notion_current=None・notion_updated_at=event.occurred_at
    にフォールバックした結果、ソース側の新しい値と同時刻でタイするケースを想定している。
    """
    candidates = [
        ToolValue(tool=Tool.NOTION, value=None, updated_at=T0),
        ToolValue(tool=Tool.KINTONE, value="新規登録名", updated_at=T0),
    ]

    result = resolve_conflict("CLI-001", "取引先名", candidates, db_key="client_master", detected_at=T0)

    assert result.action == ResolutionAction.PROPAGATE_VALUE
    assert result.resolved_value == "新規登録名"
    assert result.target_tools == frozenset({Tool.NOTION})


def test_propagate_delete_when_notion_side_is_the_one_that_emptied() -> None:
    """空欄化が新しい側がNotionであっても、Notion優先ルールではなく更新日時ルールに従う。"""
    candidates = [
        _tv(Tool.KINTONE, "旧値", minutes_after_t0=0),
        _tv(Tool.NOTION, "", minutes_after_t0=10),
    ]

    result = resolve_conflict("CLI-001", "取引先名", candidates, db_key="client_master", detected_at=T0)

    assert result.action == ResolutionAction.PROPAGATE_DELETE
    assert result.resolved_value is None
    assert result.target_tools == frozenset({Tool.KINTONE})


# --- 2. 一方が空欄か？ -> NO: 双方に異なる値が存在（データ競合） -----------------------


def test_notion_override_when_both_values_differ() -> None:
    candidates = [
        _tv(Tool.NOTION, "商談中(B)", minutes_after_t0=0),
        _tv(Tool.KINTONE, "失注", minutes_after_t0=10),  # 更新日時がNotionより新しくても関係ない
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", candidates, db_key="project", detected_at=T0
    )

    assert result.action == ResolutionAction.NOTION_OVERRIDE
    assert result.resolved_value == "商談中(B)"
    assert result.target_tools == frozenset({Tool.KINTONE})
    assert len(result.rejected) == 1
    rejected = result.rejected[0]
    assert rejected.record_id == "MSA-PJ-001"
    assert rejected.property_name == "営業ステータス"
    assert rejected.adopted_value == "商談中(B)"
    assert rejected.rejected_value == "失注"
    assert rejected.rejected_tool == Tool.KINTONE
    assert rejected.occurred_at == T0


def test_notion_override_rejects_all_non_notion_distinct_values() -> None:
    candidates = [
        _tv(Tool.NOTION, "商談中(B)", minutes_after_t0=0),
        _tv(Tool.KINTONE, "失注", minutes_after_t0=10),
        _tv(Tool.SPREADSHEET, "契約済", minutes_after_t0=20),
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", candidates, db_key="project", detected_at=T0
    )

    assert result.action == ResolutionAction.NOTION_OVERRIDE
    assert result.target_tools == frozenset({Tool.KINTONE, Tool.SPREADSHEET})
    assert {r.rejected_tool for r in result.rejected} == {Tool.KINTONE, Tool.SPREADSHEET}
    assert {r.rejected_value for r in result.rejected} == {"失注", "契約済"}


def test_multiple_distinct_nonempty_values_with_one_empty_is_still_a_conflict() -> None:
    """非空の値が2種類以上ある場合は、空欄が混ざっていても「片方空欄」ルールではなく競合として扱う。"""
    candidates = [
        _tv(Tool.NOTION, "商談中(B)", minutes_after_t0=0),
        _tv(Tool.KINTONE, "", minutes_after_t0=5),
        _tv(Tool.SPREADSHEET, "失注", minutes_after_t0=10),
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", candidates, db_key="project", detected_at=T0
    )

    assert result.action == ResolutionAction.NOTION_OVERRIDE
    assert result.resolved_value == "商談中(B)"
    # 空欄だったkintoneはrejectedデータには含めない（却下すべき「値」が無いため）が、
    # Notionの値へ補完する必要はあるためtarget_toolsには含める。
    assert Tool.KINTONE in result.target_tools
    assert {r.rejected_tool for r in result.rejected} == {Tool.SPREADSHEET}


# --- アラート通知（重要項目） --------------------------------------------------------


def test_notify_slack_true_for_important_property_via_config_file() -> None:
    candidates = [
        _tv(Tool.NOTION, "商談中(B)", minutes_after_t0=0),
        _tv(Tool.KINTONE, "失注", minutes_after_t0=10),
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", candidates, db_key="project", detected_at=T0
    )

    assert result.notify_slack is True


def test_notify_slack_false_for_non_important_property_via_config_file() -> None:
    candidates = [
        _tv(Tool.NOTION, "新案件名A", minutes_after_t0=0),
        _tv(Tool.KINTONE, "新案件名B", minutes_after_t0=10),
    ]

    result = resolve_conflict("MSA-PJ-001", "案件名", candidates, db_key="project", detected_at=T0)

    assert result.notify_slack is False


def test_notify_slack_false_when_no_conflict() -> None:
    candidates = [
        _tv(Tool.NOTION, "商談中(B)", minutes_after_t0=0),
        _tv(Tool.KINTONE, "商談中(B)", minutes_after_t0=10),
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", candidates, db_key="project", detected_at=T0
    )

    assert result.notify_slack is False


def test_notify_slack_false_with_explicit_empty_important_properties() -> None:
    candidates = [
        _tv(Tool.NOTION, "商談中(B)", minutes_after_t0=0),
        _tv(Tool.KINTONE, "失注", minutes_after_t0=10),
    ]

    result = resolve_conflict(
        "MSA-PJ-001",
        "営業ステータス",
        candidates,
        db_key="project",
        detected_at=T0,
        important_properties={},
    )

    assert result.notify_slack is False


# --- 入力バリデーション ---------------------------------------------------------------


def test_resolve_conflict_raises_on_empty_candidates() -> None:
    with pytest.raises(ValueError):
        resolve_conflict("CLI-001", "取引先名", [], db_key="client_master", detected_at=T0)


def test_resolve_conflict_raises_without_notion_candidate() -> None:
    candidates = [_tv(Tool.KINTONE, "値", minutes_after_t0=0)]
    with pytest.raises(ValueError):
        resolve_conflict("CLI-001", "取引先名", candidates, db_key="client_master", detected_at=T0)


# --- load_important_properties / is_important_property --------------------------------


def test_load_important_properties_reads_default_config_file() -> None:
    config = load_important_properties()

    assert "営業ステータス" in config["project"]


def test_is_important_property_true_with_default_config() -> None:
    assert is_important_property("project", "確度") is True


def test_is_important_property_false_for_unknown_db() -> None:
    assert is_important_property("no-such-db", "営業ステータス") is False


def test_is_important_property_with_explicit_mapping() -> None:
    mapping = {"project": frozenset({"独自重要項目"})}

    assert is_important_property("project", "独自重要項目", mapping) is True
    assert is_important_property("project", "営業ステータス", mapping) is False
