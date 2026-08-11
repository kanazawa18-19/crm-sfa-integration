"""webhook_handlers配下で共有する小さなヘルパー。

BLOCKER5（ペイロード不正・欠損時の未捕捉例外）・BLOCKER7（署名検証・認証の欠如）への
対応として、各ハンドラ共通のエラーレスポンス整形・共有シークレット検証もここに集約する。
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# BLOCKER7: 共有トークン方式によるWebhook認証で参照するヘッダー名。
# 各ツールの実際の署名方式（HMAC署名か単純な共有トークンか）は仕様書に明記が無いため、
# まずは全ツール共通の単純な共有トークン方式で実装する。本番では各ツールの標準署名検証
# 方式（Notion: Verification Token、kintone/Zoho/GAS: HMAC署名等）に置き換えること。
WEBHOOK_SECRET_HEADER = "X-Webhook-Secret"


def get_header(headers: Mapping[str, str], name: str) -> str | None:
    """HTTPヘッダー名の大文字小文字を区別せずに値を取得する。

    API Gateway / Lambda Function URL 等、ヘッダーキーの大文字小文字表記が
    経路によって揺れるため、ここで吸収する。
    """
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def parse_iso_datetime(value: str) -> datetime:
    """各ツールのタイムスタンプ文字列（末尾Z含む）をdatetimeへ変換する。

    Notion/kintoneはUTCを末尾"Z"で表す（例: 2026-08-05T09:00:00.000Z）ため、
    datetime.fromisoformatが解釈できる+00:00表記へ変換してから渡す。
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def verify_webhook_secret(headers: Mapping[str, str], env_var: str) -> bool:
    """共有シークレットによるWebhook認証（BLOCKER7）。fail-closed設計。

    env_varで指定した環境変数（例: KINTONE_WEBHOOK_SECRET）にX-Webhook-Secretヘッダーが
    一致する場合のみ通過させる。env_varが未設定の場合はデフォルトで検証失敗（拒否）とする。
    本番デプロイ時に環境変数の設定を忘れただけで認証なしの書き込みエンドポイントが
    野放しになる事態を防ぐため。

    ローカル開発でシークレット未発行のまま動作確認したい場合のみ、環境変数
    ALLOW_UNSIGNED_WEBHOOKS=true を明示的に設定することでenv_var未設定時の通過を許容できる
    （この場合もシークレットが設定されていて値が不一致のリクエストは引き続き拒否する）。
    """
    expected = os.environ.get(env_var)
    if expected:
        return get_header(headers, WEBHOOK_SECRET_HEADER) == expected
    return os.environ.get("ALLOW_UNSIGNED_WEBHOOKS", "").strip().lower() == "true"


def verify_webhook_body_token(body: Mapping[str, Any], *, token_field: str, env_var: str) -> bool:
    """リクエストbody内に埋め込まれた共有トークンによるWebhook認証。fail-closed設計。

    Zoho CRM Notifications（watch）APIのように、外部ツール側の仕様上、着信リクエストへ
    任意のHTTPヘッダーを付与させられないケース向け。verify_webhook_secret()（ヘッダー方式）
    と同じfail-closedの考え方で、env_varで指定した環境変数（例: ZOHO_WEBHOOK_SECRET）が
    body[token_field]と一致する場合のみ通過させる。env_var未設定時はデフォルトで検証失敗
    （拒否）とし、ローカル開発でのみ ALLOW_UNSIGNED_WEBHOOKS=true による通過を許容する
    （この場合もシークレットが設定されていて値が不一致のリクエストは引き続き拒否する）。

    比較には hmac.compare_digest() を使い、タイミングサイドチャネルによるトークン漏洩を防ぐ
    （単純な==比較は文字列長・一致文字数に応じて比較時間が変わり得るため避ける）。
    """
    expected = os.environ.get(env_var)
    if expected:
        actual = body.get(token_field)
        if not isinstance(actual, str):
            return False
        return hmac.compare_digest(actual, expected)
    return os.environ.get("ALLOW_UNSIGNED_WEBHOOKS", "").strip().lower() == "true"


def unauthorized_response() -> dict[str, Any]:
    """BLOCKER7: 共有シークレット不一致時の401レスポンス。"""
    return {"statusCode": 401, "body": json.dumps({"error": "invalid webhook secret"})}


def bad_request_response(message: str) -> dict[str, Any]:
    """BLOCKER5: ペイロードのパース・変換失敗時の400レスポンス（内部詳細を含む簡潔なメッセージのみ）。"""
    return {"statusCode": 400, "body": json.dumps({"error": message})}


def internal_error_response() -> dict[str, Any]:
    """BLOCKER5: 予期しない例外発生時の500レスポンス。詳細はログにのみ出力し、
    レスポンスボディには内部実装の詳細を含めない。"""
    return {"statusCode": 500, "body": json.dumps({"error": "internal server error"})}
