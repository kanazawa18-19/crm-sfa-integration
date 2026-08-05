"""06_営業分析ロジック「勝ちパターン分析」「クロスセル対象抽出」の検証。"""

from __future__ import annotations

from src.analytics.win_pattern import (
    ClientServiceStatus,
    ProposalRecord,
    WinPattern,
    analyze_win_patterns,
    extract_cross_sell_targets,
)


def test_analyze_win_patterns_empty_input_returns_empty_list() -> None:
    assert analyze_win_patterns([]) == []


def test_analyze_win_patterns_groups_by_meeting_number_and_services() -> None:
    records = [
        ProposalRecord("P1", meeting_number=2, services=frozenset({"リピッテ"}), is_won=True),
        ProposalRecord("P2", meeting_number=2, services=frozenset({"リピッテ"}), is_won=True),
        ProposalRecord("P3", meeting_number=2, services=frozenset({"リピッテ"}), is_won=False),
        ProposalRecord("P4", meeting_number=3, services=frozenset({"リピッテ", "メイリー"}), is_won=True),
    ]

    # min_sample_size=1を明示し、サンプル数によるフィルタが無い状態でグルーピングのみを検証する。
    result = analyze_win_patterns(records, min_sample_size=1)

    assert result[0] == WinPattern(
        meeting_number=3, services=frozenset({"リピッテ", "メイリー"}), sample_size=1, win_rate=1.0
    )
    assert result[1] == WinPattern(
        meeting_number=2, services=frozenset({"リピッテ"}), sample_size=3, win_rate=2 / 3
    )


def test_analyze_win_patterns_sorted_descending_by_win_rate() -> None:
    records = [
        ProposalRecord("P1", meeting_number=1, services=frozenset({"A"}), is_won=False),
        ProposalRecord("P2", meeting_number=2, services=frozenset({"B"}), is_won=True),
    ]

    result = analyze_win_patterns(records, min_sample_size=1)

    assert [p.win_rate for p in result] == [1.0, 0.0]


def test_analyze_win_patterns_default_min_sample_size_excludes_small_samples() -> None:
    """デフォルトのmin_sample_size=3により、サンプル数1〜2件のノイズが除外されることを確認する。"""
    records = [
        ProposalRecord("P1", meeting_number=1, services=frozenset({"A"}), is_won=True),
        ProposalRecord("P2", meeting_number=2, services=frozenset({"B"}), is_won=True),
        ProposalRecord("P3", meeting_number=2, services=frozenset({"B"}), is_won=True),
        ProposalRecord("P4", meeting_number=2, services=frozenset({"B"}), is_won=False),
    ]

    result = analyze_win_patterns(records)

    assert len(result) == 1
    assert result[0].meeting_number == 2
    assert result[0].services == frozenset({"B"})
    assert result[0].sample_size == 3


def test_analyze_win_patterns_filters_by_min_sample_size() -> None:
    records = [
        ProposalRecord("P1", meeting_number=1, services=frozenset({"A"}), is_won=True),
        ProposalRecord("P2", meeting_number=2, services=frozenset({"B"}), is_won=True),
        ProposalRecord("P3", meeting_number=2, services=frozenset({"B"}), is_won=False),
    ]

    result = analyze_win_patterns(records, min_sample_size=2)

    assert len(result) == 1
    assert result[0].meeting_number == 2
    assert result[0].services == frozenset({"B"})


def test_extract_cross_sell_targets_empty_input_returns_empty_dict() -> None:
    assert extract_cross_sell_targets([], all_services=["リピッテ"]) == {}


def test_extract_cross_sell_targets_excludes_contracted_and_proposed_services() -> None:
    clients = [
        ClientServiceStatus(
            client_id="CL-001",
            contracted_services=frozenset({"リピッテ"}),
            proposed_services=frozenset({"メイリー"}),
        ),
    ]

    result = extract_cross_sell_targets(
        clients, all_services=["リピッテ", "メイリー", "ホテラボ", "オルト"]
    )

    assert result == {"CL-001": frozenset({"ホテラボ", "オルト"})}


def test_extract_cross_sell_targets_client_with_no_untapped_services_is_absent() -> None:
    clients = [
        ClientServiceStatus(
            client_id="CL-001",
            contracted_services=frozenset({"リピッテ"}),
            proposed_services=frozenset(),
        ),
    ]

    result = extract_cross_sell_targets(clients, all_services=["リピッテ"])

    assert result == {}
