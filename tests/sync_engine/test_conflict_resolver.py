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


# --- 2. 双方に異なる値が存在（データ競合） -> 最新編集優先ルールで解決 ------------------


def test_more_recent_non_notion_edit_wins_over_notions_stale_value() -> None:
    """実際の本番障害の再現ケース: Notionの値が古く、他ツール側の編集がそれより新しい場合、
    Notionが無条件に勝つのではなく、より新しい編集を採用する（最新編集優先ルール）。"""
    candidates = [
        _tv(Tool.NOTION, "商談中(B)", minutes_after_t0=0),
        _tv(Tool.KINTONE, "失注", minutes_after_t0=10),  # Notionより更新日時が新しい
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", candidates, db_key="project", detected_at=T0
    )

    assert result.action == ResolutionAction.PROPAGATE_VALUE
    assert result.resolved_value == "失注"
    assert result.target_tools == frozenset({Tool.NOTION})
    assert len(result.rejected) == 1
    rejected = result.rejected[0]
    assert rejected.record_id == "MSA-PJ-001"
    assert rejected.property_name == "営業ステータス"
    assert rejected.adopted_value == "失注"
    assert rejected.rejected_value == "商談中(B)"
    assert rejected.rejected_tool == Tool.NOTION
    assert rejected.adopted_tool == Tool.KINTONE
    assert rejected.occurred_at == T0


def test_notion_still_wins_when_notion_is_genuinely_the_most_recent_edit() -> None:
    """Notion側の編集が実際に他ツールより新しい場合は、引き続きNotionの値が採用される
    （最新編集優先ルールが正しく機能していること・今回の修正がこのケースを壊していないこと
    の確認）。"""
    candidates = [
        _tv(Tool.KINTONE, "失注", minutes_after_t0=0),
        _tv(Tool.NOTION, "商談中(B)", minutes_after_t0=10),  # kintoneより更新日時が新しい
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", candidates, db_key="project", detected_at=T0
    )

    assert result.action == ResolutionAction.NOTION_OVERRIDE
    assert result.resolved_value == "商談中(B)"
    assert result.target_tools == frozenset({Tool.KINTONE})
    assert len(result.rejected) == 1
    rejected = result.rejected[0]
    assert rejected.adopted_value == "商談中(B)"
    assert rejected.adopted_tool == Tool.NOTION
    assert rejected.rejected_value == "失注"
    assert rejected.rejected_tool == Tool.KINTONE


def test_notion_wins_tie_break_when_updated_at_exactly_ties() -> None:
    """複数候補のupdated_atが完全に同時刻でタイした場合、_pick_tie_break_winner()が
    Tool.NOTIONを明示的に優先することで、Notionがフォールバックのタイブレーク先として
    採用されることを固定化する（意図的な保証であり、偶然のリスト順序に依存させないことを
    ここで明示する）。"""
    candidates = [
        ToolValue(tool=Tool.NOTION, value="商談中(B)", updated_at=T0),
        ToolValue(tool=Tool.KINTONE, value="失注", updated_at=T0),  # Notionと完全に同時刻
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", candidates, db_key="project", detected_at=T0
    )

    assert result.action == ResolutionAction.NOTION_OVERRIDE
    assert result.resolved_value == "商談中(B)"
    assert result.target_tools == frozenset({Tool.KINTONE})


def test_notion_wins_tie_break_even_when_notion_is_not_first_in_candidates_list() -> None:
    """WARN2回帰テスト: Notion優先のタイブレークが、dispatcher.py側の「candidatesの先頭に
    Tool.NOTIONを配置する」という呼び出し順序に暗黙に依存していないことを確認する
    （Notionをリストの末尾に置いても結果が変わらないこと＝resolve_conflict自身が
    Tool.NOTIONを明示的に判定していることの証明）。"""
    candidates = [
        ToolValue(tool=Tool.KINTONE, value="失注", updated_at=T0),
        ToolValue(tool=Tool.NOTION, value="商談中(B)", updated_at=T0),  # あえて末尾に配置
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", candidates, db_key="project", detected_at=T0
    )

    assert result.action == ResolutionAction.NOTION_OVERRIDE
    assert result.resolved_value == "商談中(B)"
    assert result.target_tools == frozenset({Tool.KINTONE})


