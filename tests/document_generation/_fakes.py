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
        self.update_calls: list[dict[str, Any]] = []

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        return self._raw_page

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
        self.update_calls.append({"page_id": page_id, "properties": properties})


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
    def __init__(
        self,
        *,
        copy_id: str = "copy-123",
        exported_content: bytes = b"binary-content",
        approval_id: str = "approval-1",
        approval_state: dict[str, Any] | None = None,
    ) -> None:
        self.copy_id = copy_id
        self.exported_content = exported_content
        self.approval_id = approval_id
        self.approval_state = approval_state if approval_state is not None else {"state": "APPROVED"}
        self.copy_calls: list[dict[str, Any]] = []
        self.export_calls: list[dict[str, Any]] = []
        self.deleted_ids: list[str] = []
        self.move_calls: list[dict[str, Any]] = []
        self.rename_calls: list[dict[str, Any]] = []
        self.replace_content_calls: list[dict[str, Any]] = []
        self.start_approval_calls: list[dict[str, Any]] = []
        self.get_approval_calls: list[dict[str, Any]] = []
        self.cancel_approval_calls: list[dict[str, Any]] = []

    def copy_as_native(
        self, file_id: str, *, target_mime_type: str, new_name: str, parents: list[str] | None = None
    ) -> str:
        self.copy_calls.append(
            {
                "file_id": file_id,
                "target_mime_type": target_mime_type,
                "new_name": new_name,
                "parents": parents,
            }
        )
        return self.copy_id

    def export(self, file_id: str, *, mime_type: str) -> bytes:
        self.export_calls.append({"file_id": file_id, "mime_type": mime_type})
        return self.exported_content

    def delete(self, file_id: str) -> None:
        self.deleted_ids.append(file_id)

    def move(self, file_id: str, *, add_parent: str, remove_parent: str) -> None:
        self.move_calls.append({"file_id": file_id, "add_parent": add_parent, "remove_parent": remove_parent})

    def rename(self, file_id: str, *, name: str) -> None:
        self.rename_calls.append({"file_id": file_id, "name": name})

    def replace_content(self, file_id: str, *, content: bytes, mime_type: str) -> None:
        self.replace_content_calls.append({"file_id": file_id, "content": content, "mime_type": mime_type})

    def start_approval(self, file_id: str, *, reviewer_emails: list[str], message: str = "") -> str:
        self.start_approval_calls.append(
            {"file_id": file_id, "reviewer_emails": reviewer_emails, "message": message}
        )
        return self.approval_id

    def get_approval(self, file_id: str, approval_id: str) -> dict[str, Any]:
        self.get_approval_calls.append({"file_id": file_id, "approval_id": approval_id})
        return self.approval_state

    def cancel_approval(self, file_id: str, approval_id: str) -> None:
        self.cancel_approval_calls.append({"file_id": file_id, "approval_id": approval_id})


class FakeSheetsClient:
    def __init__(
        self,
        rows: list[list[Any]] | None = None,
        *,
        sheet_title: str = "雛形",
        sheet_id: int = 1,
        has_template_sheet: bool = True,
    ) -> None:
        self.rows = rows or []
        self.sheet_title = sheet_title
        self.sheet_id = sheet_id
        self.has_template_sheet = has_template_sheet
        self.updates: dict[str, str] = {}
        self.keep_only_sheet_calls: list[dict[str, Any]] = []

    def get_values(self, spreadsheet_id: str, range_: str) -> list[list[Any]]:
        return self.rows

    def update_value(self, spreadsheet_id: str, cell: str, value: str) -> None:
        self.updates[cell] = value

    def find_sheet(self, spreadsheet_id: str, *, exact_title: str) -> tuple[str, int] | None:
        if not self.has_template_sheet:
            return None
        return self.sheet_title, self.sheet_id

    def keep_only_sheet(self, spreadsheet_id: str, *, sheet_id: int) -> None:
        self.keep_only_sheet_calls.append({"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id})


class FakeDocsClient:
    def __init__(self, *, occurrences: int = 1) -> None:
        self.replace_calls: list[dict[str, str]] = []
        self._occurrences = occurrences

    def replace_all_text(self, document_id: str, *, search_text: str, replace_text: str) -> int:
        self.replace_calls.append(
            {"document_id": document_id, "search_text": search_text, "replace_text": replace_text}
        )
        return self._occurrences
