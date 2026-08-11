from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from src.api.dashboard_service import build_manager_alerts


class FakeDataSource:
    """`NotionDataSource`と同じインターフェース（get_projects/get_actions）を持つテスト用スタブ。

    `page_to_display_dict` + ユーザー名解決まで済んだ後の形（本番のNotionDataSourceが
    返す形）の辞書を直接保持する。（tests/api/test_dashboard_service.pyのFakeDataSourceと同じ流儀）
    """

    def __init__(
        self, projects: list[dict[str, Any]] | None = None, actions: list[dict[str, Any]] | None = None
    ) -> None:
        self._projects = projects or []
        self._actions = actions or []

    def get_projects(self) -> list[dict[str, Any]]:
        return self._projects

    def get_actions(self) -> list[dict[str, Any]]:
        return self._actions


def _project(**overrides: Any) -> dict[str, Any]:
    base = {
        "notion_page_id": "proj-1",
        "案件名": "サンプルホテル",
        "営業ステータス": "アポ",
        "確度": "A",
        "初期費用": 100000,
        "月額費用": 30000,
        "担当メンバー": ["田中太郎"],
        "次回アクション日": None,
        "提案サービス": ["リピッテ"],
        "作成日時": "2026-08-05T09:00:00.000Z",
    }
    base.update(overrides)
    return base


AS_OF = date(2026, 8, 12)


# --- lost --------------------------------------------------------------------------------------


def test_lost_bucket_contains_project_with_lost_status() -> None:
    data_source = FakeDataSource(projects=[_project(notion_page_id="p1", 営業ステータス="失注")])

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert [a["notion_page_id"] for a in result["alerts"]["lost"]] == ["p1"]
    assert result["alerts"]["lost"][0]["reason"] == "lost"
    assert result["counts"]["lost"] == 1


# --- lost_candidate（確度Dの代理指標） -----------------------------------------------------------


