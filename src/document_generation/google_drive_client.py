"""Google Drive API (`https://www.googleapis.com/drive/v3/files`) との連携を行う
`GoogleDriveDocClient`。

テンプレートファイルのOffice形式→Google native形式変換コピー・PDF/Office形式へのエクスポート・
一時コピーの削除を担う。テンプレートは共有ドライブ(Shared Drive)上に置かれているため、
全リクエストで`supportsAllDrives=true`を必ず付与する（実データ確認済み。付け忘れると
mimeTypeが判明していても404 File not foundになる）。

認証は`google_auth.get_google_access_token()`経由（サービスアカウント優先・`GOOGLE_ACCESS_TOKEN`
へフォールバック）で解決したBearerトークンを使う。`access_token`引数を明示指定した場合は
そちらを優先する（テスト用）。
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from src.document_generation.google_auth import get_google_access_token
from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    raise_for_error,
    request_with_retry,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.googleapis.com/drive/v3/files"


class GoogleDriveApiError(ApiError):
    """Google Drive API呼び出し失敗時に送出する例外。"""


class GoogleDriveDocClient:
    def __init__(
        self,
        *,
        access_token: str | None = None,
        base_url: str = _BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        # access_tokenを明示指定しない場合は毎リクエスト時にget_google_access_token()を
        # 呼び、サービスアカウントの自動更新（有効期限切れ間近での再取得）を効かせる。
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def _headers(self) -> dict[str, str]:
        token = self._access_token if self._access_token is not None else get_google_access_token()
        return {"Authorization": f"Bearer {token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        idempotent: bool = True,
    ) -> requests.Response:
        params_with_shared_drive = {"supportsAllDrives": "true", **(params or {})}
        return request_with_retry(
            method,
            f"{self._base_url}{path}",
            headers=self._headers(),
            json_body=json_body,
            params=params_with_shared_drive,
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
            idempotent=idempotent,
        )

    def get_mime_type(self, file_id: str) -> str:
        response = self._request("GET", f"/{file_id}", params={"fields": "mimeType"})
        raise_for_error(response, GoogleDriveApiError)
        return response.json()["mimeType"]

    def copy_as_native(self, file_id: str, *, target_mime_type: str, new_name: str) -> str:
        """テンプレートをコピーしつつ、指定したGoogle native形式(`target_mime_type`)へ変換する。

        Office形式(.xlsx/.docx)→native変換にも、ネイティブ同士のコピーにも使える
        （実データで動作確認済み: `files.copy`のリクエストボディにnative形式のmimeTypeを
        明示指定すると、コピー時に自動変換される）。コピー系（非冪等）操作のため、
        5xx/タイムアウト時の重複コピー生成を避けリトライしない。コピー先のfile_idを返す。
        """
        response = self._request(
            "POST",
            f"/{file_id}/copy",
            json_body={"mimeType": target_mime_type, "name": new_name},
            idempotent=False,
        )
        raise_for_error(response, GoogleDriveApiError)
        return response.json()["id"]

    def export(self, file_id: str, *, mime_type: str) -> bytes:
        response = self._request("GET", f"/{file_id}/export", params={"mimeType": mime_type})
        raise_for_error(response, GoogleDriveApiError)
        return response.content

    def delete(self, file_id: str) -> None:
        """一時コピーファイルを削除する。削除失敗時は例外を送出せずログ警告のみ出す
        （テンプレート生成処理自体は既に完了しているため、後片付けの失敗で全体を失敗扱いにしない）。
        """
        try:
            # DELETEは同じfile_idに対して繰り返し呼んでも副作用が増えない冪等な操作。
            # idempotent=Falseにするとリトライが完全に無効化され、429/5xx・タイムアウト時に
            # 一時コピーがDrive上に残り続けやすくなる（idempotent=Trueでリトライを効かせる）。
            response = self._request("DELETE", f"/{file_id}", idempotent=True)
            raise_for_error(response, GoogleDriveApiError)
        except (GoogleDriveApiError, requests.exceptions.RequestException) as exc:
            logger.warning("failed to delete temporary Drive copy file_id=%r: %s", file_id, exc)
