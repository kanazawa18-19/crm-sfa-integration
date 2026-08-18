from __future__ import annotations

from datetime import date

import pytest

from src.document_generation.application_generator import generate_application
from src.document_generation.common import TemplateNotFoundError, TemplateSheetNotFoundError
from src.document_generation.template_registry import TemplateInfo
from tests.document_generation._fakes import (
    FakeClientMasterClient,
    FakeGoogleDriveDocClient,
    FakeProjectNotionClient,
    FakeSheetsClient,
    FakeTemplateRegistry,
    build_raw_project_page,
)

PAGE_ID = "abcd1234-0000-0000-0000-000000000000"
SHEET_NAME = "案件Aタブ"


def test_generate_application_copies_fills_exports_and_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.document_generation.application_generator._today_jst", lambda: date(2026, 8, 7)
    )
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["メイリー"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="メイリー_申込書.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("申込書", "メイリー"): template})
    drive_client = FakeGoogleDriveDocClient()
    rows = [
        ["", "", "", "", "", "発行日：", "", "2026/8/7", "", ""],
        ["〇〇　御中", "", "", "", "", "", "", "", "", ""],
        ["", "", "件名：", "", "", "", "", "", "", ""],
    ]
    sheets_client = FakeSheetsClient(rows, sheet_title=SHEET_NAME)

    result = generate_application(
        PAGE_ID,
        registry=registry,
        drive_client=drive_client,
        sheets_client=sheets_client,
        notion_client=FakeProjectNotionClient(raw_page),
        client_master_client=FakeClientMasterClient("テスト商店"),
    )

    assert drive_client.copy_calls == [
        {
            "file_id": "TEMPLATE_ID",
            "target_mime_type": "application/vnd.google-apps.spreadsheet",
            "new_name": f"__tmp_application_{PAGE_ID}",
            "parents": None,
        }
    ]
    assert drive_client.export_calls == [
        {
            "file_id": "copy-123",
            "mime_type": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        }
    ]
    assert drive_client.deleted_ids == ["copy-123"]

    assert sheets_client.updates[f"'{SHEET_NAME}'!H1"] == "2026/08/07"
    assert sheets_client.updates[f"'{SHEET_NAME}'!A2"] == "テスト商店　御中"
    assert sheets_client.updates[f"'{SHEET_NAME}'!D3"] == "テスト案件"
    # Drive APIのexportはワークブック全体を書き出してしまうため、対象タブ以外を削除して
    # から export する必要がある（情報漏洩リスク対応の回帰確認）。
    assert sheets_client.keep_only_sheet_calls == [
        {"spreadsheet_id": "copy-123", "sheet_id": sheets_client.sheet_id}
    ]

    assert result.file_name == "テスト案件_申込書.xlsx"
    assert result.mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_generate_application_raises_template_sheet_not_found_when_template_tab_missing() -> None:
    """実データ回帰確認: テンプレートのスプレッドシートに「雛形」タブが無い場合、
    他クライアントのタブを誤って使わずエラーにする。"""
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["メイリー"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="メイリー_申込書.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("申込書", "メイリー"): template})
    drive_client = FakeGoogleDriveDocClient()
    sheets_client = FakeSheetsClient(has_template_sheet=False)

    with pytest.raises(TemplateSheetNotFoundError):
        generate_application(
            PAGE_ID,
            registry=registry,
            drive_client=drive_client,
            sheets_client=sheets_client,
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient(),
        )

    # コピー自体は作られているため、一時ファイルの削除は行われる。
    assert drive_client.deleted_ids == ["copy-123"]


def test_generate_application_raises_template_not_found_when_no_matching_template() -> None:
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"])
    registry = FakeTemplateRegistry({})
    drive_client = FakeGoogleDriveDocClient()

    with pytest.raises(TemplateNotFoundError):
        generate_application(
            PAGE_ID,
            registry=registry,
            drive_client=drive_client,
            sheets_client=FakeSheetsClient(),
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient(),
        )

    assert drive_client.copy_calls == []
