"""案件データから見積書(PDF)を生成する。

見積書NOの既存の採番規則（実データ例: "CN20251001K01", "CN2026071301K", "CN2025081501KY"）は
表記ゆれがあり完全な再現は困難なため、簡略化した独自ルールで新規採番する
（`CN{YYYYMMDD}{Notion案件IDの先頭4文字を大文字化}`）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.document_generation.common import (
    BASELINE_NOTE,
    TEMPLATE_SHEET_TITLE,
    DocumentResult,
    TemplateSheetNotFoundError,
    resolve_template,
)
from src.document_generation.google_drive_client import GoogleDriveDocClient
from src.document_generation.project_data import fetch_project_document_data
from src.document_generation.sheet_filler import (
    HttpSheetsValuesClient,
    LabelSheetsClient,
    fill_cell_containing,
    fill_labeled_cells,
)
from src.document_generation.template_registry import TemplateRegistry

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))
_CATEGORY = "見積書"
_NATIVE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
_PDF_MIME_TYPE = "application/pdf"
_ADDRESSEE_MARKER = "御中"

_SEAL_NOTE = (
    "担当者印影欄はテンプレート上フローティング画像として配置されているため、"
    "自動生成では差し替えできません（テンプレートの雛形のままです。送付前に手動で確認・"
    "差し替えてください）。"
)


def _today_jst() -> date:
    return datetime.now(_JST).date()


def _generate_quote_number(notion_page_id: str, *, today: date | None = None) -> str:
    """見積書NOを新規採番する（既存の採番規則は完全には解明できていないため簡略化した
    独自ルールを採用している）。"""
    resolved_today = today or _today_jst()
    return f"CN{resolved_today.strftime('%Y%m%d')}{notion_page_id[:4].upper()}"


def generate_quote(
    notion_page_id: str,
    *,
    registry: TemplateRegistry | None = None,
    drive_client: GoogleDriveDocClient | None = None,
    sheets_client: LabelSheetsClient | None = None,
    notion_client: Any | None = None,
    client_master_client: Any | None = None,
) -> DocumentResult:
    """案件データを取得し、テンプレートを解決・コピーしてラベル駆動でセルを差し込み、
    PDFとしてexportする。生成完了後、Drive上の一時コピーは削除する。"""
    resolved_registry = registry or TemplateRegistry()
    project_data = fetch_project_document_data(
        notion_page_id, notion_client=notion_client, client_master_client=client_master_client
    )
    template = resolve_template(_CATEGORY, project_data.proposed_services, resolved_registry)

    resolved_drive_client = drive_client or GoogleDriveDocClient()
    resolved_sheets_client = sheets_client or HttpSheetsValuesClient()
    notes: list[str] = [BASELINE_NOTE, _SEAL_NOTE]

    copy_id = resolved_drive_client.copy_as_native(
        template.file_id,
        target_mime_type=_NATIVE_SHEET_MIME_TYPE,
        new_name=f"__tmp_quote_{notion_page_id}",
    )
    try:
        found = resolved_sheets_client.find_sheet(copy_id, exact_title=TEMPLATE_SHEET_TITLE)
        if found is None:
            raise TemplateSheetNotFoundError(
                f"テンプレート「{template.file_name}」に「{TEMPLATE_SHEET_TITLE}」という名前の"
                "空タブが見つかりませんでした。スプレッドシート上に空の雛形タブを作成し、"
                f"タブ名を「{TEMPLATE_SHEET_TITLE}」にしてください。"
            )
        sheet_name, sheet_id = found
        # Drive APIのexportはワークブック全体（＝他の全クライアントの過去案件タブ）を
        # まとめて書き出してしまうため、対象タブ以外を削除してから export する
        # （実データ確認で判明した重大な情報漏洩リスクへの対応）。
        resolved_sheets_client.keep_only_sheet(copy_id, sheet_id=sheet_id)

        values_by_label: dict[str, str] = {
            "見積書NO": _generate_quote_number(notion_page_id),
            "発行日": _today_jst().strftime("%Y/%m/%d"),
        }
        if project_data.project_name:
            values_by_label["件名"] = project_data.project_name
        if project_data.memo:
            values_by_label["注意事項"] = project_data.memo
        if project_data.assignee_name:
            values_by_label["担当"] = project_data.assignee_name

        fill_labeled_cells(resolved_sheets_client, copy_id, sheet_name, values_by_label)

        if project_data.client_name:
            addressee_found = fill_cell_containing(
                resolved_sheets_client,
                copy_id,
                sheet_name,
                _ADDRESSEE_MARKER,
                f"{project_data.client_name}　{_ADDRESSEE_MARKER}",
            )
            if not addressee_found:
                notes.append("宛先セル（「御中」を含むセル）が見つからず、宛先の差し込みは未反映です。")
        else:
            notes.append("取引先名が案件データから取得できなかったため、宛先の差し込みは未反映です。")

        content = resolved_drive_client.export(copy_id, mime_type=_PDF_MIME_TYPE)
    finally:
        resolved_drive_client.delete(copy_id)

    return DocumentResult(
        content=content,
        file_name=f"{project_data.project_name or notion_page_id}_見積書.pdf",
        mime_type=_PDF_MIME_TYPE,
        notes=notes,
    )
