"""見積書承認リクエスト(DocumentApproval)の状態確定ポーリング(2026-08-18)。

Drive Approvalsはpush通知を持たないため、`status="in_progress"`の行を定期的に
`GoogleDriveDocClient.get_approval()`でポーリングし、状態が確定した(APPROVED/DECLINED/
CANCELLED)ものについてフォルダ移動・DB更新・Notion「見積書」プロパティ更新・Slack通知を行う
(`GET /api/cron/document-approval-poll`から呼ばれる、`src/api/app.py`参照)。
"""

from __future__ import annotations

import logging
from typing import Any

from src.api.notion_display import page_to_display_dict
from src.db_schema.project import PROJECT_SCHEMA
from src.document_generation.approval_db import (
    APPROVED,
    CANCELLED,
    DECLINED,
    DocumentApproval,
    list_in_progress_approvals,
    update_approval_status,
)
from src.document_generation.approval_notify import notify_quote_approval_result
from src.document_generation.drive_connection_db import get_rep_drive_connection
from src.document_generation.google_drive_client import GoogleDriveDocClient
from src.document_generation.quote_generator import QUOTE_PENDING_APPROVAL_FOLDER_ID, QUOTE_SENT_FOLDER_ID
from src.gmail_sync.gmail_client import refresh_access_token
from src.gmail_sync.token_crypto import decrypt_token
from src.sync_engine.clients.notion_client import HttpNotionClient

logger = logging.getLogger(__name__)

_DRIVE_STATE_TO_STATUS = {"APPROVED": APPROVED, "DECLINED": DECLINED, "CANCELLED": CANCELLED}

PROP_案件名 = "案件名"
PROP_見積書 = "見積書"


def _drive_file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def _project_name(raw_page: dict[str, Any], *, fallback: str) -> str:
    # ドキュメント生成側(project_data.fetch_project_document_data)は取引先マスターも
    # 合わせて取得するが、ここでは案件名のみあれば十分なため、raw_pageから直接
    # page_to_display_dict()で取り出す(取引先マスターDBへの追加API呼び出しを避ける)。
    record, _skipped = page_to_display_dict(raw_page, PROJECT_SCHEMA)
    return record.get(PROP_案件名) or fallback


def _append_quote_file_link(
    notion_client: Any, notion_page_id: str, raw_page: dict[str, Any], *, name: str, url: str
) -> None:
    """Notion案件の「見積書」(FILESプロパティ)へファイルリンクを追記する(既存分は残す)。

    `HttpNotionClient.get_page()`はFILES型プロパティを読み飛ばす仕様
    (`notion_client.PARSEABLE_NOTION_PROPERTY_TYPES`未対応)のため、`get_raw_page()`の
    生JSONから既存の外部リンク登録分を手動で読み出してから追記する。

    同一URLが既に含まれている場合は追記しない(obasan-qualityレビューWARN対応: `move()`成功後・
    `update_approval_status()`前に例外が起きて`poll_document_approvals`が再実行された場合、
    重複追記を防ぐため)。
    """
    existing_prop = (raw_page.get("properties") or {}).get(PROP_見積書) or {}
    existing_files = existing_prop.get("files") or []
    existing_entries = [
        {"name": f.get("name"), "url": (f.get("external") or {}).get("url")}
        for f in existing_files
        if f.get("type") == "external" and (f.get("external") or {}).get("url")
    ]
    if any(entry["url"] == url for entry in existing_entries):
        return
    notion_client.update_page(notion_page_id, {PROP_見積書: existing_entries + [{"name": name, "url": url}]})


def _resolve_one(approval: DocumentApproval, *, notion_client: Any) -> str | None:
    """1件の承認リクエストをポーリングし、状態が確定していれば反映する。反映した場合は
    新しいstatus文字列を、まだ確定していない/スキップした場合はNoneを返す。"""
    connection = get_rep_drive_connection(approval.requested_by_email)
    if connection is None:
        logger.warning(
            "document approval id=%r: requested_by_email=%r has no RepDriveConnection "
            "(Drive連携が解除された可能性); skipping",
            approval.id,
            approval.requested_by_email,
        )
        return None

    access_token = refresh_access_token(decrypt_token(connection.refresh_token_enc))
    drive_client = GoogleDriveDocClient(access_token=access_token)

    approval_state = drive_client.get_approval(approval.drive_file_id, approval.drive_approval_id)
    new_status = _DRIVE_STATE_TO_STATUS.get(approval_state.get("status"))
    if new_status is None:
        return None  # まだ進行中、またはDrive側の未知のstatus値

    raw_page = notion_client.get_raw_page(approval.notion_project_id)
    project_name = _project_name(raw_page, fallback=approval.notion_project_id)

    if new_status == APPROVED:
        drive_client.move(
            approval.drive_file_id,
            add_parent=QUOTE_SENT_FOLDER_ID,
            remove_parent=QUOTE_PENDING_APPROVAL_FOLDER_ID,
        )
        _append_quote_file_link(
            notion_client,
            approval.notion_project_id,
            raw_page,
            name=f"{project_name}_見積書",
            url=_drive_file_url(approval.drive_file_id),
        )
    # DECLINED/CANCELLED: 一時格納フォルダにファイルを残したまま、依頼者へ通知のみ行う
    # (計画書「Context」4.参照)。

    update_approval_status(approval.id, new_status)
    notify_quote_approval_result(
        requested_by_email=approval.requested_by_email,
        project_name=project_name,
        approver_emails=approval.approver_emails,
        status=new_status,
        # 却下者特定(未検証のフォールバック付き、approval_notify._extract_declined_reviewers
        # 参照)のため、既に取得済みのget_approval()の生レスポンスをそのまま渡す。
        approval_state=approval_state,
    )
    return new_status


def poll_document_approvals(*, notion_client: Any | None = None) -> dict[str, Any]:
    """`GET /api/cron/document-approval-poll`のエントリポイント本体。

    1件の失敗が他の承認リクエストの処理を止めないよう、個別にtry/exceptで独立させる
    (`src/email_reminders/reminder_check.py`の対象ごと独立処理と同じ方針)。
    """
    resolved_notion_client = notion_client or HttpNotionClient(
        PROJECT_SCHEMA.key, PROJECT_SCHEMA.notion_database_id
    )
    approvals = list_in_progress_approvals()
    resolved = 0
    errors = 0
    for approval in approvals:
        try:
            new_status = _resolve_one(approval, notion_client=resolved_notion_client)
            if new_status is not None:
                resolved += 1
        except Exception:
            logger.exception("document approval poll failed for id=%r", approval.id)
            errors += 1

    return {"checked": len(approvals), "resolved": resolved, "errors": errors}
