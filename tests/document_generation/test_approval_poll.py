"""見積書承認リクエストの状態確定ポーリング(`approval_poll.poll_document_approvals`)の単体テスト。

Google Drive API/Postgres/Slackへは一切アクセスしない(すべてmonkeypatch/フェイクで差し替える)。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.document_generation.approval_db import APPROVED, CANCELLED, DECLINED, IN_PROGRESS, DocumentApproval
from src.document_generation.approval_poll import poll_document_approvals
from src.document_generation.drive_connection_db import RepDriveConnection
from src.document_generation.quote_generator import QUOTE_PENDING_APPROVAL_FOLDER_ID, QUOTE_SENT_FOLDER_ID
from tests.document_generation._fakes import FakeGoogleDriveDocClient, FakeProjectNotionClient, build_raw_project_page

PAGE_ID = "abcd1234-0000-0000-0000-000000000000"


def _make_approval(
    *, approval_id: str = "row-1", status: str = IN_PROGRESS, drive_file_id: str = "file-1"
) -> DocumentApproval:
    return DocumentApproval(
        id=approval_id,
        notion_project_id=PAGE_ID,
        category="見積書",
        drive_file_id=drive_file_id,
        drive_approval_id="drive-approval-1",
        approver_emails=["approver@example.com"],
        requested_by_email="rep@example.com",
        status=status,
        created_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
        resolved_at=None,
    )


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    approvals: list[DocumentApproval],
    connection: RepDriveConnection | None,
    drive_client: FakeGoogleDriveDocClient,
) -> tuple[list[tuple[str, str]], list[dict[str, object]]]:
    monkeypatch.setattr(
        "src.document_generation.approval_poll.list_in_progress_approvals", lambda: approvals
    )
    monkeypatch.setattr(
        "src.document_generation.approval_poll.get_rep_drive_connection", lambda rep_email: connection
    )
    monkeypatch.setattr(
        "src.document_generation.approval_poll.decrypt_token", lambda enc: f"decrypted:{enc}"
    )
    monkeypatch.setattr(
        "src.document_generation.approval_poll.refresh_access_token",
        lambda refresh_token: "access-token",
    )
    monkeypatch.setattr(
        "src.document_generation.approval_poll.GoogleDriveDocClient", lambda **kwargs: drive_client
    )

    status_updates: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.document_generation.approval_poll.update_approval_status",
        lambda approval_id, status: status_updates.append((approval_id, status)),
    )

    notify_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "src.document_generation.approval_poll.notify_quote_approval_result",
        lambda **kwargs: notify_calls.append(kwargs),
    )
    return status_updates, notify_calls


def test_poll_moves_file_and_appends_notion_file_link_when_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = RepDriveConnection(
        rep_email="rep@example.com", refresh_token_enc="enc", connected_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    drive_client = FakeGoogleDriveDocClient(approval_state={"status": "APPROVED"})
    approval = _make_approval(drive_file_id="file-1")
    status_updates, notify_calls = _patch_common(
        monkeypatch, approvals=[approval], connection=connection, drive_client=drive_client
    )

    raw_page = build_raw_project_page(page_id=PAGE_ID, project_name="テスト案件")
    notion_client = FakeProjectNotionClient(raw_page)

    result = poll_document_approvals(notion_client=notion_client)

    assert result == {"checked": 1, "resolved": 1, "errors": 0}
    assert drive_client.move_calls == [
        {"file_id": "file-1", "add_parent": QUOTE_SENT_FOLDER_ID, "remove_parent": QUOTE_PENDING_APPROVAL_FOLDER_ID}
    ]
    assert status_updates == [("row-1", APPROVED)]
    assert notify_calls == [
        {
            "requested_by_email": "rep@example.com",
            "project_name": "テスト案件",
            "approver_emails": ["approver@example.com"],
            "status": APPROVED,
            "approval_state": {"status": "APPROVED"},
        }
    ]
    assert len(notion_client.update_calls) == 1
    updated_files = notion_client.update_calls[0]["properties"]["見積書"]
    assert updated_files == [{"name": "テスト案件_見積書", "url": "https://drive.google.com/file/d/file-1/view"}]


def test_poll_preserves_existing_notion_file_links_when_appending(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = RepDriveConnection(
        rep_email="rep@example.com", refresh_token_enc="enc", connected_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    drive_client = FakeGoogleDriveDocClient(approval_state={"status": "APPROVED"})
    approval = _make_approval(drive_file_id="file-2")
    _patch_common(monkeypatch, approvals=[approval], connection=connection, drive_client=drive_client)

    raw_page = build_raw_project_page(page_id=PAGE_ID, project_name="テスト案件")
    raw_page["properties"]["見積書"] = {
        "type": "files",
        "files": [
            {"type": "external", "name": "既存見積書", "external": {"url": "https://drive.google.com/file/d/old/view"}}
        ],
    }
    notion_client = FakeProjectNotionClient(raw_page)

    poll_document_approvals(notion_client=notion_client)

    updated_files = notion_client.update_calls[0]["properties"]["見積書"]
    assert updated_files == [
        {"name": "既存見積書", "url": "https://drive.google.com/file/d/old/view"},
        {"name": "テスト案件_見積書", "url": "https://drive.google.com/file/d/file-2/view"},
    ]


def test_poll_skips_appending_notion_file_link_when_same_url_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """move()成功後・DB更新前に例外が起きて再実行された場合の重複追記防止
    (obasan-qualityレビューWARN対応)。"""
    connection = RepDriveConnection(
        rep_email="rep@example.com", refresh_token_enc="enc", connected_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    drive_client = FakeGoogleDriveDocClient(approval_state={"status": "APPROVED"})
    approval = _make_approval(drive_file_id="file-1")
    _patch_common(monkeypatch, approvals=[approval], connection=connection, drive_client=drive_client)

    raw_page = build_raw_project_page(page_id=PAGE_ID, project_name="テスト案件")
    raw_page["properties"]["見積書"] = {
        "type": "files",
        "files": [
            {
                "type": "external",
                "name": "テスト案件_見積書",
                "external": {"url": "https://drive.google.com/file/d/file-1/view"},
            }
        ],
    }
    notion_client = FakeProjectNotionClient(raw_page)

    poll_document_approvals(notion_client=notion_client)

    assert notion_client.update_calls == []


def test_poll_does_not_move_or_update_notion_when_declined(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = RepDriveConnection(
        rep_email="rep@example.com", refresh_token_enc="enc", connected_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    drive_client = FakeGoogleDriveDocClient(approval_state={"status": "DECLINED"})
    approval = _make_approval(drive_file_id="file-1")
    status_updates, notify_calls = _patch_common(
        monkeypatch, approvals=[approval], connection=connection, drive_client=drive_client
    )
    notion_client = FakeProjectNotionClient(build_raw_project_page(page_id=PAGE_ID, project_name="テスト案件"))

    result = poll_document_approvals(notion_client=notion_client)

    assert result == {"checked": 1, "resolved": 1, "errors": 0}
    assert drive_client.move_calls == []
    assert notion_client.update_calls == []
    assert status_updates == [("row-1", DECLINED)]
    assert notify_calls[0]["status"] == DECLINED
    # 却下者特定(未検証のフォールバック付き、approval_notify._extract_declined_reviewers参照)の
    # ためget_approval()の生レスポンスをそのままnotify_quote_approval_result()へ渡す。
    assert notify_calls[0]["approval_state"] == {"status": "DECLINED"}


def test_poll_does_not_move_or_update_notion_when_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = RepDriveConnection(
        rep_email="rep@example.com", refresh_token_enc="enc", connected_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    drive_client = FakeGoogleDriveDocClient(approval_state={"status": "CANCELLED"})
    approval = _make_approval(drive_file_id="file-1")
    status_updates, notify_calls = _patch_common(
        monkeypatch, approvals=[approval], connection=connection, drive_client=drive_client
    )
    notion_client = FakeProjectNotionClient(build_raw_project_page(page_id=PAGE_ID, project_name="テスト案件"))

    result = poll_document_approvals(notion_client=notion_client)

    assert result == {"checked": 1, "resolved": 1, "errors": 0}
    assert drive_client.move_calls == []
    assert status_updates == [("row-1", CANCELLED)]


def test_poll_leaves_in_progress_untouched_when_still_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = RepDriveConnection(
        rep_email="rep@example.com", refresh_token_enc="enc", connected_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    # Driveのstateがまだ確定していない(承認待ち)場合の応答。
    drive_client = FakeGoogleDriveDocClient(approval_state={"status": "NEEDS_ACTION"})
    approval = _make_approval()
    status_updates, notify_calls = _patch_common(
        monkeypatch, approvals=[approval], connection=connection, drive_client=drive_client
    )
    notion_client = FakeProjectNotionClient(build_raw_project_page(page_id=PAGE_ID))

    result = poll_document_approvals(notion_client=notion_client)

    assert result == {"checked": 1, "resolved": 0, "errors": 0}
    assert status_updates == []
    assert notify_calls == []


def test_poll_skips_when_requested_by_has_no_drive_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """依頼者がDrive連携を解除済み等でRepDriveConnectionが見つからない場合、例外にせず
    スキップする(cron全体は成功として終える)。"""
    drive_client = FakeGoogleDriveDocClient()
    approval = _make_approval()
    status_updates, notify_calls = _patch_common(
        monkeypatch, approvals=[approval], connection=None, drive_client=drive_client
    )
    notion_client = FakeProjectNotionClient(build_raw_project_page(page_id=PAGE_ID))

    result = poll_document_approvals(notion_client=notion_client)

    assert result == {"checked": 1, "resolved": 0, "errors": 0}
    assert drive_client.get_approval_calls == []
    assert status_updates == []
    assert notify_calls == []


def test_poll_counts_error_and_continues_when_one_approval_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """1件の処理失敗が他の承認リクエストの処理を止めないこと。"""
    connection = RepDriveConnection(
        rep_email="rep@example.com", refresh_token_enc="enc", connected_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    failing_approval = _make_approval(approval_id="row-fail", drive_file_id="file-fail")
    ok_approval = _make_approval(approval_id="row-ok", drive_file_id="file-ok")

    class _RaisingOnFirstCallDriveClient(FakeGoogleDriveDocClient):
        def get_approval(self, file_id: str, approval_id: str) -> dict[str, object]:
            if file_id == "file-fail":
                raise RuntimeError("drive api boom")
            return super().get_approval(file_id, approval_id)

    drive_client = _RaisingOnFirstCallDriveClient(approval_state={"status": "APPROVED"})
    status_updates, notify_calls = _patch_common(
        monkeypatch,
        approvals=[failing_approval, ok_approval],
        connection=connection,
        drive_client=drive_client,
    )
    notion_client = FakeProjectNotionClient(build_raw_project_page(page_id=PAGE_ID, project_name="テスト案件"))

    result = poll_document_approvals(notion_client=notion_client)

    assert result == {"checked": 2, "resolved": 1, "errors": 1}
    assert status_updates == [("row-ok", APPROVED)]