def test_tie_break_between_two_non_notion_tools_is_deterministic_regardless_of_list_order() -> None:
    """WARN1回帰テスト: Notionを含まない2ツールがupdated_atで完全にタイし、値が異なる場合、
    どちらが採用されるかはTool.value（文字列）の昇順という決定的なキーで決まり、
    candidatesリストの構築順序（≒frozensetの非決定的なイテレーション順）には依存しない
    ことを、入力順序を入れ替えた2パターンで確認する。"""
    notion = ToolValue(tool=Tool.NOTION, value="商談中(B)", updated_at=T0 - timedelta(minutes=10))
    kintone = ToolValue(tool=Tool.KINTONE, value="失注", updated_at=T0)
    zoho = ToolValue(tool=Tool.ZOHO, value="契約済", updated_at=T0)  # kintoneと完全に同時刻

    result_order_a = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", [notion, kintone, zoho], db_key="project", detected_at=T0
    )
    result_order_b = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", [notion, zoho, kintone], db_key="project", detected_at=T0
    )

    assert result_order_a.resolved_value == result_order_b.resolved_value
    # kintone("kintone") < zoho("zoho") をTool.valueの昇順で比較するとkintoneが選ばれる。
    assert result_order_a.resolved_value == "失注"


def test_notion_override_rejects_all_non_notion_distinct_values() -> None:
    """Notion自身の編集が最新の場合、他の非空の異なる値を持つツールは全て却下される。"""
    candidates = [
        _tv(Tool.KINTONE, "失注", minutes_after_t0=0),
        _tv(Tool.SPREADSHEET, "契約済", minutes_after_t0=5),
        _tv(Tool.NOTION, "商談中(B)", minutes_after_t0=20),  # 最新
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", candidates, db_key="project", detected_at=T0
    )

    assert result.action == ResolutionAction.NOTION_OVERRIDE
    assert result.target_tools == frozenset({Tool.KINTONE, Tool.SPREADSHEET})
    assert {r.rejected_tool for r in result.rejected} == {Tool.KINTONE, Tool.SPREADSHEET}
    assert {r.rejected_value for r in result.rejected} == {"失注", "契約済"}
    assert {r.adopted_tool for r in result.rejected} == {Tool.NOTION}


def test_multiple_distinct_nonempty_values_with_one_empty_is_still_a_conflict() -> None:
    """非空の値が2種類以上ある場合は、空欄が混ざっていても「片方空欄」ルールではなく競合として
    扱い、その中でも最新編集優先ルールで解決する。"""
    candidates = [
        _tv(Tool.NOTION, "商談中(B)", minutes_after_t0=0),
        _tv(Tool.KINTONE, "", minutes_after_t0=5),
        _tv(Tool.SPREADSHEET, "失注", minutes_after_t0=10),  # 最新かつ非空 → これが採用される
    ]

    result = resolve_conflict(
        "MSA-PJ-001", "営業ステータス", candidates, db_key="project", detected_at=T0
    )

    assert result.action == ResolutionAction.PROPAGATE_VALUE
    assert result.resolved_value == "失注"
    # 空欄だったkintoneはrejectedデータには含めない（却下すべき「値」が無いため）が、
    # 採用値へ補完する必要はあるためtarget_toolsには含める。
    assert Tool.KINTONE in result.target_tools
    assert Tool.NOTION in result.target_tools
    assert {r.rejected_tool for r in result.rejected} == {Tool.NOTION}
    assert result.rejected[0].rejected_value == "商談中(B)"
    assert result.rejected[0].adopted_tool == Tool.SPREADSHEET


def test_rejected_and_notify_slack_fire_regardless_of_which_side_wins() -> None:
    """rejected/notify_slackは、採用側がNotionであっても他ツールであっても、非空の値が
    2種類以上存在した場合は同様に発生する（Notion固有のイベントではない）。"""
    # ケース1: 採用側がNotion以外。
    non_notion_wins = resolve_conflict(
        "MSA-PJ-001",
        "営業ステータス",
        [
            _tv(Tool.NOTION, "商談中(B)", minutes_after_t0=0),
            _tv(Tool.KINTONE, "失注", minutes_after_t0=10),
        ],
        db_key="project",
        detected_at=T0,
    )
    assert len(non_notion_wins.rejected) == 1
    assert non_notion_wins.notify_slack is True

    # ケース2: 採用側がNotion。
    notion_wins = resolve_conflict(
        "MSA-PJ-001",
        "営業ステータス",
        [
            _tv(Tool.KINTONE, "失注", minutes_after_t0=0),
            _tv(Tool.NOTION, "商談中(B)", minutes_after_t0=10),
        ],
        db_key="project",
        detected_at=T0,
    )
    assert len(notion_wins.rejected) == 1
    assert notion_wins.notify_slack is True


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
