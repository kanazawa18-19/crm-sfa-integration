from __future__ import annotations

import pytest

from src.document_generation.common import ContractGenerationError, TemplateNotFoundError
from src.document_generation.contract_generator import (
    DocsApiError,
    GoogleDocsTextReplacer,
    generate_contract,
)
from src.document_generation.template_registry import TemplateInfo
from tests.document_generation._fakes import (
    FakeClientMasterClient,
    FakeDocsClient,
    FakeGoogleDriveDocClient,
    FakeProjectNotionClient,
    FakeTemplateRegistry,
    build_raw_project_page,
)

PAGE_ID = "abcd1234-0000-0000-0000-000000000000"
DOCS_BASE = "https://docs.googleapis.com/v1/documents"


def test_generate_contract_copies_replaces_placeholder_exports_and_deletes() -> None:
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["ILCA（三密代官、HOTEL DX）"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="ILCA_契約書.docx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("契約書", "ILCA"): template})
    drive_client = FakeGoogleDriveDocClient()
    docs_client = FakeDocsClient()

    result = generate_contract(
        PAGE_ID,
        registry=registry,
        drive_client=drive_client,
        docs_client=docs_client,
        notion_client=FakeProjectNotionClient(raw_page),
        client_master_client=FakeClientMasterClient("テスト商店"),
    )

    assert drive_client.copy_calls == [
        {
            "file_id": "TEMPLATE_ID",
            "target_mime_type": "application/vnd.google-apps.document",
            "new_name": f"__tmp_contract_{PAGE_ID}",
        }
    ]
    assert docs_client.replace_calls == [
        {"document_id": "copy-123", "search_text": "〇〇", "replace_text": "テスト商店"}
    ]
    assert drive_client.export_calls == [
        {
            "file_id": "copy-123",
            "mime_type": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        }
    ]
    assert drive_client.deleted_ids == ["copy-123"]

    assert result.file_name == "テスト案件_契約書.docx"
    assert result.mime_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    # BASELINE_NOTE（全生成器共通の確認喚起）のみが含まれ、宛先未反映等の追加notesは無い。
    assert len(result.notes) == 1


def test_generate_contract_skips_replace_and_adds_note_when_client_name_missing() -> None:
    raw_page = build_raw_project_page(
        page_id=PAGE_ID, proposed_services=["ILCA（三密代官、HOTEL DX）"], client_master_ids=[]
    )
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="ILCA_契約書.docx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("契約書", "ILCA"): template})
    drive_client = FakeGoogleDriveDocClient()
    docs_client = FakeDocsClient()

    result = generate_contract(
        PAGE_ID,
        registry=registry,
        drive_client=drive_client,
        docs_client=docs_client,
        notion_client=FakeProjectNotionClient(raw_page),
        client_master_client=FakeClientMasterClient(),
    )

    assert docs_client.replace_calls == []
    assert any("取引先名" in note for note in result.notes)
    # 宛先が無くても一時コピーの削除は必ず行う。
    assert drive_client.deleted_ids == ["copy-123"]


def test_generate_contract_raises_template_not_found_when_service_unmapped() -> None:
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["ビールオーダー"])
    registry = FakeTemplateRegistry({})
    drive_client = FakeGoogleDriveDocClient()

    with pytest.raises(TemplateNotFoundError):
        generate_contract(
            PAGE_ID,
            registry=registry,
            drive_client=drive_client,
            docs_client=FakeDocsClient(),
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient(),
        )

    assert drive_client.copy_calls == []


def test_generate_contract_raises_when_placeholder_occurs_multiple_times() -> None:
    """BLOCKER回帰確認: 「〇〇」がテンプレート本文の複数箇所（日付欄等）に出現する場合、
    無条件の全置換では意図しない箇所まで取引先名で書き換えてしまう。占有件数が1件以外
    なら生成自体を失敗させ、そのまま送付されてしまう事故を防ぐ。
    """
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["ILCA（三密代官、HOTEL DX）"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="ILCA_契約書.docx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("契約書", "ILCA"): template})
    drive_client = FakeGoogleDriveDocClient()
    docs_client = FakeDocsClient(occurrences=3)

    with pytest.raises(ContractGenerationError):
        generate_contract(
            PAGE_ID,
            registry=registry,
            drive_client=drive_client,
            docs_client=docs_client,
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient("テスト商店"),
        )

    # 生成は失敗させるが、一時コピーの削除は必ず行う。
    assert drive_client.deleted_ids == ["copy-123"]


def test_generate_contract_raises_when_placeholder_occurs_zero_times() -> None:
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["ILCA（三密代官、HOTEL DX）"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="ILCA_契約書.docx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("契約書", "ILCA"): template})
    drive_client = FakeGoogleDriveDocClient()
    docs_client = FakeDocsClient(occurrences=0)

    with pytest.raises(ContractGenerationError):
        generate_contract(
            PAGE_ID,
            registry=registry,
            drive_client=drive_client,
            docs_client=docs_client,
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient("テスト商店"),
        )

    assert drive_client.deleted_ids == ["copy-123"]


def test_generate_contract_deletes_copy_even_when_export_fails() -> None:
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["ILCA（三密代官、HOTEL DX）"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="ILCA_契約書.docx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("契約書", "ILCA"): template})
    drive_client = FakeGoogleDriveDocClient()

    def _raise_export(*args: object, **kwargs: object) -> bytes:
        raise RuntimeError("export failed")

    drive_client.export = _raise_export  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        generate_contract(
            PAGE_ID,
            registry=registry,
            drive_client=drive_client,
            docs_client=FakeDocsClient(),
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient("テスト商店"),
        )

    assert drive_client.deleted_ids == ["copy-123"]


# --- GoogleDocsTextReplacer（実HTTP通信部分） --------------------------------------------------


def test_google_docs_text_replacer_sends_replace_all_text_request(requests_mock) -> None:
    requests_mock.post(
        f"{DOCS_BASE}/doc-1:batchUpdate",
        json={"replies": [{"replaceAllText": {"occurrencesChanged": 1}}]},
    )
    replacer = GoogleDocsTextReplacer(access_token="secret-access-token")

    occurrences = replacer.replace_all_text(
        "doc-1", search_text="〇〇", replace_text="テスト商店"
    )

    assert occurrences == 1
    sent_body = requests_mock.last_request.json()
    assert sent_body == {
        "requests": [
            {
                "replaceAllText": {
                    "containsText": {"text": "〇〇", "matchCase": True},
                    "replaceText": "テスト商店",
                }
            }
        ]
    }
    assert requests_mock.last_request.headers["Authorization"] == "Bearer secret-access-token"


def test_google_docs_text_replacer_raises_on_5xx(
    requests_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.post(f"{DOCS_BASE}/doc-1:batchUpdate", status_code=500, json={"error": "boom"})
    replacer = GoogleDocsTextReplacer(access_token="secret-access-token")

    with pytest.raises(DocsApiError):
        replacer.replace_all_text("doc-1", search_text="〇〇", replace_text="テスト商店")


def test_google_docs_text_replacer_raises_value_error_when_access_token_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)

    with pytest.raises(ValueError, match="GOOGLE_ACCESS_TOKEN"):
        GoogleDocsTextReplacer().replace_all_text("doc-1", search_text="x", replace_text="y")
