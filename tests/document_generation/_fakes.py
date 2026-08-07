"""tests/document_generation配下で共有するテストヘルパー・フェイク実装。"""

from __future__ import annotations

from typing import Any

from src.document_generation.template_registry import TemplateInfo


def build_raw_project_page(
    *,
    page_id: str = "26d6f1e2-0000-0000-0000-000000000000",
    project_name: str = "テスト案件",
    proposed_services: list[str] | None = None,
    memo: str | None = "テスト備考",
    assignee_id: str = "user-1",
    assignee_name: str | None = "金沢",
    client_master_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Notion API `GET /v1/pages/{id}` 相当の生ページJSON（案件管理DB）を組み立てる。"""
    resolved_services = proposed_services if proposed_services is not None else ["リピッテ"]
    resolved_client_ids = client_master_ids if client_master_ids is not None else ["client-1"]
    people = [{"id": assignee_id, "name": assignee_name}] if assignee_name is not None else []
    return {
        "id": page_id,
        "properties": {
            "案件名": {"type": "title", "title": [{"plain_text": project_name}]},
            "提案サービス": {
                "type": "multi_select",
                "multi_select": [{"name": s} for s in resolved_services],
            },
            "メモ": {
                "type": "rich_text",
                "rich_text": ([{"plain_text": memo}] if memo else []),
            },
            "担当メンバー": {"type": "people", "people": people},
            "取引先マスター": {
                "type": "relation",
                "relation": [{"id": cid} for cid in resolved_client_ids],
            },
        },
    }


class FakeProjectNotionClient:
    def __init__(self, raw_page: dict[str, Any]) -> None:
        self._raw_page = raw_page

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        return self._raw_page


class FakeClientMasterClient:
    def __init__(self, client_name: str | None = "テスト商店") -> None:
        self._client_name = client_name

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        if self._client_name is None:
            return None
        return {"取引先名": self._client_name}


class FakeTemplateRegistry:
    def __init__(self, templates: dict[tuple[str, str], TemplateInfo]) -> None:
        self._templates = templates
        self.calls: list[tuple[str, str]] = []

    def find_template(self, category: str, service: str) -> TemplateInfo | None:
        self.calls.append((category, service))
        return self._templates.get((category, service))


class FakeGoogleDriveDocClient:
    def __init__(self, *, copy_id: str = "copy-123", exported_content: bytes = b"binary-content") -> None:
        self.copy_id = copy_id
        self.exported_content = exported_content
        self.copy_calls: list[dict[str, Any]] = []
        self.export_calls: list[dict[str, Any]] = []
        self.deleted_ids: list[str] = []

    def copy_as_native(self, file_id: str, *, target_mime_type: str, new_name: str) -> str:
        self.copy_calls.append(
            {"file_id": file_id, "target_mime_type": target_mime_type, "new_name": new_name}
        )
        return self.copy_id

    def export(self, file_id: str, *, mime_type: str) -> bytes:
        self.export_calls.append({"file_id": file_id, "mime_type": mime_type})
        return self.exported_content

    def delete(self, file_id: str) -> None:
        self.deleted_ids.append(file_id)


class FakeSheetsClient:
    def __init__(self, rows: list[list[Any]] | None = None, *, first_sheet_title: str = "案件タブ1") -> None:
        self.rows = rows or []
        self.first_sheet_title = first_sheet_title
        self.updates: dict[str, str] = {}

    def get_values(self, spreadsheet_id: str, range_: str) -> list[list[Any]]:
        return self.rows

    def update_value(self, spreadsheet_id: str, cell: str, value: str) -> None:
        self.updates[cell] = value

    def get_first_sheet_title(self, spreadsheet_id: str) -> str:
        return self.first_sheet_title


class FakeDocsClient:
    def __init__(self, *, occurrences: int = 1) -> None:
        self.replace_calls: list[dict[str, str]] = []
        self._occurrences = occurrences

    def replace_all_text(self, document_id: str, *, search_text: str, replace_text: str) -> int:
        self.replace_calls.append(
            {"document_id": document_id, "search_text": search_text, "replace_text": replace_text}
        )
        return self._occurrences
