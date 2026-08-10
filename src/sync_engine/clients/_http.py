"""clients配下で共有する小さなヘルパー（タイムアウト・簡易リトライ・エラー整形）。

Phase2レビューでタイムアウト未設定が問題視された教訓を踏まえ、全クライアント共通で
タイムアウト・429/5xx時の指数バックオフ付き簡易リトライをここに集約する
（webhook_handlers/_common.py と同様の「小さな共有ヘルパー」方針）。

429（レート制限）は5xx/タイムアウトとは異なる専用の大きめのリトライ予算
（DEFAULT_MAX_RATE_LIMIT_RETRIES）を持ち、idempotent=Falseでも常にリトライする
（2026-08-10、Notion本番一括投入で最初のレート制限到達時に即座に処理全体が
落ちていた問題への対応。詳細はrequest_with_retry()のdocstring参照）。
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, Mapping

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5

# 429は他のリトライ可能ステータスと異なり「そのうち解消する」ことが前提の一時的な状態
# （レート制限のウィンドウが経過すれば必ず成功するようになる）のため、DEFAULT_MAX_RETRIES
# （数回）ではなく、大量データの一括作成のような数時間規模の処理でも簡単に使い切らない
# 十分大きな回数を既定値とする。無制限にはせず、キー失効等の別要因を429と誤認していた
# 場合に無限ループしない安全弁として上限は設ける。
DEFAULT_MAX_RATE_LIMIT_RETRIES = 30
# shirokuma-secレビューWARN対応（2026-08-10）: DEFAULT_MAX_RATE_LIMIT_RETRIES(30)は
# 移行スクリプトのような数時間規模のバルク処理を想定した値で、ワーカースレッドが
# ブロックされても他に実害が無い。しかし本モジュールはNotionクライアント全体で共有される
# ため、これをダッシュボード/タスクAPIのような同期的なリクエストハンドラの既定値にすると、
# 移行処理がNotionをレート制限させている最中に来た通常の閲覧リクエストが最悪
# 30回 * _MAX_RATE_LIMIT_BACKOFF_SECONDS(30秒) ≒ 15分近くブロックされ、プラットフォーム側の
# タイムアウトで強制終了しうる。リクエスト/レスポンス型の呼び出し元は、この小さい方の値を
# 明示的に渡すこと（`HttpNotionClient`/`NotionUserDirectory`の`max_rate_limit_retries`引数）。
INTERACTIVE_MAX_RATE_LIMIT_RETRIES = 3
# 429リトライのバックオフ上限（秒）。指数バックオフをそのまま伸ばすと数分単位の無駄待ちに
# なるため、Notion側のレート制限ウィンドウ（実測でおおむね1秒程度でリセットされる）に対して
# 現実的な待機時間で頭打ちにする。
# obasan-qualityレビューINFO対応: 上のDEFAULT_MAX_RATE_LIMIT_RETRIES(30)と値がたまたま
# 同じ「30」だが、こちらは秒数の上限であり意味的な関連は無い（前者はリトライ「回数」）。
_MAX_RATE_LIMIT_BACKOFF_SECONDS = 30.0

# 一時的なサーバーエラーのみリトライ対象とする（429は下記で別枠として扱う）。
# 4xx（400/401/403/404等）はリトライしても解消しないため即座に呼び出し元へ返す。
_RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})


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
    max_rate_limit_retries: int = DEFAULT_MAX_RATE_LIMIT_RETRIES,
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

    ただし429（レート制限）だけはidempotentフラグに関わらず常にリトライ対象とする。429は
    APIゲートウェイ側のレート制限で「リクエストが実際の処理に到達する前に拒否された」ことが
    保証されるレスポンスのため、タイムアウトや5xxと異なり「サーバー側で処理済みかどうか
    不明」という非冪等操作特有のリスクが無い。当初は429も他のリトライ可能ステータスと
    まとめて`idempotent=False`時は一律リトライ対象外にしていたが、これだと大量データの
    一括作成（例: Notion移行の148,000件書き込み）で最初にレート制限へ到達した瞬間に
    即座に例外が飛び、処理全体が停止してしまう問題があった。429のみidempotentの対象外として
    分離し、常にリトライすることで、長時間実行のバルク作成処理がレート制限のたびに落ちずに
    完走できるようにする。

    sleep未指定時（既定）はtime.sleepを都度参照する（デフォルト引数値としてtime.sleepを
    直接束縛すると、モジュールロード時点の関数オブジェクトが固定され、テストでの
    `monkeypatch.setattr("...clients._http.time.sleep", ...)` によるパッチが効かず
    テストが実際に待機してしまうため、呼び出しのたびにtime.sleepを動的に参照する）。
    """
    effective_max_retries = max_retries if idempotent else 0
    _sleep = sleep if sleep is not None else time.sleep
    attempt = 0
    rate_limit_attempt = 0
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

        if response.status_code == 429:
            if rate_limit_attempt >= max_rate_limit_retries:
                # obasan-qualityレビューWARN対応（2026-08-10）: 上限到達時に何もログを
                # 出さないと、無人長時間実行のログを後から追う運用者が「retrying (n/N)」を
                # 自分で数えて上限到達を推測することになる。呼び出し元へエラーとして
                # 返す直前に、上限に到達して諦めたことを明示する。
                logger.warning(
                    "rate limit retries exhausted after %d attempts, giving up: %s %s",
                    rate_limit_attempt,
                    method,
                    url,
                )
                return response
            rate_limit_attempt += 1
            wait_seconds = _rate_limit_backoff_seconds(response, rate_limit_attempt, backoff_base)
            logger.warning(
                "request rate-limited (429), retrying (%d/%d) in %.1fs: %s %s",
                rate_limit_attempt,
                max_rate_limit_retries,
                wait_seconds,
                method,
                url,
            )
            _sleep(wait_seconds)
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


def _rate_limit_backoff_seconds(
    response: requests.Response, attempt: int, backoff_base: float
) -> float:
    """429応答の待機秒数を決める。NotionはじめほとんどのAPIは`Retry-After`ヘッダーで
    具体的な待機秒数を返すため、あればそれを優先する（サーバー側の実際のレート制限
    ウィンドウ残り時間に即した最短の待機になる）。ヘッダーが無い/パースできない場合のみ
    指数バックオフにフォールバックし、_MAX_RATE_LIMIT_BACKOFF_SECONDSで頭打ちにする。

    shirokuma-secレビューWARN対応: 当初はRetry-Afterヘッダーの値をそのまま信頼しており
    上限が無かった。異常に大きいが有限な値（サーバー・プロキシの不具合等）は
    _MAX_RATE_LIMIT_BACKOFF_SECONDSで頭打ちにする。`"inf"`のような`float()`がValueErrorを
    送出しない非有限値は、そもそも待機秒数として扱えないため、ヘッダー無し扱いと同様に
    指数バックオフ（それ自体も同じ上限で頭打ち）へフォールバックする。
    """
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            seconds = float(retry_after)
        except ValueError:
            seconds = None
        if seconds is not None and math.isfinite(seconds):
            return min(max(0.0, seconds), _MAX_RATE_LIMIT_BACKOFF_SECONDS)
    return min(backoff_base * (2 ** (attempt - 1)), _MAX_RATE_LIMIT_BACKOFF_SECONDS)


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
