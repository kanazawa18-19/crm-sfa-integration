from __future__ import annotations

from typing import Any

from src.meeting_sync.matcher import find_matching_project

_EMAIL_PROPERTY = "メールアドレス"
_CLIENT_MASTER_PROPERTY = "取引先マスター"
_STATUS_PROPERTY = "営業ステータス"


def _contact_page(page_id: str, email: str, client_master_ids: list[str]) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            _EMAIL_PROPERTY: {"type": "email", "email": email},
            _CLIENT_MASTER_PROPERTY: {
                "type": "relation",
                "relation": [{"id": cid} for cid in client_master_ids],
            },
        },
    }


def _project_page(page_id: str, status: str) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {_STATUS_PROPERTY: {"type": "status", "status": {"name": status}}},
    }


class FakeContactClient:
    """email -> 連絡先ページ（無ければ検索結果は空）。"""

    def __init__(self, contacts_by_email: dict[str, dict[str, Any]]) -> None:
        self._by_email = contacts_by_email
        self._by_id = {page["id"]: page for page in contacts_by_email.values()}

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        email = filter["email"]["equals"]  # type: ignore[index]
        page = self._by_email.get(email)
        return [page] if page else []

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        return self._by_id[page_id]


class FakeProjectClient:
    """取引先マスターpage_id -> 案件ページ一覧。"""

    def __init__(self, projects_by_client_master: dict[str, list[dict[str, Any]]]) -> None:
        self._by_client_master = projects_by_client_master

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        client_master_id = filter["relation"]["contains"]  # type: ignore[index]
        return self._by_client_master.get(client_master_id, [])

    def get_raw_page(self, page_id: str) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("matcher should not call get_raw_page on the project client")


def test_returns_project_when_single_attendee_matches_single_active_project() -> None:
    contact = FakeContactClient(
        {"yamada@example.com": _contact_page("contact-1", "yamada@example.com", ["client-1"])}
    )
    project = FakeProjectClient({"client-1": [_project_page("project-1", "口頭受注")]})

    result = find_matching_project(["yamada@example.com"], contact, project)

    assert result == "project-1"


def test_returns_none_when_attendee_email_not_in_contacts() -> None:
    contact = FakeContactClient({})
    project = FakeProjectClient({})

    result = find_matching_project(["unknown@example.com"], contact, project)

    assert result is None


def test_returns_none_when_client_master_has_no_projects() -> None:
    contact = FakeContactClient(
        {"yamada@example.com": _contact_page("contact-1", "yamada@example.com", ["client-1"])}
    )
    project = FakeProjectClient({})

    result = find_matching_project(["yamada@example.com"], contact, project)

    assert result is None


def test_returns_none_when_client_master_has_multiple_active_projects() -> None:
    contact = FakeContactClient(
        {"yamada@example.com": _contact_page("contact-1", "yamada@example.com", ["client-1"])}
    )
    project = FakeProjectClient(
        {
            "client-1": [
                _project_page("project-1", "口頭受注"),
                _project_page("project-2", "与件整理"),
            ]
        }
    )

    result = find_matching_project(["yamada@example.com"], contact, project)

    assert result is None


def test_two_attendees_converging_on_same_project_still_resolves() -> None:
    contact = FakeContactClient(
        {
            "yamada@example.com": _contact_page("contact-1", "yamada@example.com", ["client-1"]),
            "suzuki@example.com": _contact_page("contact-2", "suzuki@example.com", ["client-1"]),
        }
    )
    project = FakeProjectClient({"client-1": [_project_page("project-1", "口頭受注")]})

    result = find_matching_project(
        ["yamada@example.com", "suzuki@example.com"], contact, project
    )

    assert result == "project-1"


def test_two_attendees_from_different_client_masters_is_ambiguous() -> None:
    contact = FakeContactClient(
        {
            "yamada@example.com": _contact_page("contact-1", "yamada@example.com", ["client-1"]),
            "suzuki@example.com": _contact_page("contact-2", "suzuki@example.com", ["client-2"]),
        }
    )
    project = FakeProjectClient(
        {
            "client-1": [_project_page("project-1", "口頭受注")],
            "client-2": [_project_page("project-2", "口頭受注")],
        }
    )

    result = find_matching_project(
        ["yamada@example.com", "suzuki@example.com"], contact, project
    )

    assert result is None


def test_confirmed_status_project_is_not_treated_as_active() -> None:
    contact = FakeContactClient(
        {"yamada@example.com": _contact_page("contact-1", "yamada@example.com", ["client-1"])}
    )
    project = FakeProjectClient({"client-1": [_project_page("project-1", "契約")]})

    result = find_matching_project(["yamada@example.com"], contact, project)

    assert result is None


def test_unknown_status_value_is_excluded_without_raising() -> None:
    contact = FakeContactClient(
        {"yamada@example.com": _contact_page("contact-1", "yamada@example.com", ["client-1"])}
    )
    project = FakeProjectClient(
        {"client-1": [_project_page("project-1", "存在しないステータス")]}
    )

    result = find_matching_project(["yamada@example.com"], contact, project)

    assert result is None


def test_internal_domain_attendee_is_excluded_from_matching() -> None:
    contact = FakeContactClient(
        {"sales@cnctor.jp": _contact_page("contact-1", "sales@cnctor.jp", ["client-1"])}
    )
    project = FakeProjectClient({"client-1": [_project_page("project-1", "口頭受注")]})

    result = find_matching_project(
        ["sales@cnctor.jp"], contact, project, internal_domains=frozenset({"cnctor.jp"})
    )

    assert result is None
