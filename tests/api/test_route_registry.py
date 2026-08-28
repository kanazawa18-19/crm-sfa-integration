"""公開しているHTTPルートの一覧そのものを固定するテスト（2026-08-28）。

**なぜ必要か**: このAPIのパスは、外部システム側に登録された宛先である。

- `/api/cron/*` … `vercel.json`のcron定義（Vercelが叩きに来る）
- `/api/webhooks/*` … kintone/Zoho/Notion/Gmail(Pub/Sub)/Slack/MA側に登録済みのURL

つまり**パスを1文字変えたり、リファクタリングで登録し忘れたりすると、こちらのテストは
全部通ったまま本番の同期だけが静かに止まる**。実際このプロジェクトでは、cronが
`vercel.json`の配置ミスで一度も実行されていなかった事故が起きている
（docs/参照）。個々のハンドラの振る舞いを見るテストは別にあるが、
「どのパスが存在するか」自体を守るテストは無かったため、ここで固定する。

リファクタリングでルーターへ分割する等、**構成を変えてもこのリストが変わらないこと**が
安全に進めるための最低条件になる。意図的にパスを増減させる場合だけ、この期待値を
一緒に更新すること。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.routing import APIRoute

from src.api.app import app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERCEL_JSON = _REPO_ROOT / "vercel.json"

# 実装されている全ルート（メソッド, パス）。増減させる場合はここも更新する。
_EXPECTED_ROUTES = {
    ("GET", "/healthz"),
    # --- 外部システムから叩かれるWebhook ---
    ("POST", "/api/webhooks/notion"),
    ("POST", "/api/webhooks/kintone"),
    ("POST", "/api/webhooks/zoho"),
    ("POST", "/api/webhooks/spreadsheet"),
    ("POST", "/api/webhooks/web-engagement"),
    ("POST", "/api/webhooks/web-engagement-meeting"),
    ("POST", "/api/webhooks/gmail-push"),
    ("POST", "/api/webhooks/lead-inquiry"),
    ("POST", "/api/webhooks/slack-interactions"),
    # --- スケジューラから叩かれるcron ---
    ("GET", "/api/cron/daily-batch"),
    ("GET", "/api/cron/token-encryption-healthcheck"),
    ("GET", "/api/cron/gmail-sync"),
    ("GET", "/api/cron/gmail-watch-renewal"),
    ("GET", "/api/cron/incident-digest"),
    ("GET", "/api/cron/email-reminder-check"),
    ("GET", "/api/cron/document-approval-poll"),
    ("GET", "/api/cron/zoho-webhook-renewal"),
    ("GET", "/api/cron/project-mirror-reconcile"),
    ("GET", "/api/cron/relation-sync-reconcile"),
    # --- dashboard(Next.js)から叩かれる読み取り系 ---
    ("GET", "/api/dashboard/summary"),
    ("GET", "/api/reports/daily"),
    ("GET", "/api/members/performance"),
    ("GET", "/api/alerts/manager"),
    ("GET", "/api/tasks"),
    ("GET", "/api/projects/search"),
    ("GET", "/api/clients/search"),
    ("GET", "/api/contacts/search"),
    ("GET", "/api/clients/{client_id}/360"),
    ("GET", "/api/documents/generate"),
    ("POST", "/api/documents/quote/request-approval"),
    ("GET", "/api/settings/revenue-target-sheet"),
    ("POST", "/api/settings/revenue-target-sheet"),
}


def _registered_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue  # FastAPIが自動で足す /docs 等は対象外
        for method in route.methods or set():
            if method in {"HEAD", "OPTIONS"}:
                continue
            routes.add((method, route.path))
    return routes


def test_registered_routes_match_the_expected_set() -> None:
    """公開ルートの集合が期待どおりであること（増減の両方を検出する）。"""
    registered = _registered_routes()

    missing = _EXPECTED_ROUTES - registered
    unexpected = registered - _EXPECTED_ROUTES

    assert not missing, (
        f"期待していたルートが登録されていません: {sorted(missing)}。"
        "外部システム（Vercel cron / kintone / Zoho / Notion / Slack / MA）が"
        "このパスを叩きに来るため、消えると本番の同期が静かに止まります。"
    )
    assert not unexpected, (
        f"想定外のルートが増えています: {sorted(unexpected)}。"
        "意図した追加であれば、このテストの_EXPECTED_ROUTESにも追加してください。"
    )


def test_every_vercel_cron_path_is_actually_implemented() -> None:
    """`vercel.json`のcronパスが、実際にこのアプリへ実装されていること。

    登録だけあって実装が無い（あるいはパスがずれている）と、Vercelは毎日404を叩き続け、
    その処理は永久に走らない。過去に実際に起きた事故の再発防止。
    """
    crons = json.loads(_VERCEL_JSON.read_text(encoding="utf-8")).get("crons", [])
    assert crons, "vercel.jsonにcron定義がありません"

    registered_paths = {path for _, path in _registered_routes()}
    missing = [c["path"] for c in crons if c["path"] not in registered_paths]

    assert not missing, (
        f"vercel.jsonに登録されているのに実装が無いcronパス: {missing}。"
        "Vercelはこのパスを叩き続けますが404になり、処理は実行されません。"
    )


def test_every_cron_route_requires_a_secret() -> None:
    """すべての`/api/cron/*`が認証依存（`verify_*_cron_secret`）を持つこと。

    cronのURLは推測しやすく、認証が無いと誰でも夜間バッチを起動できてしまう。
    新しいcronを足すときに`dependencies=[Depends(verify_cron_secret)]`を書き忘れる事故を
    ここで止める。
    """
    unprotected: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/cron/"):
            continue
        dependency_names = {
            call.__name__
            for call in (d.call for d in route.dependant.dependencies)
            if call is not None
        }
        if not any(name.startswith("verify_") for name in dependency_names):
            unprotected.append(route.path)

    assert not unprotected, (
        f"認証依存が付いていないcronルート: {unprotected}。"
        "dependencies=[Depends(verify_cron_secret)]（専用シークレットの場合は該当の"
        "verify_*関数）を付けてください。"
    )
