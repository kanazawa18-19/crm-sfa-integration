from __future__ import annotations

from datetime import date

import pytest

from src.document_generation.common import TemplateNotFoundError
from src.document_generation.quote_generator import generate_quote
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


def test_generate_quote_copies_fills_exports_and_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.document_generation.quote_generator._today_jst", lambda: date(2026, 8, 7))
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="リピッテホテル_見積書.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()
    rows = [
        ["", "", "", "", "", "発行日：", "", "2026/8/7", "", ""],
        ["", "", "", "", "", "見積書NO：", "", "CN20251001K01", "", ""],
        ["〇〇　御中", "", "", "", "", "", "", "", "", ""],
        ["", "", "件名：", "", "", "", "", "", "", ""],
    ]
    sheets_client = FakeSheetsClient(rows, first_sheet_title=SHEET_NAME)

    result = generate_quote(
        PAGE_ID,
        registry=registry,
        drive_client=drive_client,
        sheets_client=sheets_client,
        notion_client=FakeProjectNotionClient(raw_page),
        client_master_client=FakeClientMasterClient("テスト商店"),
    )

    # コピー -> 書き込み -> export -> 削除の順序・引数を検証。
    assert drive_client.copy_calls == [
        {
            "file_id": "TEMPLATE_ID",
            "target_mime_type": "application/vnd.google-apps.spreadsheet",
            "new_name": f"__tmp_quote_{PAGE_ID}",
        }
    ]
    assert drive_client.export_calls == [{"file_id": "copy-123", "mime_type": "application/pdf"}]
    assert drive_client.deleted_ids == ["copy-123"]

    assert sheets_client.updates[f"'{SHEET_NAME}'!H1"] == "2026/08/07"
    assert sheets_client.updates[f"'{SHEET_NAME}'!H2"] == "CN20260807ABCD"
    assert sheets_client.updates[f"'{SHEET_NAME}'!A3"] == "テスト商店　御中"
    assert sheets_client.updates[f"'{SHEET_NAME}'!D4"] == "テスト案件"

    assert result.content == b"binary-content"
    assert result.mime_type == "application/pdf"
    assert result.file_name == "テスト案件_見積書.pdf"


def test_generate_quote_deletes_copy_even_when_export_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.document_generation.quote_generator._today_jst", lambda: date(2026, 8, 7))
    raw_page = build_raw_project_page(page_id=PAGE_ID)
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="x.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()

    def _raise_export(*args: object, **kwargs: object) -> bytes:
        raise RuntimeError("export failed")

    drive_client.export = _raise_export  # type: ignore[assignment]
    sheets_client = FakeSheetsClient([], first_sheet_title=SHEET_NAME)

    with pytest.raises(RuntimeError):
        generate_quote(
            PAGE_ID,
            registry=registry,
            drive_client=drive_client,
            sheets_client=sheets_client,
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient("テスト商店"),
        )

    assert drive_client.deleted_ids == ["copy-123"]


def test_generate_quote_raises_template_not_found_when_service_unmapped() -> None:
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["その他"])
    registry = FakeTemplateRegistry({})
    drive_client = FakeGoogleDriveDocClient()

    with pytest.raises(TemplateNotFoundError):
        generate_quote(
            PAGE_ID,
            registry=registry,
            drive_client=drive_client,
            sheets_client=FakeSheetsClient(),
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient(),
        )

    assert drive_client.copy_calls == []


def test_generate_quote_raises_template_not_found_when_no_matching_template_in_db() -> None:
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"])
    registry = FakeTemplateRegistry({})  # DB未登録
    drive_client = FakeGoogleDriveDocClient()

    with pytest.raises(TemplateNotFoundError):
        generate_quote(
            PAGE_ID,
            registry=registry,
            drive_client=drive_client,
            sheets_client=FakeSheetsClient(),
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient(),
        )

    assert drive_client.copy_calls == []


def test_generate_quote_adds_note_when_client_name_missing() -> None:
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"], client_master_ids=[])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="x.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()
    sheets_client = FakeSheetsClient([], first_sheet_title=SHEET_NAME)

    result = generate_quote(
        PAGE_ID,
        registry=registry,
        drive_client=drive_client,
        sheets_client=sheets_client,
        notion_client=FakeProjectNotionClient(raw_page),
        client_master_client=FakeClientMasterClient(),
    )

    assert any("取引先名" in note for note in result.notes)