def test_lost_candidate_bucket_contains_active_status_with_confidence_d() -> None:
    data_source = FakeDataSource(
        projects=[_project(notion_page_id="p1", 営業ステータス="アポ", 確度="D")]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert [a["notion_page_id"] for a in result["alerts"]["lost_candidate"]] == ["p1"]
    assert result["alerts"]["lost_candidate"][0]["reason"] == "lost_candidate"


def test_lost_candidate_bucket_excludes_active_status_with_higher_confidence() -> None:
    data_source = FakeDataSource(
        projects=[_project(notion_page_id="p1", 営業ステータス="アポ", 確度="A")]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert result["alerts"]["lost_candidate"] == []


def test_lost_candidate_bucket_excludes_confidence_d_when_not_active_status() -> None:
    # 失注済み案件の確度がDでも、lost_candidateではなくlostに分類される。
    data_source = FakeDataSource(
        projects=[_project(notion_page_id="p1", 営業ステータス="失注", 確度="D")]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert result["alerts"]["lost_candidate"] == []
    assert [a["notion_page_id"] for a in result["alerts"]["lost"]] == ["p1"]


def test_lost_candidate_bucket_excludes_cancelled_status_with_confidence_d() -> None:
    # 解約済み案件の確度がDでも、lost_candidateには含まれない。
    data_source = FakeDataSource(
        projects=[_project(notion_page_id="p1", 営業ステータス="解約", 確度="D")]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert result["alerts"]["lost_candidate"] == []


def test_cancelled_status_project_is_excluded_from_all_buckets() -> None:
    # 解約は「進行中」「失注」「契約済」いずれの区分にも当たらず、現状どのバケットにも
    # 入らない（将来のリファクタリングで挙動が変わらないよう固定するテスト）。
    data_source = FakeDataSource(
        projects=[_project(notion_page_id="p1", 営業ステータス="解約", 確度="D")]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert all(
        a["notion_page_id"] != "p1" for bucket in result["alerts"].values() for a in bucket
    )


def test_lost_candidate_bucket_excludes_missing_confidence_without_crashing() -> None:
    data_source = FakeDataSource(
        projects=[_project(notion_page_id="p1", 営業ステータス="アポ", 確度=None)]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert result["alerts"]["lost_candidate"] == []


def test_lost_candidate_bucket_entries_are_flagged_as_proxy() -> None:
    data_source = FakeDataSource(
        projects=[_project(notion_page_id="p1", 営業ステータス="アポ", 確度="D")]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert result["alerts"]["lost_candidate"][0]["is_proxy"] is True


def test_non_lost_candidate_bucket_entries_are_not_flagged_as_proxy() -> None:
    data_source = FakeDataSource(
        projects=[
            _project(notion_page_id="p1", 営業ステータス="失注"),
            _project(notion_page_id="p2", 営業ステータス="契約"),
            _project(notion_page_id="p3", 営業ステータス="アポ", 確度="A"),
        ]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert result["alerts"]["lost"][0]["is_proxy"] is False
    assert result["alerts"]["won"][0]["is_proxy"] is False
    assert result["alerts"]["stalled"][0]["is_proxy"] is False


def test_notes_document_lost_candidate_proxy_caveat() -> None:
    data_source = FakeDataSource(projects=[])

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert any("失注候補" in note and "代理指標" in note for note in result["notes"])
    assert any("スナップショット" in note for note in result["notes"])


# --- stalled -----------------------------------------------------------------------------------


def test_stalled_bucket_contains_active_project_with_missing_next_action_date() -> None:
    data_source = FakeDataSource(
        projects=[_project(notion_page_id="p1", 営業ステータス="アポ", 次回アクション日=None)]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert [a["notion_page_id"] for a in result["alerts"]["stalled"]] == ["p1"]


def test_stalled_bucket_contains_active_project_with_old_next_action_date() -> None:
    data_source = FakeDataSource(
        projects=[
            _project(notion_page_id="p1", 営業ステータス="アポ", 次回アクション日="2026-07-01")
        ]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert [a["notion_page_id"] for a in result["alerts"]["stalled"]] == ["p1"]


def test_stalled_bucket_excludes_active_project_with_recent_next_action_date() -> None:
    data_source = FakeDataSource(
        projects=[
            _project(notion_page_id="p1", 営業ステータス="アポ", 次回アクション日="2026-08-13")
        ]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert result["alerts"]["stalled"] == []


def test_stalled_bucket_includes_project_exactly_at_threshold_boundary() -> None:
    # AS_OF(2026-08-12) - 14日 = 2026-07-29。ちょうど閾値日数前の案件は「N日以上前」に
    # 該当するのでstalledに含まれる（境界値、off-by-one回帰防止）。
    data_source = FakeDataSource(
        projects=[
            _project(notion_page_id="p1", 営業ステータス="アポ", 次回アクション日="2026-07-29")
        ]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert [a["notion_page_id"] for a in result["alerts"]["stalled"]] == ["p1"]


def test_stalled_bucket_excludes_project_one_day_short_of_threshold_boundary() -> None:
    # 閾値の1日手前（2026-07-30）はまだ「N日以上前」に該当しないのでstalledに含まれない。
    data_source = FakeDataSource(
        projects=[
            _project(notion_page_id="p1", 営業ステータス="アポ", 次回アクション日="2026-07-30")
        ]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert result["alerts"]["stalled"] == []


# --- won ---------------------------------------------------------------------------------------


# --- バケットの非排他性 ---------------------------------------------------------------------------


def test_lost_candidate_and_stalled_buckets_are_not_mutually_exclusive() -> None:
    # 確度Dかつ次回アクション日が古い/未設定の案件は、lost_candidateとstalledの両方に
    # 重複して含まれ得る（counts単純合算での過大カウントに繋がる点を明示するテスト）。
    data_source = FakeDataSource(
        projects=[
            _project(notion_page_id="p1", 営業ステータス="アポ", 確度="D", 次回アクション日=None)
        ]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert [a["notion_page_id"] for a in result["alerts"]["lost_candidate"]] == ["p1"]
    assert [a["notion_page_id"] for a in result["alerts"]["stalled"]] == ["p1"]


def test_notes_document_buckets_are_not_mutually_exclusive() -> None:
    data_source = FakeDataSource(projects=[])

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert any("排他ではありません" in note for note in result["notes"])


def test_notes_document_stalled_is_distinct_from_condition_module() -> None:
    data_source = FakeDataSource(projects=[])

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert any(
        "condition" in note and "停滞リスク" in note and "要フォロー" in note
        for note in result["notes"]
    )


def test_won_bucket_contains_confirmed_status_project() -> None:
    data_source = FakeDataSource(projects=[_project(notion_page_id="p1", 営業ステータス="契約")])

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert [a["notion_page_id"] for a in result["alerts"]["won"]] == ["p1"]
    assert result["alerts"]["won"][0]["reason"] == "won"


# --- 未知ステータス -------------------------------------------------------------------------------


def test_unknown_status_is_skipped_with_warning_logged(caplog: pytest.LogCaptureFixture) -> None:
    data_source = FakeDataSource(
        projects=[
            _project(notion_page_id="p1", 営業ステータス="謎のステータス"),
            _project(notion_page_id="p2", 営業ステータス="失注"),
        ]
    )

    with caplog.at_level("WARNING"):
        result = build_manager_alerts(AS_OF, data_source=data_source)

    assert result["counts"]["lost"] == 1
    assert all(
        a["notion_page_id"] != "p1"
        for bucket in result["alerts"].values()
        for a in bucket
    )
    assert any("謎のステータス" in record.getMessage() for record in caplog.records)


def test_project_with_none_status_is_skipped_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_source = FakeDataSource(projects=[_project(notion_page_id="p1", 営業ステータス=None)])

    with caplog.at_level("WARNING"):
        result = build_manager_alerts(AS_OF, data_source=data_source)

    assert all(bucket == [] for bucket in result["alerts"].values())
    assert caplog.records == []


# --- MANAGER_ALERT_STALLED_DAYS環境変数 -----------------------------------------------------------


def test_stalled_days_threshold_defaults_to_14() -> None:
    data_source = FakeDataSource(projects=[])

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert result["stalled_days_threshold"] == 14


def test_stalled_days_threshold_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGER_ALERT_STALLED_DAYS", "3")
    data_source = FakeDataSource(
        projects=[
            _project(notion_page_id="p1", 営業ステータス="アポ", 次回アクション日="2026-08-10")
        ]
    )

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert result["stalled_days_threshold"] == 3
    # as_of(8/12) - 3日 = 8/9。次回アクション日8/10は8/9より後なのでstalledにならない。
    assert result["alerts"]["stalled"] == []


def test_stalled_days_threshold_falls_back_to_default_for_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MANAGER_ALERT_STALLED_DAYS", "not-a-number")
    data_source = FakeDataSource(projects=[])

    result = build_manager_alerts(AS_OF, data_source=data_source)

    assert result["stalled_days_threshold"] == 14
