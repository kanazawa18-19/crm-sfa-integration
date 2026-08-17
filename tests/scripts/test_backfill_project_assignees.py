from __future__ import annotations

from typing import Any

from scripts.backfill_project_assignees import (
    AutoAssignCandidate,
    ExecutionResult,
    KnownOwner,
    build_notion_page_id_to_kintone_id,
    build_notion_page_id_to_zoho_deal_id,
    execute_assignments,
    fetch_zoho_deal_owner_emails,
    plan_backfill,
    print_summary,
)
from src.sync_engine.id_mapping import IdMapping


def _page(
    page_id: str,
    *,
    project_name: str = "サンプル案件",
    assignees: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """案件管理DBの生Notionページオブジェクトを模す（担当メンバー=people型、
    案件名=title型のみを含む最小構成）。"""
    return {
        "id": page_id,
        "properties": {
            "案件名": {"type": "title", "title": [{"plain_text": project_name}]},
            "担当メンバー": {"type": "people", "people": assignees or []},
        },
    }


class _FakeZohoResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.ok = status_code < 400
        self._body = body or {}

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeZohoClient:
    """`HttpZohoClient.request()`を模す、ページ単位のレスポンスを差し替え可能なフェイク。"""

    def __init__(self, responses_by_page: dict[int, _FakeZohoResponse]) -> None:
        self._responses_by_page = responses_by_page
        self.requested_urls: list[str] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeZohoResponse:
        self.requested_urls.append(url)
        page = int(url.rsplit("&page=", 1)[1])
        return self._responses_by_page[page]


class _FakeNotionClient:
    """`execute_assignments()`が使う`get_page`/`update_page`のみを模したフェイク。"""

    def __init__(self, pages_by_id: dict[str, dict[str, Any] | None] | None = None) -> None:
        self._pages_by_id = pages_by_id or {}
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self._update_page_side_effect: dict[str, Exception] = {}

    def set_update_page_error(self, page_id: str, exc: Exception) -> None:
        self._update_page_side_effect[page_id] = exc

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        return self._pages_by_id.get(page_id)

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
        if page_id in self._update_page_side_effect:
            raise self._update_page_side_effect[page_id]
        self.updated.append((page_id, properties))


_KNOWN_OWNERS: dict[str, KnownOwner] = {
    "kanazawa@cnctor.jp": KnownOwner("notion-user-kanazawa", "金沢裕貴"),
    "kunikata@cnctor.jp": KnownOwner("notion-user-kunikata", "國方勇樹"),
}


def _candidate(page_id: str = "p1", **overrides: Any) -> AutoAssignCandidate:
    defaults: dict[str, Any] = dict(
        page_id=page_id,
        project_name="サンプル案件",
        zoho_deal_id="zoho-1",
        owner_email="kanazawa@cnctor.jp",
        resolved_user_id="notion-user-kanazawa",
        resolved_user_name="金沢裕貴",
    )
    defaults.update(overrides)
    return AutoAssignCandidate(**defaults)


# --- build_notion_page_id_to_zoho_deal_id -----------------------------------------------------


def test_build_notion_page_id_to_zoho_deal_id_maps_notion_key_to_zoho_id() -> None:
    mappings = [IdMapping(notion_key="p1", db_key="project", zoho_id="zoho-1")]

    result = build_notion_page_id_to_zoho_deal_id(mappings)

    assert result == {"p1": "zoho-1"}


def test_build_notion_page_id_to_zoho_deal_id_skips_mappings_without_zoho_id() -> None:
    mappings = [
        IdMapping(notion_key="p1", db_key="project", zoho_id="zoho-1"),
        IdMapping(notion_key="p2", db_key="project", zoho_id=None),
    ]

    result = build_notion_page_id_to_zoho_deal_id(mappings)

    assert result == {"p1": "zoho-1"}


# --- build_notion_page_id_to_kintone_id ---------------------------------------------------------


def test_build_notion_page_id_to_kintone_id_maps_notion_key_to_kintone_id() -> None:
    mappings = [IdMapping(notion_key="p1", db_key="project", kintone_id="123")]

    result = build_notion_page_id_to_kintone_id(mappings)

    assert result == {"p1": "123"}


def test_build_notion_page_id_to_kintone_id_skips_mappings_without_kintone_id() -> None:
    mappings = [
        IdMapping(notion_key="p1", db_key="project", kintone_id="123"),
        IdMapping(notion_key="p2", db_key="project", kintone_id=None, zoho_id="zoho-2"),
    ]

    result = build_notion_page_id_to_kintone_id(mappings)

    assert result == {"p1": "123"}


# --- fetch_zoho_deal_owner_emails -------------------------------------------------------------


def test_fetch_zoho_deal_owner_emails_maps_deal_id_to_owner_email() -> None:
    client = _FakeZohoClient(
        {
            1: _FakeZohoResponse(
                200,
                {
                    "data": [
                        {"id": "deal-1", "Owner": {"id": "u1", "name": "金沢裕貴", "email": "kanazawa@cnctor.jp"}},
                    ],
                    "info": {"more_records": False},
                },
            ),
        }
    )

    result = fetch_zoho_deal_owner_emails(client)  # type: ignore[arg-type]

    assert result == {"deal-1": "kanazawa@cnctor.jp"}


def test_fetch_zoho_deal_owner_emails_pages_through_more_records() -> None:
    client = _FakeZohoClient(
        {
            1: _FakeZohoResponse(
                200,
                {
                    "data": [{"id": "deal-1", "Owner": {"email": "kanazawa@cnctor.jp"}}],
                    "info": {"more_records": True},
                },
            ),
            2: _FakeZohoResponse(
                200,
                {
                    "data": [{"id": "deal-2", "Owner": {"email": "kunikata@cnctor.jp"}}],
                    "info": {"more_records": False},
                },
            ),
        }
    )

    result = fetch_zoho_deal_owner_emails(client)  # type: ignore[arg-type]

    assert result == {"deal-1": "kanazawa@cnctor.jp", "deal-2": "kunikata@cnctor.jp"}
    assert len(client.requested_urls) == 2


def test_fetch_zoho_deal_owner_emails_skips_deals_without_owner() -> None:
    client = _FakeZohoClient(
        {
            1: _FakeZohoResponse(
                200,
                {
                    "data": [{"id": "deal-1", "Owner": None}],
                    "info": {"more_records": False},
                },
            ),
        }
    )

    result = fetch_zoho_deal_owner_emails(client)  # type: ignore[arg-type]

    assert result == {}


def test_fetch_zoho_deal_owner_emails_stops_on_204_no_content() -> None:
    client = _FakeZohoClient({1: _FakeZohoResponse(204)})

    result = fetch_zoho_deal_owner_emails(client)  # type: ignore[arg-type]

    assert result == {}


# --- plan_backfill: 対象抽出 ------------------------------------------------------------------


def test_plan_backfill_skips_pages_with_existing_assignee() -> None:
    pages = [_page("p1", assignees=[{"id": "user-1", "name": "田中太郎"}])]

    plan = plan_backfill(pages, {"p1": "zoho-1"}, {"zoho-1": "kanazawa@cnctor.jp"})

    assert plan.auto_assign == []
    assert plan.needs_review == []


# --- plan_backfill: 自動割当（IDマッピング解決済み・Owner設定済み・既知メール） ------------------------


def test_plan_backfill_auto_assigns_when_owner_is_known() -> None:
    pages = [_page("p1", project_name="A社案件")]
    page_id_to_zoho_deal_id = {"p1": "zoho-1"}
    zoho_deal_owner_emails = {"zoho-1": "kanazawa@cnctor.jp"}

    plan = plan_backfill(
        pages,
        page_id_to_zoho_deal_id,
        zoho_deal_owner_emails,
        known_owners=_KNOWN_OWNERS,
    )

    assert len(plan.auto_assign) == 1
    candidate = plan.auto_assign[0]
    assert candidate.page_id == "p1"
    assert candidate.project_name == "A社案件"
    assert candidate.zoho_deal_id == "zoho-1"
    assert candidate.owner_email == "kanazawa@cnctor.jp"
    assert candidate.resolved_user_id == "notion-user-kanazawa"
    assert plan.needs_review == []


def test_plan_backfill_email_matching_is_case_and_whitespace_insensitive() -> None:
    pages = [_page("p1")]
    page_id_to_zoho_deal_id = {"p1": "zoho-1"}
    # Zoho側から返るメールが大文字混じり・前後空白付きのケース。
    zoho_deal_owner_emails = {"zoho-1": "  Kanazawa@CNCTOR.JP  "}

    plan = plan_backfill(
        pages,
        page_id_to_zoho_deal_id,
        zoho_deal_owner_emails,
        known_owners=_KNOWN_OWNERS,
    )

    assert len(plan.auto_assign) == 1
    assert plan.auto_assign[0].resolved_user_id == "notion-user-kanazawa"
    assert plan.needs_review == []


def test_plan_backfill_excludes_kintone_origin_pages_from_auto_assign() -> None:
    """kintone起源の案件（IdMappingにkintone_idがある）は、他の条件が揃っていても
    自動割当には含めず要レビューへ振り分ける（同期先データ欠損リスク対応）。"""
    pages = [_page("p1")]
    page_id_to_zoho_deal_id = {"p1": "zoho-1"}
    zoho_deal_owner_emails = {"zoho-1": "kanazawa@cnctor.jp"}
    page_id_to_kintone_id = {"p1": "123"}

    plan = plan_backfill(
        pages,
        page_id_to_zoho_deal_id,
        zoho_deal_owner_emails,
        page_id_to_kintone_id,
        known_owners=_KNOWN_OWNERS,
    )

    assert plan.auto_assign == []
    assert len(plan.needs_review) == 1
    assert plan.needs_review[0].page_id == "p1"
    assert plan.needs_review[0].reason_category == "kintone_origin_excluded"
    assert "kintone起源案件" in plan.needs_review[0].reason


def test_plan_backfill_does_not_exclude_pages_without_kintone_id() -> None:
    """kintone_idマッピングが空辞書（省略時）の場合は誰も除外されない。"""
    pages = [_page("p1")]
    page_id_to_zoho_deal_id = {"p1": "zoho-1"}
    zoho_deal_owner_emails = {"zoho-1": "kanazawa@cnctor.jp"}

    plan = plan_backfill(
        pages,
        page_id_to_zoho_deal_id,
        zoho_deal_owner_emails,
        known_owners=_KNOWN_OWNERS,
    )

    assert len(plan.auto_assign) == 1
    assert plan.needs_review == []


# --- plan_backfill: レビュー行きの各パターン ------------------------------------------------------


def test_plan_backfill_needs_review_when_no_zoho_deal_id_mapping() -> None:
    pages = [_page("p1")]

    plan = plan_backfill(pages, {}, {}, known_owners=_KNOWN_OWNERS)

    assert plan.auto_assign == []
    assert len(plan.needs_review) == 1
    assert "IDマッピング未登録" in plan.needs_review[0].reason
    assert plan.needs_review[0].reason_category == "id_mapping_missing"


def test_plan_backfill_needs_review_when_zoho_deal_has_no_owner() -> None:
    pages = [_page("p1")]
    page_id_to_zoho_deal_id = {"p1": "zoho-1"}

    plan = plan_backfill(pages, page_id_to_zoho_deal_id, {}, known_owners=_KNOWN_OWNERS)

    assert plan.auto_assign == []
    assert len(plan.needs_review) == 1
    assert "Ownerが設定されていません" in plan.needs_review[0].reason
    assert plan.needs_review[0].reason_category == "owner_not_set"


def test_plan_backfill_needs_review_when_owner_not_in_known_map() -> None:
    pages = [_page("p1")]
    page_id_to_zoho_deal_id = {"p1": "zoho-1"}
    zoho_deal_owner_emails = {"zoho-1": "sugimoto@cnctor.jp"}  # 未解決の5名の1人

    plan = plan_backfill(
        pages,
        page_id_to_zoho_deal_id,
        zoho_deal_owner_emails,
        known_owners=_KNOWN_OWNERS,
    )

    assert plan.auto_assign == []
    assert len(plan.needs_review) == 1
    assert "未解決です" in plan.needs_review[0].reason
    assert plan.needs_review[0].reason_category == "owner_unresolved"


# --- plan_backfill: 複数ページ混在 --------------------------------------------------------------


def test_plan_backfill_classifies_multiple_pages_independently() -> None:
    pages = [
        _page("p1"),  # 自動割当
        _page("p2"),  # レビュー行き（IDマッピング未登録）
        _page("p3", assignees=[{"id": "user-2", "name": "鈴木花子"}]),  # 既に設定済み、対象外
    ]
    page_id_to_zoho_deal_id = {"p1": "zoho-1"}
    zoho_deal_owner_emails = {"zoho-1": "kanazawa@cnctor.jp"}

    plan = plan_backfill(
        pages,
        page_id_to_zoho_deal_id,
        zoho_deal_owner_emails,
        known_owners=_KNOWN_OWNERS,
    )

    assert [c.page_id for c in plan.auto_assign] == ["p1"]
    assert [r.page_id for r in plan.needs_review] == ["p2"]


# --- print_summary: dry-run出力フォーマット -------------------------------------------------------


def test_print_summary_dry_run_shows_would_assign_verb(capsys: Any) -> None:
    pages = [_page("p1")]
    plan = plan_backfill(
        pages,
        {"p1": "zoho-1"},
        {"zoho-1": "kanazawa@cnctor.jp"},
        known_owners=_KNOWN_OWNERS,
    )

    print_summary(plan, total_pages=1, dry_run=True)

    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "自動割当予定: 1件" in out
    assert "担当メンバー未設定の案件（Notion側フィルタ済み）: 1件" in out
    assert "対象（判定対象）: 1件" in out
    assert "p1" in out


def test_print_summary_execute_shows_assigned_verb(capsys: Any) -> None:
    pages = [_page("p1")]
    plan = plan_backfill(
        pages,
        {"p1": "zoho-1"},
        {"zoho-1": "kanazawa@cnctor.jp"},
        known_owners=_KNOWN_OWNERS,
    )

    print_summary(plan, total_pages=1, dry_run=False)

    out = capsys.readouterr().out
    assert "本番実行" in out
    assert "自動割当: 1件" in out


def test_print_summary_reports_needs_review_count_and_reason(capsys: Any) -> None:
    pages = [_page("p1", project_name="B社案件")]
    plan = plan_backfill(pages, {}, {}, known_owners=_KNOWN_OWNERS)

    print_summary(plan, total_pages=1, dry_run=True)

    out = capsys.readouterr().out
    assert "レビュー行き（自動判定できず）: 1件" in out
    assert "B社案件" in out
    assert "IDマッピング未登録" in out


def test_print_summary_reports_needs_review_reason_category_breakdown(capsys: Any) -> None:
    pages = [
        _page("p1", project_name="C社案件"),  # id_mapping_missing
        _page("p2", project_name="D社案件"),  # owner_not_set
    ]
    page_id_to_zoho_deal_id = {"p2": "zoho-2"}

    plan = plan_backfill(
        pages, page_id_to_zoho_deal_id, {}, known_owners=_KNOWN_OWNERS
    )

    print_summary(plan, total_pages=2, dry_run=True)

    out = capsys.readouterr().out
    assert "IDマッピング未登録: 1件" in out
    assert "Zoho Owner未設定: 1件" in out


def test_print_summary_reports_kintone_origin_excluded_in_needs_review_breakdown(
    capsys: Any,
) -> None:
    pages = [_page("p1"), _page("p2")]
    page_id_to_zoho_deal_id = {"p1": "zoho-1", "p2": "zoho-2"}
    zoho_deal_owner_emails = {"zoho-1": "kanazawa@cnctor.jp", "zoho-2": "kunikata@cnctor.jp"}
    page_id_to_kintone_id = {"p1": "123"}

    plan = plan_backfill(
        pages,
        page_id_to_zoho_deal_id,
        zoho_deal_owner_emails,
        page_id_to_kintone_id,
        known_owners=_KNOWN_OWNERS,
    )

    print_summary(plan, total_pages=2, dry_run=True)

    out = capsys.readouterr().out
    # kintone起源のp1は自動割当には含まれず、要レビューの理由別内訳に計上される。
    assert len(plan.auto_assign) == 1
    assert "自動割当予定: 1件" in out
    assert "kintone起源のため対象外: 1件" in out


# --- execute_assignments: TOCTOU再確認・失敗継続 -----------------------------------------------


def test_execute_assignments_writes_and_reports_success() -> None:
    candidate = _candidate("p1")
    client = _FakeNotionClient(pages_by_id={"p1": {"担当メンバー": []}})

    result = execute_assignments(client, [candidate])  # type: ignore[arg-type]

    assert result.succeeded == [candidate]
    assert result.skipped == []
    assert result.failed == []
    assert client.updated == [("p1", {"担当メンバー": ["notion-user-kanazawa"]})]


def test_execute_assignments_skips_when_already_assigned_at_write_time() -> None:
    candidate = _candidate("p1")
    # 直前の再確認時点で既に別のユーザーが設定されている(TOCTOUで手動設定と競合したケース)。
    client = _FakeNotionClient(pages_by_id={"p1": {"担当メンバー": ["someone-else"]}})

    result = execute_assignments(client, [candidate])  # type: ignore[arg-type]

    assert result.succeeded == []
    assert result.skipped == [candidate]
    assert result.failed == []
    assert client.updated == []


def test_execute_assignments_skips_when_page_missing_at_write_time() -> None:
    candidate = _candidate("p1")
    client = _FakeNotionClient(pages_by_id={})  # get_pageがNoneを返す(ページ未検出)

    result = execute_assignments(client, [candidate])  # type: ignore[arg-type]

    assert result.succeeded == []
    assert result.skipped == [candidate]
    assert result.failed == []


def test_execute_assignments_continues_after_one_failure() -> None:
    candidate1 = _candidate("p1", project_name="1件目")
    candidate2 = _candidate("p2", project_name="2件目")
    client = _FakeNotionClient(
        pages_by_id={"p1": {"担当メンバー": []}, "p2": {"担当メンバー": []}}
    )
    client.set_update_page_error("p1", RuntimeError("notion api error"))

    result = execute_assignments(client, [candidate1, candidate2])  # type: ignore[arg-type]

    assert result.succeeded == [candidate2]
    assert len(result.failed) == 1
    assert result.failed[0].candidate == candidate1
    assert "notion api error" in result.failed[0].error
    # 1件目が失敗しても2件目は書き込まれている。
    assert client.updated == [("p2", {"担当メンバー": ["notion-user-kanazawa"]})]


def test_execution_result_is_a_dataclass_with_expected_fields() -> None:
    result = ExecutionResult(succeeded=[], skipped=[], failed=[])
    assert result.succeeded == []
    assert result.skipped == []
    assert result.failed == []
