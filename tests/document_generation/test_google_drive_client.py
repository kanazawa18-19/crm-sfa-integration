"""GoogleDriveDocClientの単体テスト（実HTTP通信はrequests_mockでモック）。

共有ドライブ上のテンプレートを扱うため、全リクエストに`supportsAllDrives=true`が
付与されていることを重点的に検証する（実データ確認済みの必須要件、付け忘れると404になる）。
"""

from __future__ import annotations

import pytest

from src.document_generation.google_drive_client import (
    GoogleDriveApiError,
    GoogleDriveDocClient,
)

BASE = "https://www.googleapis.com/drive/v3/files"


@pytest.fixture
def client() -> GoogleDriveDocClient:
    return GoogleDriveDocClient(access_token="secret-access-token")


def test_raises_value_error_when_access_token_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)

    with pytest.raises(ValueError, match="GOOGLE_ACCESS_TOKEN"):
        GoogleDriveDocClient().get_mime_type("file-1")


def test_get_mime_type_sends_supports_all_drives(requests_mock, client: GoogleDriveDocClient) -> None:
    requests_mock.get(f"{BASE}/file-1", json={"mimeType": "application/vnd.google-apps.spreadsheet"})

    mime_type = client.get_mime_type("file-1")

    assert mime_type == "application/vnd.google-apps.spreadsheet"
    assert requests_mock.last_request.qs["supportsalldrives"] == ["true"]


def test_copy_as_native_sends_target_mime_type_and_returns_new_file_id(
    requests_mock, client: GoogleDriveDocClient
) -> None:
    requests_mock.post(f"{BASE}/file-1/copy", json={"id": "copy-99"})

    new_id = client.copy_as_native(
        "file-1", target_mime_type="application/vnd.google-apps.spreadsheet", new_name="__tmp_x"
    )

    assert new_id == "copy-99"
    sent_body = requests_mock.last_request.json()
    assert sent_body == {"mimeType": "application/vnd.google-apps.spreadsheet", "name": "__tmp_x"}
    assert requests_mock.last_request.qs["supportsalldrives"] == ["true"]


def test_copy_as_native_does_not_retry_on_5xx(
    requests_mock, client: GoogleDriveDocClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    copy_mock = requests_mock.post(f"{BASE}/file-1/copy", status_code=500, json={"error": "boom"})

    with pytest.raises(GoogleDriveApiError):
        client.copy_as_native(
            "file-1", target_mime_type="application/vnd.google-apps.spreadsheet", new_name="x"
        )

    assert copy_mock.call_count == 1


def test_export_returns_binary_content_and_sends_supports_all_drives(
    requests_mock, client: GoogleDriveDocClient
) -> None:
    requests_mock.get(f"{BASE}/file-1/export", content=b"%PDF-1.4 fake pdf bytes")

    content = client.export("file-1", mime_type="application/pdf")

    assert content == b"%PDF-1.4 fake pdf bytes"
    assert requests_mock.last_request.qs["mimetype"] == ["application/pdf"]
    assert requests_mock.last_request.qs["supportsalldrives"] == ["true"]


def test_delete_sends_supports_all_drives(requests_mock, client: GoogleDriveDocClient) -> None:
    delete_mock = requests_mock.delete(f"{BASE}/file-1", status_code=204)

    client.delete("file-1")

    assert delete_mock.call_count == 1
    assert requests_mock.last_request.qs["supportsalldrives"] == ["true"]


def test_delete_does_not_raise_when_deletion_fails(
    requests_mock, client: GoogleDriveDocClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """削除失敗時は例外を送出せずログ警告のみに留める。"""
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.delete(f"{BASE}/file-1", status_code=500, json={"error": "boom"})

    client.delete("file-1")  # 例外を送出しないことを確認


def test_copy_as_native_sends_parents_when_specified(requests_mock, client: GoogleDriveDocClient) -> None:
    """見積書承認フロー(2026-08-18)向け: コピー先を指定フォルダ直下に作成する。"""
    requests_mock.post(f"{BASE}/file-1/copy", json={"id": "copy-99"})

    client.copy_as_native(
        "file-1",
        target_mime_type="application/vnd.google-apps.spreadsheet",
        new_name="__tmp_x",
        parents=["folder-1"],
    )

    sent_body = requests_mock.last_request.json()
    assert sent_body == {
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "name": "__tmp_x",
        "parents": ["folder-1"],
    }


def test_move_sends_add_and_remove_parents_and_supports_all_drives(
    requests_mock, client: GoogleDriveDocClient
) -> None:
    move_mock = requests_mock.patch(f"{BASE}/file-1", json={"id": "file-1"})

    client.move("file-1", add_parent="folder-sent", remove_parent="folder-pending")

    assert move_mock.call_count == 1
    assert requests_mock.last_request.qs["addparents"] == ["folder-sent"]
    assert requests_mock.last_request.qs["removeparents"] == ["folder-pending"]
    assert requests_mock.last_request.qs["supportsalldrives"] == ["true"]


def test_start_approval_sends_reviewer_and_message_and_returns_approval_id(
    requests_mock, client: GoogleDriveDocClient
) -> None:
    requests_mock.post(f"{BASE}/file-1/approvals:start", json={"approvalId": "approval-1"})

    approval_id = client.start_approval("file-1", reviewer_email="approver@example.com", message="ご確認お願いします")

    assert approval_id == "approval-1"
    sent_body = requests_mock.last_request.json()
    assert sent_body == {
        "reviewerEmails": ["approver@example.com"],
        "message": "ご確認お願いします",
    }
    assert "supportsalldrives" not in requests_mock.last_request.qs


def test_start_approval_omits_review_instructions_when_message_empty(
    requests_mock, client: GoogleDriveDocClient
) -> None:
    requests_mock.post(f"{BASE}/file-1/approvals:start", json={"approvalId": "approval-1"})

    client.start_approval("file-1", reviewer_email="approver@example.com")

    sent_body = requests_mock.last_request.json()
    assert "message" not in sent_body


def test_start_approval_raises_when_response_has_no_approval_id(
    requests_mock, client: GoogleDriveDocClient
) -> None:
    requests_mock.post(f"{BASE}/file-1/approvals:start", json={})

    with pytest.raises(GoogleDriveApiError):
        client.start_approval("file-1", reviewer_email="approver@example.com")


def test_get_approval_returns_status(requests_mock, client: GoogleDriveDocClient) -> None:
    requests_mock.get(f"{BASE}/file-1/approvals/approval-1", json={"status": "APPROVED"})

    approval = client.get_approval("file-1", "approval-1")

    assert approval == {"status": "APPROVED"}
    # approvals系エンドポイントはsupportsAllDrivesを受け付けない(HTTP 400になる、
    # 2026-08-18実機確認)。
    assert "supportsalldrives" not in requests_mock.last_request.qs


def test_cancel_approval_sends_post_to_cancel_endpoint(
    requests_mock, client: GoogleDriveDocClient
) -> None:
    cancel_mock = requests_mock.post(f"{BASE}/file-1/approvals/approval-1:cancel", json={})

    client.cancel_approval("file-1", "approval-1")

    assert cancel_mock.call_count == 1
    assert "supportsalldrives" not in requests_mock.last_request.qs


def test_cancel_approval_raises_on_error(requests_mock, client: GoogleDriveDocClient) -> None:
    requests_mock.post(f"{BASE}/file-1/approvals/approval-1:cancel", status_code=404, json={"error": "boom"})

    with pytest.raises(GoogleDriveApiError):
        client.cancel_approval("file-1", "approval-1")
