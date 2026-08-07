"""案件データから契約書(Word)を生成する。

条項本文の自動生成は行わず、Docs API `documents.batchUpdate`の`replaceAllText`で宛先
プレースホルダ「〇〇」（実データ確認済み、例:「〇〇ホテルズを甲，株式会社コネクター・ジャパン
を乙とする」）を取引先名へ置換するのみを行う（スコープ限定）。
"""

from __future__ import annotations

import logging
from typing import Any

from src.document_generation.common import (
    BASELINE_NOTE,
    ContractGenerationError,
    DocumentResult,
    resolve_template,
)
from src.document_generation.google_auth import get_google_access_token
from src.document_generation.google_drive_client import GoogleDriveDocClient
from src.document_generation.project_data import fetch_project_document_data
from src.document_generation.template_registry import TemplateRegistry
from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    raise_for_error,
    request_with_retry,
)

logger = logging.getLogger(__name__)

_CATEGORY = "契約書"
_NATIVE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
_DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_ADDRESSEE_PLACEHOLDER = "〇〇"
_DOCS_BASE_URL = "https://docs.googleapis.com/v1/documents"


class DocsApiError(ApiError):
    """Google Docs API呼び出し失敗時に送出する例外。"""


class GoogleDocsTextReplacer:
    """Docs API `documents.batchUpdate`(replaceAllText)の薄いラッパー。"""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        base_url: str = _DOCS_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def _headers(self) -> dict[str, str]:
        token = self._access_token if self._access_token is not None else get_google_access_token()
        return {"Authorization": f"Bearer {token}"}

    def replace_all_text(self, document_id: str, *, search_text: str, replace_text: str) -> int:
        """`search_text`を`replace_text`へ全置換し、実際に置換された件数を返す。

        戻り値（`occurrencesChanged`）を呼び出し元が検証できるようにする。Docs APIの
        `replaceAllText`はドキュメント全文中の一致箇所を無条件に全て置換する仕様のため、
        件数を確認せずに使うと、宛先プレースホルダのつもりが日付・金額欄等の意図しない
        箇所まで書き換えてしまう事故につながる（法的文書のため実害が大きい）。
        """
        response = request_with_retry(
            "POST",
            f"{self._base_url}/{document_id}:batchUpdate",
            headers=self._headers(),
            json_body={
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {"text": search_text, "matchCase": True},
                            "replaceText": replace_text,
                        }
                    }
                ]
            },
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
        )
        raise_for_error(response, DocsApiError)
        body = response.json()
        replies = body.get("replies") or []
        if not replies:
            return 0
        return replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)


def generate_contract(
    notion_page_id: str,
    *,
    registry: TemplateRegistry | None = None,
    drive_client: GoogleDriveDocClient | None = None,
    docs_client: GoogleDocsTextReplacer | None = None,
    notion_client: Any | None = None,
    client_master_client: Any | None = None,
) -> DocumentResult:
    """案件データを取得し、テンプレートを解決・コピーして宛先プレースホルダを置換し、
    Word(.docx)としてexportする。生成完了後、Drive上の一時コピーは削除する。"""
    resolved_registry = registry or TemplateRegistry()
    project_data = fetch_project_document_data(
        notion_page_id, notion_client=notion_client, client_master_client=client_master_client
    )
    template = resolve_template(_CATEGORY, project_data.proposed_services, resolved_registry)

    resolved_drive_client = drive_client or GoogleDriveDocClient()
    resolved_docs_client = docs_client or GoogleDocsTextReplacer()
    notes: list[str] = [BASELINE_NOTE]

    copy_id = resolved_drive_client.copy_as_native(
        template.file_id,
        target_mime_type=_NATIVE_DOC_MIME_TYPE,
        new_name=f"__tmp_contract_{notion_page_id}",
    )
    try:
        if project_data.client_name:
            occurrences = resolved_docs_client.replace_all_text(
                copy_id, search_text=_ADDRESSEE_PLACEHOLDER, replace_text=project_data.client_name
            )
            if occurrences != 1:
                # 「〇〇」はテンプレート本文中に複数回（日付欄「〇〇年〇〇月〇〇日」等）
                # 出現している可能性があり、無条件の全置換では意図しない箇所まで
                # 取引先名で書き換えてしまう事故につながる（法的文書のため実害が大きい）。
                # 想定通り1箇所だけヒットした場合のみ生成を成功とする（安全側に倒す）。
                raise ContractGenerationError(
                    f"契約書の自動生成に失敗しました（宛先プレースホルダ「{_ADDRESSEE_PLACEHOLDER}」"
                    f"の置換件数が想定外の{occurrences}件でした）。"
                    "情報システム担当（テンプレート管理者）に連絡してください。"
                )
        else:
            notes.append(
                "取引先名が案件データから取得できなかったため、宛先プレースホルダ「〇〇」は未置換です。"
            )
        content = resolved_drive_client.export(copy_id, mime_type=_DOCX_MIME_TYPE)
    finally:
        resolved_drive_client.delete(copy_id)

    return DocumentResult(
        content=content,
        file_name=f"{project_data.project_name or notion_page_id}_契約書.docx",
        mime_type=_DOCX_MIME_TYPE,
        notes=notes,
    )
