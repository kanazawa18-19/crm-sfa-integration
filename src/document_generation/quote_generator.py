"""案件データから見積書(PDF)を生成する。

見積書NOの既存の採番規則（実データ例: "CN20251001K01", "CN2026071301K", "CN2025081501KY"）は
表記ゆれがあり完全な再現は困難なため、簡略化した独自ルールで新規採番する
（`CN{YYYYMMDD}{Notion案件IDの先頭4文字を大文字化}`）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.document_generation.approval_db import find_in_progress_approval, insert_document_approval
from src.document_generation.approver_db import is_active_document_approver
from src.document_generation.common import (
    BASELINE_NOTE,
    TEMPLATE_SHEET_TITLE,
    DocumentResult,
    TemplateSheetNotFoundError,
    resolve_template,
)
from src.document_generation.drive_connection_db import get_rep_drive_connection
from src.document_generation.google_drive_client import GoogleDriveDocClient
from src.document_generation.project_data import ProjectDocumentData, fetch_project_document_data
from src.document_generation.sheet_filler import (
    HttpSheetsValuesClient,
    LabelSheetsClient,
    fill_cell_containing,
    fill_labeled_cells,
)
from src.document_generation.template_registry import TemplateInfo, TemplateRegistry
from src.gmail_sync.gmail_client import refresh_access_token
from src.gmail_sync.token_crypto import decrypt_token

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))
_CATEGORY = "見積書"
_NATIVE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
_PDF_MIME_TYPE = "application/pdf"
_ADDRESSEE_MARKER = "御中"

# 見積書 承認フロー(2026-08-18)向けのDrive上の固定フォルダID。実名はそれぞれ
# 「営業部」(一時格納)・「送付済」(承認後の移動先)（計画書「Context」参照）。
QUOTE_PENDING_APPROVAL_FOLDER_ID = "1-JEnDVJQPY677vqIObtTLeiFt437jPGa"
QUOTE_SENT_FOLDER_ID = "1HZDKCBD1JLq1g9MEg9alU0azAjPKdSzl"

_SEAL_NOTE = (
    "担当者印影欄はテンプレート上フローティング画像として配置されているため、"
    "自動生成では差し替えできません（テンプレートの雛形のままです。送付前に手動で確認・"
    "差し替えてください）。"
)


class DriveNotConnectedError(Exception):
    """承認リクエストの送信元担当者(`requested_by_email`)が自分のDrive OAuth接続
    (`RepDriveConnection`)を未接続の場合に送出する(2026-08-18)。呼び出し元(API層)は
    422等へ変換し、フロント側で`/settings/drive`への導線を出す想定。"""


class InvalidApproverEmailError(Exception):
    """`approver_email`が`DocumentApprover`テーブルに`active=true`で登録されていない場合に
    送出する(2026-08-18)。フロントのセレクトボックスはUI制約に過ぎずAPIを直接叩けば
    任意のメールアドレスを送信できてしまうため、サーバー側で必ず検証する
    (shirokuma-secレビューBLOCKER対応)。"""


class DuplicateApprovalRequestError(Exception):
    """同じ案件(`notion_page_id`)・カテゴリで既に`in_progress`の承認リクエストが存在する場合に
    送出する(2026-08-18、承認リクエストの重複送信防止)。"""


def _today_jst() -> date:
    return datetime.now(_JST).date()


def _generate_quote_number(notion_page_id: str, *, today: date | None = None) -> str:
    """見積書NOを新規採番する（既存の採番規則は完全には解明できていないため簡略化した
    独自ルールを採用している）。"""
    resolved_today = today or _today_jst()
    return f"CN{resolved_today.strftime('%Y%m%d')}{notion_page_id[:4].upper()}"


def _build_quote_copy(
    notion_page_id: str,
    project_data: ProjectDocumentData,
    template: TemplateInfo,
    *,
    drive_client: GoogleDriveDocClient,
    sheets_client: LabelSheetsClient,
    new_name: str,
    parents: list[str] | None = None,
) -> tuple[str, list[str]]:
    """テンプレートをコピーし、ラベル駆動でセルを差し込むところまでを行う共通処理
    （`generate_quote`のその場ダウンロード用途・`request_quote_approval`の承認リクエスト用途、
    双方から呼ばれる。2026-08-18、承認フロー追加に伴い`generate_quote`本体から抽出）。

    コピー先file_idと生成メモ(`notes`)を返す。コピー作成後に失敗した場合
    （雛形タブ未検出等）は、ここで一時コピーを削除してから例外を再送出する
    （呼び出し元ごとに同じ後片付けを重複実装しないため）。`new_name`は呼び出し元ごとに
    異なるコピー名を指定する（`generate_quote`は使い捨ての内部名、`request_quote_approval`は
    承認フローの成果物として永久保存されるため案件名ベースの分かりやすい名前を渡す——
    共用にすると承認フローの保存物が`__tmp_quote_{id}`のままDrive上に残ってしまうため、
    shirokuma-secレビューBLOCKER対応で引数化した）。`parents`を指定すると、コピーを
    テンプレートと同じ場所ではなく指定フォルダ直下に作成する
    （承認リクエストフローでは一時格納フォルダへ直接作成する）。
    """
    notes: list[str] = [BASELINE_NOTE, _SEAL_NOTE]
    copy_id = drive_client.copy_as_native(
        template.file_id,
        target_mime_type=_NATIVE_SHEET_MIME_TYPE,
        new_name=new_name,
        parents=parents,
    )
    try:
        found = sheets_client.find_sheet(copy_id, exact_title=TEMPLATE_SHEET_TITLE)
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
        sheets_client.keep_only_sheet(copy_id, sheet_id=sheet_id)

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

        fill_labeled_cells(sheets_client, copy_id, sheet_name, values_by_label)

        if project_data.client_name:
            addressee_found = fill_cell_containing(
                sheets_client,
                copy_id,
                sheet_name,
                _ADDRESSEE_MARKER,
                f"{project_data.client_name}　{_ADDRESSEE_MARKER}",
            )
            if not addressee_found:
                notes.append("宛先セル（「御中」を含むセル）が見つからず、宛先の差し込みは未反映です。")
        else:
            notes.append("取引先名が案件データから取得できなかったため、宛先の差し込みは未反映です。")
    except Exception:
        drive_client.delete(copy_id)
        raise
    return copy_id, notes


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

    copy_id, notes = _build_quote_copy(
        notion_page_id,
        project_data,
        template,
        drive_client=resolved_drive_client,
        sheets_client=resolved_sheets_client,
        new_name=f"__tmp_quote_{notion_page_id}",
    )
    try:
        content = resolved_drive_client.export(copy_id, mime_type=_PDF_MIME_TYPE)
    finally:
        resolved_drive_client.delete(copy_id)

    return DocumentResult(
        content=content,
        file_name=f"{project_data.project_name or notion_page_id}_見積書.pdf",
        mime_type=_PDF_MIME_TYPE,
        notes=notes,
    )


@dataclass(frozen=True)
class QuoteApprovalResult:
    drive_file_id: str
    drive_approval_id: str
    document_approval_id: str


def request_quote_approval(
    notion_page_id: str,
    *,
    approver_email: str,
    requested_by_email: str,
    message: str = "",
    registry: TemplateRegistry | None = None,
    notion_client: Any | None = None,
    client_master_client: Any | None = None,
) -> QuoteApprovalResult:
    """見積書を生成して一時格納フォルダ(`QUOTE_PENDING_APPROVAL_FOLDER_ID`)へ保存し、
    Drive純正の「承認をリクエスト」機能で`approver_email`宛に承認リクエストを送信する
    (2026-08-18)。

    `approver_email`はクライアント(フロントのセレクトボックス)から渡された値をそのまま
    信頼せず、`DocumentApprover`テーブルに`active=true`で登録済みかをここで検証する
    （未登録なら`InvalidApproverEmailError`。UI制約に過ぎないセレクトボックスを介さず
    APIを直接叩けば任意のメールアドレス——社外含む——へ本物のDrive承認リクエストを送信
    できてしまう問題への対処、shirokuma-secレビューBLOCKER対応）。

    同じ案件・カテゴリで既に`in_progress`の承認リクエストがあれば
    `DuplicateApprovalRequestError`を送出する（重複送信防止）。

    サービスアカウントでは`canStartApproval`が`false`であることを実機検証済みのため
    （計画書「認証方式の紆余曲折」参照）、`requested_by_email`本人が事前に
    `/settings/drive`から接続したDrive OAuth（`RepDriveConnection`、`RepGmailConnection`と
    同じ個人OAuth同意方式）のアクセストークンを使う。未接続の場合は`DriveNotConnectedError`
    を送出する。

    生成したコピーはセル差し込み後にPDFへ変換し(`replace_content()`で同じfile_idのまま中身を
    置き換える、2026-08-19)、`generate_quote`と異なりdeleteはしない——コピー自体(PDF化後)が
    承認対象の成果物であり、一時格納フォルダに残したまま`DocumentApproval`行を作成して返す
    （状態確定後の移動は`approval_poll`が行う）。

    `start_approval()`失敗時は孤立したコピーをDriveから削除する。`start_approval()`成功後に
    DB書き込み(`insert_document_approval()`)が失敗した場合は、Drive上に送信済みの承認
    リクエストが記録なしで残り続けないよう`cancel_approval()`で取り消す（いずれも
    shirokuma-secレビューBLOCKER対応）。
    """
    if not is_active_document_approver(approver_email):
        raise InvalidApproverEmailError(
            f"{approver_email}は承認者として登録されていません。承認者一覧に登録済みの"
            "メールアドレスを指定してください。"
        )
    if find_in_progress_approval(notion_page_id, _CATEGORY) is not None:
        raise DuplicateApprovalRequestError(
            f"この案件（{notion_page_id}）の見積書は既に承認リクエストが進行中です。"
        )

    connection = get_rep_drive_connection(requested_by_email)
    if connection is None:
        raise DriveNotConnectedError(
            f"{requested_by_email}のDrive連携が未接続です。設定画面（/settings/drive）から"
            "Drive連携を行ってください。"
        )
    access_token = refresh_access_token(decrypt_token(connection.refresh_token_enc))

    resolved_registry = registry or TemplateRegistry()
    project_data = fetch_project_document_data(
        notion_page_id, notion_client=notion_client, client_master_client=client_master_client
    )
    template = resolve_template(_CATEGORY, project_data.proposed_services, resolved_registry)

    drive_client = GoogleDriveDocClient(access_token=access_token)
    sheets_client = HttpSheetsValuesClient(access_token=access_token)

    quote_name = f"{project_data.project_name or notion_page_id}_見積書"
    copy_id, _notes = _build_quote_copy(
        notion_page_id,
        project_data,
        template,
        drive_client=drive_client,
        sheets_client=sheets_client,
        new_name=quote_name,
        parents=[QUOTE_PENDING_APPROVAL_FOLDER_ID],
    )

    try:
        # セル差し込みまで終えたSheetsコピーをPDFへ変換する(2026-08-19)。承認リクエストは
        # 編集可能なSheets形式ではなくPDFで送るのが既存の運用実態だったため(過去の承認
        # メール履歴で確認)、同じfile_idのまま中身をPDFへ置き換えてから承認をリクエストする。
        pdf_bytes = drive_client.export(copy_id, mime_type=_PDF_MIME_TYPE)
        drive_client.replace_content(copy_id, content=pdf_bytes, mime_type=_PDF_MIME_TYPE)
        drive_client.rename(copy_id, name=f"{quote_name}.pdf")
    except Exception:
        logger.exception(
            "failed to convert quote copy to PDF for notion_page_id=%r; deleting orphaned "
            "Drive copy file_id=%r",
            notion_page_id,
            copy_id,
        )
        drive_client.delete(copy_id)
        raise

    try:
        drive_approval_id = drive_client.start_approval(
            copy_id, reviewer_email=approver_email, message=message
        )
    except Exception:
        logger.exception(
            "start_approval failed for notion_page_id=%r; deleting orphaned Drive copy "
            "file_id=%r",
            notion_page_id,
            copy_id,
        )
        drive_client.delete(copy_id)
        raise

    try:
        document_approval_id = insert_document_approval(
            notion_project_id=notion_page_id,
            category=_CATEGORY,
            drive_file_id=copy_id,
            drive_approval_id=drive_approval_id,
            approver_email=approver_email,
            requested_by_email=requested_by_email,
        )
    except Exception:
        logger.exception(
            "insert_document_approval failed after start_approval already succeeded "
            "(notion_page_id=%r, drive_file_id=%r, drive_approval_id=%r); attempting to "
            "cancel the Drive approval request so it does not stay in_progress with no "
            "DocumentApproval record",
            notion_page_id,
            copy_id,
            drive_approval_id,
        )
        try:
            drive_client.cancel_approval(copy_id, drive_approval_id)
        except Exception:
            logger.exception(
                "cancel_approval also failed for file_id=%r approval_id=%r; manual cleanup "
                "in Drive may be required",
                copy_id,
                drive_approval_id,
            )
        raise

    return QuoteApprovalResult(
        drive_file_id=copy_id,
        drive_approval_id=drive_approval_id,
        document_approval_id=document_approval_id,
    )
