"""テンプレート管理DB「【営業部】見積書／規約／申込書／契約書」からテンプレートファイルを
動的に解決する`TemplateRegistry`。

`ファイル&メディア`プロパティが`external`タイプ（Google DriveのURLを指す）の場合のみ対応する
（`file`タイプ=Notion添付のPDF/画像固定文書は本機能のスコープ外）。ファイルIDはハードコードせず、
毎回Notion APIをクエリして解決する。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    raise_for_error,
    request_with_retry,
)

logger = logging.getLogger(__name__)

_NOTION_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"

# 「【営業部】見積書／規約／申込書／契約書」DBのdatabase_id。
TEMPLATE_DATABASE_ID = "0f60fd81-990d-4f7e-870c-6a4c52615e5a"

_PROP_CATEGORY = "カテゴリ"
_PROP_SERVICE = "サービス"
_PROP_FILES = "ファイル&メディア"

# https://docs.google.com/spreadsheets/d/{FILE_ID}/edit?gid=... や
# https://docs.google.com/document/d/{FILE_ID}/edit のようなURLからFILE_IDを抽出する。
_DRIVE_FILE_ID_PATTERN = re.compile(r"/d/([a-zA-Z0-9_-]+)")


class TemplateRegistryError(ApiError):
    """テンプレート管理DBへのNotion API呼び出し失敗時に送出する例外。"""


@dataclass(frozen=True)
class TemplateInfo:
    file_id: str
    file_name: str
    mime_type_hint: str | None


class TemplateRegistry:
    """「【営業部】見積書／規約／申込書／契約書」DBをクエリし、テンプレートファイルを解決する。"""

    def __init__(
        self,
        *,
        database_id: str = TEMPLATE_DATABASE_ID,
        api_key: str | None = None,
        base_url: str = _BASE_URL,
        notion_version: str = _NOTION_VERSION,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._database_id = database_id
        self._api_key = api_key if api_key is not None else os.environ.get("NOTION_API_KEY")
        if not self._api_key:
            raise ValueError(
                "NOTION_API_KEY environment variable (or api_key argument) is required but not set"
            )
        self._base_url = base_url.rstrip("/")
        self._notion_version = notion_version
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Notion-Version": self._notion_version,
            "Content-Type": "application/json",
        }

    def find_template(self, category: str, service: str) -> TemplateInfo | None:
        """`カテゴリ`が一致し`サービス`に`service`を含む最初の1件を返す（無ければNone）。

        Notion API側で`カテゴリ`(select)一致・`サービス`(multi_select)包含の複合filterを
        指定してクエリするため、該当件数は少ない前提でページングは行わない
        （軽量な実装で十分という前提のシンプルな1回クエリ）。
        """
        response = request_with_retry(
            "POST",
            f"{self._base_url}/databases/{self._database_id}/query",
            headers=self._headers(),
            json_body={
                "filter": {
                    "and": [
                        {"property": _PROP_CATEGORY, "select": {"equals": category}},
                        {"property": _PROP_SERVICE, "multi_select": {"contains": service}},
                    ]
                }
            },
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
        )
        raise_for_error(response, TemplateRegistryError)
        results = response.json().get("results") or []
        for page in results:
            template = _extract_template_info(page)
            if template is not None:
                return template
        return None


def _extract_template_info(page: dict[str, Any]) -> TemplateInfo | None:
    properties = page.get("properties") or {}
    files_prop = properties.get(_PROP_FILES) or {}
    files = files_prop.get("files") or []
    for file in files:
        if file.get("type") != "external":
            # Notion添付ファイル（file）はスコープ外（今回対象は見積書/申込書/契約書のみで、
            # これらは全てGoogle DriveのURLを指すexternalファイルとして登録されている想定）。
            continue
        url = (file.get("external") or {}).get("url")
        if not url:
            continue
        match = _DRIVE_FILE_ID_PATTERN.search(url)
        if not match:
            logger.warning("could not extract Google Drive file id from url: %r", url)
            continue
        file_name = file.get("name") or ""
        return TemplateInfo(
            file_id=match.group(1), file_name=file_name, mime_type_hint=_guess_mime_type_hint(file_name)
        )
    return None


def _guess_mime_type_hint(file_name: str) -> str | None:
    lower = file_name.lower()
    if lower.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if lower.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if lower.endswith(".pdf"):
        return "application/pdf"
    return None
