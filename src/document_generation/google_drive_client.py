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
import time
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
_UPLOAD_BASE_URL = "https://www.googleapis.com/upload/drive/v3/files"

# start_approval()のapprovalIdフォールバック(list_approvals())のリトライ回数・間隔。
# Drive Approvals APIの結果整合性(eventual consistency)対策(2026-08-18実機確認)。
_START_APPROVAL_FALLBACK_ATTEMPTS = 4
_START_APPROVAL_FALLBACK_DELAY_SECONDS = 1.5


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
        include_shared_drive_support: bool = True,
    ) -> requests.Response:
        # `supportsAllDrives`は`files`リソース(copy/get/update等)向けのクエリパラメータで、
        # `approvals:start`等のApprovals系エンドポイントには存在しない(2026-08-18実機確認、
        # 付与すると`HTTP 400: Unknown name "supportsAllDrives"`になる)。Approvals系メソッド
        # (start_approval/get_approval/cancel_approval)は`include_shared_drive_support=False`
        # で呼ぶこと。
        params_with_shared_drive = (
            {"supportsAllDrives": "true", **(params or {})} if include_shared_drive_support else (params or {})
        )
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

    def copy_as_native(
        self, file_id: str, *, target_mime_type: str, new_name: str, parents: list[str] | None = None
    ) -> str:
        """テンプレートをコピーしつつ、指定したGoogle native形式(`target_mime_type`)へ変換する。

        Office形式(.xlsx/.docx)→native変換にも、ネイティブ同士のコピーにも使える
        （実データで動作確認済み: `files.copy`のリクエストボディにnative形式のmimeTypeを
        明示指定すると、コピー時に自動変換される）。コピー系（非冪等）操作のため、
        5xx/タイムアウト時の重複コピー生成を避けリトライしない。コピー先のfile_idを返す。

        `parents`(2026-08-18、見積書承認リクエストフロー向けに追加)を指定すると、コピー先を
        テンプレートと同じ場所ではなく指定フォルダ直下に作成する。省略時（既存呼び出し元、
        使い捨ての一時コピー用途）はテンプレートと同じ場所にコピーされる従来通りの挙動のまま。
        """
        json_body: dict[str, Any] = {"mimeType": target_mime_type, "name": new_name}
        if parents is not None:
            json_body["parents"] = parents
        response = self._request(
            "POST",
            f"/{file_id}/copy",
            json_body=json_body,
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

    def move(self, file_id: str, *, add_parent: str, remove_parent: str) -> None:
        """`file_id`を`remove_parent`フォルダから`add_parent`フォルダへ移動する(2026-08-18、
        見積書承認フローの「一時格納フォルダ→送付済みフォルダ」移動向けに新設)。

        `PATCH /files/{id}?addParents=...&removeParents=...`はファイル本体を書き換えない
        冪等な操作のため、他メソッドと同様リトライを許容する(idempotent=Trueが既定)。
        """
        response = self._request(
            "PATCH",
            f"/{file_id}",
            params={"addParents": add_parent, "removeParents": remove_parent},
        )
        raise_for_error(response, GoogleDriveApiError)

    def rename(self, file_id: str, *, name: str) -> None:
        """`file_id`の表示名を変更する(2026-08-19、`replace_content()`でPDFへ内容変換した
        コピーの名前を`.pdf`拡張子付きに揃えるために新設)。"""
        response = self._request("PATCH", f"/{file_id}", json_body={"name": name})
        raise_for_error(response, GoogleDriveApiError)

    def replace_content(self, file_id: str, *, content: bytes, mime_type: str) -> None:
        """`file_id`の中身をまるごと置き換える(単純メディアアップロード、`uploadType=media`、
        2026-08-19)。Sheets形式のコピーをPDFへ変換する用途向け——`export()`で取得したPDF
        バイト列をそのまま同じfile_idへ書き戻すことで、承認対象ファイル自体をPDF化する
        (承認リクエストは編集可能なファイルではなくPDFで送るのが既存の運用実態だったため)。

        通常の`_request()`とは異なる`/upload/drive/v3/files/{id}`エンドポイントを使うため、
        ここだけ`request_with_retry`を直接呼ぶ(`supportsAllDrives`もこのアップロード系
        エンドポイントには存在しない)。
        """
        response = request_with_retry(
            "PATCH",
            f"{_UPLOAD_BASE_URL}/{file_id}",
            headers={**self._headers(), "Content-Type": mime_type},
            data=content,
            params={"uploadType": "media"},
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
            idempotent=False,
        )
        raise_for_error(response, GoogleDriveApiError)

    def start_approval(self, file_id: str, *, reviewer_email: str, message: str = "") -> str:
        """Google Driveの純正「承認をリクエスト」機能(Drive Approvals)で、`reviewer_email`宛に
        承認リクエストを送信する(2026-08-18)。返り値はポーリング(`get_approval()`)に使う
        承認リクエストID(`approvalId`)。

        リクエスト形状は公式REST référence
        (https://developers.google.com/workspace/drive/api/reference/rest/v3/approvals/start)で
        確認済み: `POST /files/{fileId}/approvals:start`、body は
        `{"reviewerEmails": [...], "message": "..."}`。

        公式ドキュメント上は`approvals:start`のレスポンスにも`approvalId`が含まれる想定だが、
        2026-08-18の実機テストでは`fields`クエリパラメータを指定しない場合`{"kind":
        "drive#approval"}`のみの部分レスポンス(Google APIのデフォルト projection)しか
        返ってこなかった(`list_approvals()`/`get_approval()`も同様)。Drive APIは`fields`を
        明示しないと必要なフィールドが省略されることがあるため、`fields=*`を明示的に付与する。
        なお`fields=*`指定後も念のため、レスポンスに`approvalId`が無い場合は
        `list_approvals()`で直後に作成された`IN_PROGRESS`状態の承認を探すフォールバックを
        行う(結果整合性対策)。
        """
        json_body: dict[str, Any] = {"reviewerEmails": [reviewer_email]}
        if message:
            json_body["message"] = message
        response = self._request(
            "POST",
            f"/{file_id}/approvals:start",
            json_body=json_body,
            params={"fields": "*"},
            idempotent=False,
            include_shared_drive_support=False,
        )
        raise_for_error(response, GoogleDriveApiError)
        data = response.json()
        approval_id = data.get("approvalId")
        if approval_id:
            return approval_id

        # list_approvals()側も直後は結果整合性(eventual consistency)により空を返すことが
        # ある(2026-08-18実機テストで確認)。数回リトライしてから諦める。
        last_seen: list[dict[str, Any]] = []
        for attempt in range(_START_APPROVAL_FALLBACK_ATTEMPTS):
            if attempt > 0:
                time.sleep(_START_APPROVAL_FALLBACK_DELAY_SECONDS)
            last_seen = self.list_approvals(file_id)
            for approval in last_seen:
                if approval.get("status") == "IN_PROGRESS":
                    fallback_id = approval.get("approvalId")
                    if fallback_id:
                        return fallback_id

        raise GoogleDriveApiError(
            response.status_code,
            f"no approvalId in start_approval response ({data!r}) and none found via "
            f"list_approvals after {_START_APPROVAL_FALLBACK_ATTEMPTS} attempts: {last_seen!r}",
        )

    def list_approvals(self, file_id: str) -> list[dict[str, Any]]:
        """`file_id`上の承認リクエスト一覧を取得する(`GET /files/{fileId}/approvals`、
        公式REST referenceで確認済み)。`start_approval()`のフォールバック用。"""
        response = self._request(
            "GET",
            f"/{file_id}/approvals",
            params={"fields": "*"},
            include_shared_drive_support=False,
        )
        raise_for_error(response, GoogleDriveApiError)
        return response.json().get("items", [])

    def get_approval(self, file_id: str, approval_id: str) -> dict[str, Any]:
        """承認リクエストの現在の状態(レスポンスの`status`フィールド: `IN_PROGRESS`/
        `APPROVED`/`DECLINED`/`CANCELLED`)を取得する(2026-08-18、承認状態ポーリングcron向け、
        フィールド名は公式REST referenceで確認済み)。"""
        response = self._request(
            "GET",
            f"/{file_id}/approvals/{approval_id}",
            params={"fields": "*"},
            include_shared_drive_support=False,
        )
        raise_for_error(response, GoogleDriveApiError)
        return response.json()

    def cancel_approval(self, file_id: str, approval_id: str) -> None:
        """進行中の承認リクエストを取り消す(2026-08-18、`start_approval()`成功後にDB書き込み
        (`insert_document_approval()`)が失敗した場合の後片付け向けに追加。放置すると
        Drive上には承認リクエストが送信済みなのにシステム側に一切記録が残らない状態になる)。
        `POST /files/{fileId}/approvals/{approvalId}:cancel`(approvals.cancel、公式REST
        referenceで確認済み)。"""
        response = self._request(
            "POST",
            f"/{file_id}/approvals/{approval_id}:cancel",
            idempotent=False,
            include_shared_drive_support=False,
        )
        raise_for_error(response, GoogleDriveApiError)
