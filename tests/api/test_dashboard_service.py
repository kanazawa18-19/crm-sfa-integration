from __future__ import annotations

from datetime import date
from typing import Any

from src.api.dashboard_service import (
    NotionDataSource,
    build_daily_report,
    build_dashboard_summary,
    build_member_performance,
    reset_cache,
    search_projects,
)


class FakeDataSource:
    """`NotionDataSource`と同じインターフェース（get_projects/get_actions）を持つテスト用スタブ。

    `page_to_display_dict` + ユーザー名解決まで済んだ後の形（本番のNotionDataSourceが
    返す形）の辞書を直接保持する。
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


def _action(**overrides: Any) -> dict[str, Any]:
    base = {
        "notion_page_id": "act-1",
        "商談回数・電話回数・メール回数（何回目）": "【電話】1回目",
        "アクション日": "2026-08-05",
        "案件名": ["proj-1"],
        "担当営業": "田中太郎",
    }
    base.update(overrides)
    return base


# --- build_dashboard_summary ------------------------------------------------------------------


def test_build_dashboard_summary_counts_by_category() -> None:
    projects = [
        _project(notion_page_id="p1", 営業ステータス="契約"),
        _project(notion_page_id="p2", 営業ステータス="アポ"),
        _project(notion_page_id="p3", 営業ステータス="失注"),
        _project(notion_page_id="p4", 営業ステータス="解約"),
    ]
    data_source = FakeDataSource(projects=projects)

    result = build_dashboard_summary(data_source=data_source)

    assert result["totals"]["project_count"] == 4
    assert result["totals"]["confirmed_count"] == 1
    assert result["totals"]["active_count"] == 1
    assert result["totals"]["lost_count"] == 1
    assert result["totals"]["cancelled_count"] == 1
    assert "as_of" in result
    assert "forecast" in result
    assert set(result["forecast"].keys()) == {"max", "expected", "min"}


def test_build_dashboard_summary_status_breakdown_sums_fees() -> None:
    projects = [
        _project(notion_page_id="p1", 営業ステータス="アポ", 初期費用=100000, 月額費用=10000),
        _project(notion_page_id="p2", 営業ステータス="アポ", 初期費用=200000, 月額費用=20000),
    ]
    data_source = FakeDataSource(projects=projects)

    result = build_dashboard_summary(data_source=data_source)

    breakdown = {entry["status"]: entry for entry in result["status_breakdown"]}
    assert breakdown["アポ"]["count"] == 2
    assert breakdown["アポ"]["initial_fee_sum"] == 300000
    assert breakdown["アポ"]["monthly_fee_sum"] == 30000
    assert breakdown["アポ"]["category"] == "進行中"


def test_build_dashboard_summary_excludes_unknown_status_without_raising(caplog) -> None:
    projects = [
        _project(notion_page_id="p1", 営業ステータス="謎のステータス"),
        _project(notion_page_id="p2", 営業ステータス="アポ"),
    ]
    data_source = FakeDataSource(projects=projects)

    with caplog.at_level("WARNING"):
        result = build_dashboard_summary(data_source=data_source)

    assert result["totals"]["project_count"] == 1
    assert all(entry["status"] != "謎のステータス" for entry in result["status_breakdown"])
    assert any("謎のステータス" in record.getMessage() for record in caplog.records)


# --- build_daily_report ------------------------------------------------------------------------


def test_build_daily_report_status_changes_always_empty() -> None:
    data_source = FakeDataSource(projects=[_project()], actions=[_action()])

    result = build_daily_report(date(2026, 8, 5), data_source=data_source)

    assert result["status_changes"] == []
    assert any("status_changes" in note for note in result["notes"])


def test_build_daily_report_new_project_listed_when_created_today() -> None:
    data_source = FakeDataSource(
        projects=[_project(作成日時="2026-08-05T09:00:00.000Z")],
        actions=[],
    )

    result = build_daily_report(date(2026, 8, 5), data_source=data_source)

    assert len(result["new_projects"]) == 1
    assert result["new_projects"][0]["client_name"] == "サンプルホテル"


def test_build_daily_report_member_summary_counts_actions_for_report_date() -> None:
    data_source = FakeDataSource(
        projects=[_project()],
        actions=[_action(アクション日="2026-08-05")],
    )

    result = build_daily_report(date(2026, 8, 5), data_source=data_source)

    assert len(result["member_summaries"]) == 1
    assert result["member_summaries"][0]["member"] == "田中太郎"
    assert result["member_summaries"][0]["total"] == 1


# --- build_member_performance ------------------------------------------------------------------


def test_build_member_performance_returns_members_and_notes() -> None:
    data_source = FakeDataSource(
        projects=[_project(営業ステータス="契約", 担当メンバー=["田中太郎"])],
        actions=[_action(担当営業="田中太郎")],
    )

    result = build_member_performance(date(2026, 8, 5), data_source=data_source)

    assert [m["member"] for m in result["members"]] == ["田中太郎"]
    assert result["as_of"] == "2026-08-05"
    assert len(result["notes"]) == 2


def test_build_member_performance_handles_no_assignee_gracefully() -> None:
    data_source = FakeDataSource(
        projects=[_project(担当メンバー=[])],
        actions=[],
    )

    result = build_member_performance(date(2026, 8, 5), data_source=data_source)

    assert result["members"] == []


# --- NotionDataSource._resolve_assignee ---------------------------------------------------------
# BLOCKER回帰確認: 「担当営業」rollupの中身が空配列（案件に紐付いていない、または紐付いた
# 案件の担当メンバーが未設定のアクション）の場合にIndexErrorを送出しないこと。


class _FakeUserDirectory:
    def resolve(self, user_id: str) -> str:
        return f"resolved:{user_id}"

    def resolve_many(self, user_ids: list[str]) -> list[str]:
        return [self.resolve(uid) for uid in user_ids]


def _data_source() -> NotionDataSource:
    # get_projects/get_actions自体は呼ばないため、project_client/action_clientはダミーでよい。
    return NotionDataSource(
        project_client=object(), action_client=object(), user_directory=_FakeUserDirectory()
    )


def test_resolve_assignee_handles_empty_rollup_array_without_raising() -> None:
    assert _data_source()._resolve_assignee([]) is None


def test_resolve_assignee_handles_nested_empty_list() -> None:
    assert _data_source()._resolve_assignee([[]]) is None


def test_resolve_assignee_resolves_flat_id_list() -> None:
    assert _data_source()._resolve_assignee(["user-1"]) == "resolved:user-1"


def test_resolve_assignee_handles_none() -> None:
    assert _data_source()._resolve_assignee(None) is None


def test_resolve_assignee_uses_embedded_name_from_people_rollup_without_directory_lookup() -> None:
    """実データ回帰確認: Notion APIのpeopleプロパティにはnameが直接埋め込まれており、
    GET /v1/usersのワークスペースメンバー一覧に無いユーザー（ゲスト等）でも正しく解決できる
    必要がある（NotionUserDirectoryに存在しないIDでも、埋め込みnameを優先して使う）。
    """
    rollup_value = [[{"id": "user-1", "name": "田中太郎"}]]
    assert _data_source()._resolve_assignee(rollup_value) == "田中太郎"


def test_resolve_assignee_falls_back_to_directory_when_name_missing() -> None:
    rollup_value = [[{"id": "user-1", "name": None}]]
    assert _data_source()._resolve_assignee(rollup_value) == "resolved:user-1"


def test_resolve_assignee_shows_placeholder_when_unresolvable_by_directory_too() -> None:
    """実データ回帰確認: 削除済み・ゲストユーザー等、NotionがnameもGET /v1/usersでの
    解決も返さないケースが実在する。この場合、生のUUIDをそのまま表示せず人間が読める
    プレースホルダーにする（`NotionUserDirectory`のフォールバック仕様＝未知IDはID自体を
    返す、を利用して「解決できなかった」ことを検知する）。
    """

    class _UnresolvingUserDirectory:
        def resolve(self, user_id: str) -> str:
            return user_id  # NotionUserDirectoryの未知ID時のフォールバック仕様を模す

        def resolve_many(self, user_ids: list[str]) -> list[str]:
            return list(user_ids)

    ds = NotionDataSource(
        project_client=object(),
        action_client=object(),
        user_directory=_UnresolvingUserDirectory(),
    )
    rollup_value = [[{"id": "a3a0e027-c89b-4fd8-b975-da5cdf7decb9", "name": None}]]

    assert ds._resolve_assignee(rollup_value) == "不明なメンバー（a3a0e027）"


# --- NotionDataSource（担当メンバー: 案件管理DBのpeople型）の名前解決 -------------------------------


def test_get_projects_uses_embedded_name_from_people_property() -> None:
    class _FakeProjectClient:
        def query_all_pages(self) -> list[dict[str, Any]]:
            return [
                {
                    "id": "proj-1",
                    "properties": {
                        "案件名": {"type": "title", "title": [{"plain_text": "サンプルホテル"}]},
                        "担当メンバー": {
                            "type": "people",
                            "people": [{"object": "user", "id": "user-1", "name": "田中太郎"}],
                        },
                    },
                }
            ]

    ds = NotionDataSource(
        project_client=_FakeProjectClient(),
        action_client=object(),
        user_directory=_FakeUserDirectory(),
    )

    projects = ds.get_projects()

    assert projects[0]["担当メンバー"] == ["田中太郎"]


# --- モジュールレベルTTLキャッシュ -----------------------------------------------------------------


class _CountingProjectClient:
    """query_all_pages()の呼び出し回数を記録するだけのフェイク（結果は常に空）。"""

    def __init__(self) -> None:
        self.call_count = 0

    def query_all_pages(self) -> list[dict[str, Any]]:
        self.call_count += 1
        return []


def test_get_projects_reuses_cached_result_within_ttl() -> None:
    reset_cache()
    project_client = _CountingProjectClient()
    data_source = NotionDataSource(
        project_client=project_client, action_client=object(), user_directory=_FakeUserDirectory()
    )

    data_source.get_projects()
    data_source.get_projects()

    assert project_client.call_count == 1
    reset_cache()


def test_get_projects_refetches_after_reset_cache() -> None:
    reset_cache()
    project_client = _CountingProjectClient()
    data_source = NotionDataSource(
        project_client=project_client, action_client=object(), user_directory=_FakeUserDirectory()
    )

    data_source.get_projects()
    reset_cache()
    data_source.get_projects()

    assert project_client.call_count == 2
    reset_cache()


def test_build_dashboard_summary_with_explicit_data_source_is_unaffected_by_module_cache() -> None:
    # data_sourceを明示的に注入するbuild_*系はNotionDataSourceを経由しないため、
    # モジュールレベルキャッシュの影響を受けない（FakeDataSourceは呼ばれるたびに素の
    # get_projects()/get_actions()を返す）ことの回帰確認。
    reset_cache()
    data_source_a = FakeDataSource(projects=[_project(営業ステータス="アポ")])
    data_source_b = FakeDataSource(projects=[_project(営業ステータス="契約")])

    result_a = build_dashboard_summary(data_source=data_source_a)
    result_b = build_dashboard_summary(data_source=data_source_b)

    assert result_a["totals"]["active_count"] == 1
    assert result_b["totals"]["confirmed_count"] == 1


# --- search_projects ------------------------------------------------------------------------


def test_search_projects_matches_case_insensitive_substring() -> None:
    data_source = FakeDataSource(
        projects=[
            _project(notion_page_id="p1", 案件名="サンプルホテル大阪"),
            _project(notion_page_id="p2", 案件名="別のホテル"),
        ]
    )

    result = search_projects("サンプル", data_source=data_source)

    assert [p["notion_page_id"] for p in result["projects"]] == ["p1"]
    assert result["total_matched"] == 1


def test_search_projects_returns_empty_for_no_match() -> None:
    data_source = FakeDataSource(projects=[_project(案件名="サンプルホテル")])

    result = search_projects("存在しない案件名", data_source=data_source)

    assert result["projects"] == []
    assert result["total_matched"] == 0


def test_search_projects_caps_results_at_max_but_reports_total_matched() -> None:
    projects = [
        _project(notion_page_id=f"p{i}", 案件名=f"サンプルホテル{i}") for i in range(25)
    ]
    data_source = FakeDataSource(projects=projects)

    result = search_projects("サンプル", data_source=data_source)

    assert len(result["projects"]) == 20
    assert result["total_matched"] == 25
