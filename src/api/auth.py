"""ダッシュボードAPI向けの簡易トークン認証（FastAPI依存性）。

`DASHBOARD_API_TOKEN`環境変数とリクエストの`Authorization: Bearer <token>`ヘッダーを
比較する。fail-closed設計であり、`DASHBOARD_API_TOKEN`が未設定の場合はデフォルトで
全リクエストを401にする。`src/sync_engine/webhook_handlers/_common.py`の
`verify_webhook_secret`（`ALLOW_UNSIGNED_WEBHOOKS`パターン）と意図的に挙動を揃えている。
ローカル開発でトークン未発行のまま動作確認したい場合のみ、`ALLOW_UNAUTHENTICATED_DASHBOARD_API`
環境変数を`"true"`（大文字小文字無視）に明示的に設定することで未認証アクセスを許容できる。
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def verify_dashboard_api_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPIの依存性として使う。認証失敗時は401 HTTPExceptionを送出する。

    トークン比較は`hmac.compare_digest`（定数時間比較）で行い、文字列の`!=`比較による
    タイミングサイドチャネルを避ける（Geminiクロスレビューでの指摘を反映）。
    """
    expected = os.environ.get("DASHBOARD_API_TOKEN")
    if not expected:
        if os.environ.get("ALLOW_UNAUTHENTICATED_DASHBOARD_API", "").strip().lower() == "true":
            return
        raise HTTPException(status_code=401, detail="unauthorized")

    if authorization is None or not hmac.compare_digest(authorization, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="unauthorized")


def verify_cron_secret(authorization: str | None = Header(default=None)) -> None:
    """Vercel Cronからの呼び出しであることを検証するFastAPI依存性。

    Vercelは`CRON_SECRET`環境変数が設定されているプロジェクトに対し、Cron Jobからの
    リクエストへ自動的に`Authorization: Bearer $CRON_SECRET`ヘッダーを付与する
    （https://vercel.com/docs/cron-jobs/manage-cron-jobs#securing-cron-jobs）。
    `verify_dashboard_api_token`と同様fail-closed設計であり、`CRON_SECRET`未設定時は
    デフォルトで全リクエストを401にする。
    """
    expected = os.environ.get("CRON_SECRET")
    if not expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    if authorization is None or not hmac.compare_digest(authorization, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="unauthorized")


def verify_email_reminder_cron_secret(authorization: str | None = Header(default=None)) -> None:
    """`GET /api/cron/email-reminder-check`専用のFastAPI依存性。

    このエンドポイントはVercel Cron（1日1回までのHobbyプラン制約）ではなく、
    GitHub Actionsのscheduled workflow（1時間おき）から呼ばれるため、Vercelが自動付与する
    `CRON_SECRET`は使わず、専用の`EMAIL_REMINDER_CRON_SECRET`（GitHub Secrets側と対になる値）
    で検証する（他のWebhookハンドラが呼び出し元ごとに専用シークレットを持つのと同じ方針）。
    `verify_cron_secret`と同様fail-closed・定数時間比較。
    """
    expected = os.environ.get("EMAIL_REMINDER_CRON_SECRET")
    if not expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    if authorization is None or not hmac.compare_digest(authorization, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="unauthorized")
