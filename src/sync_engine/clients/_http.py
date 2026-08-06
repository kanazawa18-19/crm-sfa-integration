"""clients配下で共有する小さなヘルパー（タイムアウト・簡易リトライ・エラー整形）。

Phase2レビューでタイムアウト未設定が問題視された教訓を踏まえ、全クライアント共通で
タイムアウト・429/5xx時の指数バックオフ付き簡易リトライをここに集約する
（webhook_handlers/_common.py と同様の「小さな共有ヘルパー」方針）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5

# 429（レート制限）と一時的なサーバーエラーのみリトライ対象とする。
# 4xx（400/401/403/404等）はリトライしても解消しないため即座に呼び出し元へ返す。
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class ApiError(Exception):
    """各ツールAPIエラーの共通基底クラス。ツールごとのサブクラスは各clientモジュールで定義する。"""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any | None = None,
    params: Mapping[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    sleep: Callable[[float], None] | None = None,
    idempotent: bool = True,
) -> requests.Response:
    """指数バックオフ付きの簡易リトライでHTTPリクエストを送る。

    タイムアウト・429/5xxのみリトライする。認証情報やレスポンス内容はログに出さない
    （ステータスコードとメソッド／URLのみ記録する）。

    idempotent=False（レコード作成などの非冪等な操作）の場合、タイムアウトやレスポンス
    未達がサーバー側では処理済みだった場合にリトライすると重複作成につながるため、
    max_retriesの指定に関わらずリトライしない（実質max_retries=0として扱う）。
    更新系（PATCH/PUT）は同じ内容を再送しても結果が変わらないためidempotent=True（既定）でよい。

    sleep未指定時（既定）はtime.sleepを都度参照する（デフォルト引数値としてtime.sleepを
    直接束縛すると、モジュールロード時点の関数オブジェクトが固定され、テストでの
    `monkeypatch.setattr("...clients._http.time.sleep", ...)` によるパッチが効かず
    テストが実際に待機してしまうため、呼び出しのたびにtime.sleepを動的に参照する）。
    """
    effective_max_retries = max_retries if idempotent else 0
    _sleep = sleep if sleep is not None else time.sleep
    attempt = 0
    while True:
        try:
            response = requests.request(
                method,
                url,
                headers=dict(headers) if headers is not None else None,
                json=json_body,
                params=dict(params) if params is not None else None,
                timeout=timeout,
            )
        except requests.exceptions.Timeout:
            attempt += 1
            if attempt > effective_max_retries:
                raise
            logger.warning(
                "request timed out, retrying (%d/%d): %s %s",
                attempt,
                effective_max_retries,
                method,
                url,
            )
            _sleep(backoff_base * (2 ** (attempt - 1)))
            continue

        if response.status_code in _RETRYABLE_STATUS_CODES and attempt < effective_max_retries:
            attempt += 1
            logger.warning(
                "request failed with retryable status %d, retrying (%d/%d): %s %s",
                response.status_code,
                attempt,
                effective_max_retries,
                method,
                url,
            )
            _sleep(backoff_base * (2 ** (attempt - 1)))
            continue

        return response


def extract_error_message(response: requests.Response) -> str:
    """エラーレスポンスから簡潔なメッセージを取り出す（レスポンス全文はログに出さない）。"""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        message = body.get("message") or body.get("error") or body
    else:
        message = body
    return str(message)[:200]


def raise_for_error(response: requests.Response, error_cls: type[ApiError]) -> None:
    """4xx/5xxの場合、指定のApiErrorサブクラスを送出する。"""
    if response.ok:
        return
    raise error_cls(response.status_code, extract_error_message(response))
