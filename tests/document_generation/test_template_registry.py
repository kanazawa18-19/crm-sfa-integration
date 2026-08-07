"""TemplateRegistryの単体テスト（実HTTP通信はrequests_mockでモック）。"""

from __future__ import annotations

import pytest

from src.document_generation.template_registry import (
    TemplateRegistry,
    TemplateRegistryError,
)

DATABASE_ID = "0f60fd81-990d-4f7e-870c-6a4c52615e5a"
BASE = "https://api.notion.com/v1"


@pytest.fixture
def registry() -> TemplateRegistry:
    return TemplateRegistry(database_id=DATABASE_ID, api_key="secret-notion-key")


def _external_file_page(name: str, url: str) -> dict:
    return {
        "properties": {
            "ファイル&メディア": {
                "files": [
                    {"type": "external", "name": name, "external": {"url": url}},
                ]
            }
        }
    }


def test_find_template_extracts_file_id_from_external_url(
    requests_mock, registry: TemplateRegistry
) -> None:
    requests_mock.post(
        f"{BASE}/databases/{DATABASE_ID}/query",
        json={
            "results": [
                _external_file_page(
                    "リピッテホテル_見積書.xlsx",
                    "https://docs.google.com/spreadsheets/d/FILE_ID_123/edit?gid=0",
                )
            ]
        },
    )

    template = registry.find_template("見積書", "リピッテホテル")

    assert template is not None
    assert template.file_id == "FILE_ID_123"
    assert template.file_name == "リピッテホテル_見積書.xlsx"
    assert template.mime_type_hint == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_find_template_sends_category_and_service_filter(
    requests_mock, registry: TemplateRegistry
) -> None:
    requests_mock.post(f"{BASE}/databases/{DATABASE_ID}/query", json={"results": []})

    registry.find_template("契約書", "ホテルラボ")

    sent_body = requests_mock.last_request.json()
    assert sent_body == {
        "filter": {
            "and": [
                {"property": "カテゴリ", "select": {"equals": "契約書"}},
                {"property": "サービス", "multi_select": {"contains": "ホテルラボ"}},
            ]
        }
    }


def test_find_template_returns_none_when_no_results(
    requests_mock, registry: TemplateRegistry
) -> None:
    requests_mock.post(f"{BASE}/databases/{DATABASE_ID}/query", json={"results": []})

    assert registry.find_template("見積書", "デザ丸") is None


def test_find_template_returns_none_when_only_notion_attached_files(
    requests_mock, registry: TemplateRegistry
) -> None:
    """`file`タイプ（Notion添付ファイル）はスコープ外のため無視され、Noneが返る。"""
    requests_mock.post(
        f"{BASE}/databases/{DATABASE_ID}/query",
        json={
            "results": [
                {
                    "properties": {
                        "ファイル&メディア": {
                            "files": [{"type": "file", "name": "規約.pdf", "file": {"url": "https://signed"}}]
                        }
                    }
                }
            ]
        },
    )

    assert registry.find_template("規約", "オルト") is None


def test_find_template_skips_pages_without_extractable_file_id(
    requests_mock, registry: TemplateRegistry
) -> None:
    requests_mock.post(
        f"{BASE}/databases/{DATABASE_ID}/query",
        json={
            "results": [
                _external_file_page("壊れたURL", "https://example.com/no-id-here"),
                _external_file_page(
                    "正常なテンプレート.docx",
                    "https://docs.google.com/document/d/GOOD_ID/edit",
                ),
            ]
        },
    )

    template = registry.find_template("契約書", "ILCA")

    assert template is not None
    assert template.file_id == "GOOD_ID"


def test_find_template_raises_on_5xx(
    requests_mock, registry: TemplateRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.post(f"{BASE}/databases/{DATABASE_ID}/query", status_code=500, json={"error": "boom"})

    with pytest.raises(TemplateRegistryError):
        registry.find_template("見積書", "メイリー")


def test_raises_value_error_when_api_key_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)

    with pytest.raises(ValueError, match="NOTION_API_KEY"):
        TemplateRegistry(database_id=DATABASE_ID)
