"""案件データから見積書(PDF)を生成する。

見積書NOは正式な採番ルール（`CN{YYYYMMDD}{作成者頭文字1字}{当日発行連番2桁}`、例:
"CN20260819K01"、2026-08-19に金沢さんから共有）で採番する。当日発行連番は
`quote_number_db.next_sequence_for_date`が日付ごとに原子的に払い出す。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.document_generation.approval_db import (
    find_in_progress_approval,
    insert_document_approval,
    release_approval_lock,
    try_acquire_approval_lock,
)
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
from src.document_generation.quote_number_db import next_sequence_for_date
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
    """`approver_emails`が空、または`DocumentApprover`テーブルに`active=true`で登録されて
    いないメールアドレスを1件でも含む場合に送出する(2026-08-18、2026-08-27に複数承認者
    対応)。フロントのチェックボックスはUI制約に過ぎずAPIを直接叩けば任意のメールアドレスを
    送信できてしまうため、サーバー側で必ず全件検証する(shirokuma-secレビューBLOCKER対応)。"""


class DuplicateApprovalRequestError(Exception):
    """同じ案件(`notion_page_id`)・カテゴリで既に`in_progress`の承認リクエストが存在する場合に
    送出する(2026-08-18、承認リクエストの重複送信防止)。"""


def _today_jst() -> date:
    return datetime.now(_JST).date()


def _generate_quote_number(*, creator_name: str | None, today: date | None = None) -> str:
    """見積書NOを正式ルールで採番する: `CN{YYYYMMDD}{作成者頭文字1字}{当日発行連番2桁}`
    （例: "CN20260819K01"）。

    「当日発行連番」は`quote_number_db.next_sequence_for_date`が日付ごとに1から原子的に
    払い出す（同時に複数の見積書が生成されても重複しない）。「作成者頭文字」は`creator_name`
    の先頭1文字を大文字化したもの。`creator_name`が未指定・空文字の場合は"X"で埋める
    （担当者名がNotion側にも手動入力欄にも無い異常系での採番失敗を避けるため）。
    """
    resolved_today = today or _today_jst()
    date_prefix = resolved_today.strftime("%Y%m%d")
    initial = (creator_name or "").strip()[:1].upper() or "X"
    seq = next_sequence_for_date(date_prefix)
    # 連番は2桁を想定しているが、同日100件目以降は`f"{100:02d}"`が"100"になるだけで
    # クラッシュはしない（3桁になり仕様の「2桁」からは逸脱するが、現実的な発行件数では
    # 起こらない想定。shirokuma-secレビューINFO対応のコメント）。
    return f"CN{date_prefix}{initial}{seq:02d}"


@dataclass(frozen=True)
class QuoteOverrides:
    """書類作成画面の手動入力欄(2026-08-19、金沢さんの依頼で追加)。

    Notion案件データから自動取得される値（件名・取引先名・メモ・担当者名）を人手で
    上書きしたい場合や、Notion側に元々存在しない項目（初期費用・月額費用・商材名）を
    見積書へ差し込みたい場合に使う。全項目任意。空文字列はNone同様「未入力（上書きしない）」
    として扱う（`_resolve`参照）。
    """

    memo: str | None = None
    client_name: str | None = None
    service_name: str | None = None
    initial_fee: str | None = None
    monthly_fee: str | None = None
    creator_name: str | None = None


def _resolve(override: str | None, fallback: str | None) -> str | None:
    """空文字列・Noneは「未入力」として扱い、`fallback`（Notion案件データ由来の値）を採用する。"""
    if override is not None and override.strip():
        return override.strip()
    return fallback


_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")


def _sanitize_sheet_cell_value(value: str) -> str:
    """Google Sheets APIへの書き込みは`valueInputOption=USER_ENTERED`（人間が入力したのと
    同じ扱い）のため、`=`/`+`/`-`/`@`で始まる文字列はテキストではなく数式として評価されて
    しまう（formula injection）。手動入力欄はブラウザからの自由入力を経由するため、
    該当する場合は先頭に`'`を付けてテキストとして強制する
    （shirokuma-secレビューWARN対応: 初期費用・月額費用・商材名等の新設フリーテキスト欄が
    数式インジェクションの新しい経路になっていた）。"""
    if value and value[0] in _FORMULA_TRIGGER_CHARS:
        return f"'{value}"
    return value


def _build_quote_copy(
    notion_page_id: str,
    project_data: ProjectDocumentData,
    template: TemplateInfo,
    *,
    drive_client: GoogleDriveDocClient,
    sheets_client: LabelSheetsClient,
    new_name: str,
    parents: list[str] | None = None,
    overrides: QuoteOverrides | None = None,
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

        resolved_today = _today_jst()
        resolved_memo = _resolve(overrides.memo if overrides else None, project_data.memo)
        resolved_client_name = _resolve(
            overrides.client_name if overrides else None, project_data.client_name
        )
        resolved_creator_name = _resolve(
            overrides.creator_name if overrides else None, project_data.assignee_name
        )
        resolved_service_name = _resolve(overrides.service_name if overrides else None, None)
        resolved_initial_fee = _resolve(overrides.initial_fee if overrides else None, None)
        resolved_monthly_fee = _resolve(overrides.monthly_fee if overrides else None, None)

        if not resolved_creator_name:
            # 作成者頭文字が特定できず"X"で採番される（Notion案件データの担当メンバー未設定・
            # 手動入力欄も空の場合）。理由が分からないまま送付されないよう、送付前確認欄に
            # 明示する（obasan-qualityレビューWARN対応）。
            notes.append(
                "作成者が特定できなかったため、見積書NOの先頭文字は仮の「X」で採番されました。"
                "正しい担当者名を手動入力欄の「作成者」に入力し、再生成することを推奨します。"
            )

        values_by_label: dict[str, str] = {
            "見積書NO": _generate_quote_number(creator_name=resolved_creator_name, today=resolved_today),
            "発行日": resolved_today.strftime("%Y/%m/%d"),
        }
        if project_data.project_name:
            values_by_label["件名"] = project_data.project_name
        if resolved_memo:
            values_by_label["注意事項"] = resolved_memo
        if resolved_creator_name:
            values_by_label["担当"] = resolved_creator_name
        # 商材名・初期費用・月額費用はNotion案件データ側に対応項目が無いため、手動入力欄
        # (`overrides`)からのみ差し込む。`fill_labeled_cells`はラベルがテンプレートに
        # 存在しない場合でもエラーにせず警告ログのみ出すため、対応ラベルの無いテンプレートに
        # 差し込もうとしても安全（sheet_filler.fill_labeled_cells参照）。
        if resolved_service_name:
            values_by_label["商材名"] = resolved_service_name
        if resolved_initial_fee:
            values_by_label["初期費用"] = resolved_initial_fee
        if resolved_monthly_fee:
            values_by_label["月額費用"] = resolved_monthly_fee

        values_by_label = {
            label: _sanitize_sheet_cell_value(value) for label, value in values_by_label.items()
        }
        fill_labeled_cells(sheets_client, copy_id, sheet_name, values_by_label)

        if resolved_client_name:
            addressee_found = fill_cell_containing(
                sheets_client,
                copy_id,
                sheet_name,
                _ADDRESSEE_MARKER,
                _sanitize_sheet_cell_value(f"{resolved_client_name}　{_ADDRESSEE_MARKER}"),
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
    overrides: QuoteOverrides | None = None,
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
        overrides=overrides,
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
    approver_emails: list[str],
    requested_by_email: str,
    message: str = "",
    registry: TemplateRegistry | None = None,
    notion_client: Any | None = None,
    client_master_client: Any | None = None,
    overrides: QuoteOverrides | None = None,
) -> QuoteApprovalResult:
    """見積書を生成して一時格納フォルダ(`QUOTE_PENDING_APPROVAL_FOLDER_ID`)へ保存し、
    Drive純正の「承認をリクエスト」機能で`approver_emails`宛に承認リクエストを送信する
    (2026-08-18、2026-08-27に複数承認者対応)。1回の承認リクエスト＝Drive上の1つの
    approvalであり、複数の`reviewerEmails`を持たせる形にする(承認者ごとに別々のapprovalを
    作るわけではない)。Drive側は全員が承認して初めて`APPROVED`になり、1人でも却下すれば
    全体が`DECLINED`になる（docs/quote_approval_note.md参照）。

    `approver_emails`はクライアント(フロントのチェックボックス)から渡された値をそのまま
    信頼せず、空でないこと・順序を保ったまま重複除去したうえで全件が`DocumentApprover`
    テーブルに`active=true`で登録済みかをここで検証する
    （1件でも未登録なら`InvalidApproverEmailError`。UI制約に過ぎないチェックボックスを介さず
    APIを直接叩けば任意のメールアドレス——社外含む——へ本物のDrive承認リクエストを送信
    できてしまう問題への対処、shirokuma-secレビューBLOCKER対応）。

    同じ案件・カテゴリで既に`in_progress`の承認リクエストがあれば
    `DuplicateApprovalRequestError`を送出する（重複送信防止）。

    この重複チェックから`insert_document_approval()`までの区間は、`(notion_page_id,
    category)`をキーにしたPostgresアドバイザリロック（`approval_db.try_acquire_approval_lock()`
    /`release_approval_lock()`、`src/project_mirror/db.py`の多重実行防止ロックと同じ作法）で
    直列化している（外部モデルレビュー(Gemini)で指摘されたTOCTOU対策、2026-08-28）。
    `find_in_progress_approval()`による事前チェックだけでは、チェック後・INSERT前の間に
    ボタン連打や別ウィンドウからのほぼ同時送信が割り込むと両方がチェックを通過してしまい、
    Drive上に本物の承認リクエストが2件送信されうる。ロックが取得できなかった場合
    （＝同じ案件・カテゴリで既に別のリクエストが処理中）は、新しい例外型を増やさず既存の
    `DuplicateApprovalRequestError`を送出する（`src/api/app.py`が422へ変換する既存経路を
    そのまま使う）。

    ただしメッセージ文面はロック競合（このケース）と、既存の`in_progress`行を検出した場合
    （`find_in_progress_approval()`）とで意図的に分けている（QAレビューWARN対応、2026-08-28）。
    前者はボタン連打や別ウィンドウからのほぼ同時送信による一時的な取り合い負けで「少し待てば
    通る」、後者は数時間前に既に送信済みで「既存の承認が片付くまで送ってはいけない」と、
    ユーザーが取るべき行動が正反対なため、同じ文面のままだと後者を前者と誤認して連打してしまう
    （実害はないが、営業担当者が「本当に送信済みなのか」と不安になる）。例外型自体は分けて
    いない——`src/api/app.py`は両ケースとも`DuplicateApprovalRequestError`を422へ変換する処理が
    同一で、`dashboard/app/api/documents/quote/request-approval/route.ts`も例外型を見ず
    `detail`文字列をそのままフロントへ渡すだけのため、型を分けても呼び出し元の分岐が増える
    わけではない。文面を分けるだけで目的（誤認防止）を達成できると判断した。

    **このロックはテンプレートコピー・セル差し込み・PDF変換・`start_approval()`という
    一連のDrive API呼び出しを跨いで保持される**（`insert_document_approval()`完了まで
    解放しない）。承認リクエスト送信は営業担当者が画面のボタンを押す低頻度の操作であり、
    同じ案件へ同時に2人以上が送信を試みる頻度は低いと判断し、ロック保持が数秒〜十数秒に
    及ぶトレードオフを許容している（詳細はdocs/quote_approval_note.md参照）。
    部分ユニークインデックス（`("notionProjectId", category) WHERE status = 'in_progress'`）
    ではなくロック方式を選んだ理由も同ドキュメント参照（既存データに重複した`in_progress`行が
    1組でもあると`CREATE UNIQUE INDEX`自体が失敗し、`prisma migrate deploy`はビルド時に
    走るためビルドごと失敗しデプロイが止まるリスクがある）。

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
    if not approver_emails:
        raise InvalidApproverEmailError("承認者を1人以上選択してください。")
    # 順序を保ったまま重複を除去する(dict.fromkeysはPython 3.7+で挿入順を保持する)。
    deduped_approver_emails = list(dict.fromkeys(approver_emails))
    invalid_approver_emails = [
        email for email in deduped_approver_emails if not is_active_document_approver(email)
    ]
    if invalid_approver_emails:
        # 実際にこのエラーが起きるのは「画面を開いたまま管理者が承認者を無効化した」ケースが
        # 主(送信直前まで画面上はチェックできていた承認者が、サーバー側検証の時点では
        # 無効化されている)。生のメールアドレスの列挙だけでは営業担当者が何をすればいいか
        # 分からないため、次の行動(再読み込みして選び直す)を明記する(obasan-qualityレビュー
        # WARN対応)。氏名への変換は行わない——Python側はDocumentApproverの氏名を持たない設計
        # (承認リクエスト送信時にPython側へ渡されるのは選択済みのapprover_emailsのみで、
        # DocumentApprover自体はdashboard側がPrismaで直接CRUDする、モジュールdocstring参照)の
        # ため、ここで氏名解決のためだけにDBアクセスを増やすのは今回のスコープでは見送る。
        raise InvalidApproverEmailError(
            f"{'、'.join(invalid_approver_emails)}は承認者として登録されていません。"
            "ページを再読み込みして承認者を選び直してください。"
        )
    # 重複チェック(find_in_progress_approval)からinsert_document_approvalまでの区間を
    # (notion_page_id, _CATEGORY)キーのアドバイザリロックで直列化する(TOCTOU対策、
    # 2026-08-28。詳細はこの関数のdocstring・docs/quote_approval_note.md参照)。
    # 非ブロッキングのpg_try_advisory_lockのため、同じ案件・カテゴリで既に別のリクエストが
    # 処理中の場合はここで即座にNoneが返り、Drive APIを一切呼ばずDuplicateApprovalRequestError
    # を送出する(待たせて後で通すのではなく即時失敗させるUXを選んだ)。
    lock_conn = try_acquire_approval_lock(notion_page_id, _CATEGORY)
    if lock_conn is None:
        # ロック競合(=今まさに同じ案件・カテゴリの別リクエストが処理中)。「進行中」と同じ
        # 文面にすると、数秒待てば通る一時的な失敗なのか、数時間前送信済みで既存承認が
        # 片付くまで送ってはいけないのかが区別できず誤認させる(QAレビューWARN対応、
        # 2026-08-28。このifブロック直前のコメント・関数docstring参照)。
        raise DuplicateApprovalRequestError(
            f"この案件（{notion_page_id}）の見積書承認リクエストの処理が重なりました。"
            "少し待ってから、もう一度お試しください。"
        )
    try:
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
            overrides=overrides,
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
                copy_id, reviewer_emails=deduped_approver_emails, message=message
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
                approver_emails=deduped_approver_emails,
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
    finally:
        release_approval_lock(lock_conn, notion_page_id, _CATEGORY)
