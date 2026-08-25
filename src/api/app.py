"""ダッシュボード（管理画面）向けREST APIのFastAppアプリケーション本体。

社内限定・簡易認証のWebアプリ（`dashboard/`、別エージェントが並行実装中）から呼び出される
バックエンドAPI。CORSは`DASHBOARD_FRONTEND_ORIGIN`環境変数で指定したoriginのみ許可する
fail-closed設計（未設定時は一切許可しない）。
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.api.auth import (
    verify_cron_secret,
    verify_dashboard_api_token,
    verify_document_approval_cron_secret,
    verify_email_reminder_cron_secret,
)
from src.api.client_360_service import get_client_360, search_clients, search_contacts
from src.api.token_encryption_healthcheck import run_token_encryption_healthcheck
from src.api.dashboard_service import (
    build_daily_report,
    build_dashboard_summary,
    build_manager_alerts,
    build_member_performance,
    search_projects,
)
from src.api.task_service import build_tasks
from src.api.user_directory import NotionUserDirectory
from src.document_generation.application_generator import generate_application
from src.document_generation.approval_poll import poll_document_approvals
from src.document_generation.common import (
    ContractGenerationError,
    TemplateNotFoundError,
    TemplateSheetNotFoundError,
)
from src.document_generation.contract_generator import generate_contract
from src.document_generation.quote_generator import (
    DriveNotConnectedError,
    DuplicateApprovalRequestError,
    InvalidApproverEmailError,
    QuoteOverrides,
    generate_quote,
    request_quote_approval,
)
from src.email_reminders.reminder_check import run_reminder_check
from src.gmail_sync.sync import sync_all
from src.gmail_sync.watch_registration import (
    GmailWatchNotConfiguredError,
    renew_all_watches,
)
from src.incident_detection.notify import run_incident_digest
from src.project_mirror.sync import refresh_all_projects
from src.relation_sync.sync import refresh_all_client_names
from src.reports.batch import run_report_batch
from src.reports.revenue_target_settings import (
    RevenueTargetSettingsStore,
    build_revenue_target_settings_store,
)
from src.reports.revenue_target_sheet import (
    RevenueTargetSheetFormatError,
    RevenueTargetSheetPointer,
    fetch_mrr_targets,
    fetch_unit_count_targets,
)
from src.sync_engine.clients._http import ApiError, INTERACTIVE_MAX_RATE_LIMIT_RETRIES
from src.sync_engine.clients.notion_client import NotionApiError
from src.sync_engine.clients.zoho_client import ZohoApiError
from src.sync_engine.production_wiring import ProductionSyncWiring, get_production_wiring
from src.sync_engine.webhook_handlers.gmail_push_webhook import (
    handler as gmail_push_webhook_handler,
)
from src.sync_engine.webhook_handlers.kintone_webhook import handler as kintone_webhook_handler
from src.sync_engine.webhook_handlers.lead_inquiry_webhook import (
    handler as lead_inquiry_webhook_handler,
)
from src.sync_engine.webhook_handlers.notion_webhook import (
    handler_with_proxy as notion_webhook_handler_with_proxy,
)
from src.sync_engine.webhook_handlers.slack_interaction_webhook import (
    handler as slack_interaction_webhook_handler,
)
from src.sync_engine.webhook_handlers.spreadsheet_webhook import (
    handler as spreadsheet_webhook_handler,
)
from src.sync_engine.webhook_handlers.web_engagement_meeting_webhook import (
    handler as web_engagement_meeting_webhook_handler,
)
from src.sync_engine.webhook_handlers.web_engagement_webhook import (
    handler as web_engagement_webhook_handler,
)
from src.sync_engine.webhook_handlers.zoho_webhook import handler as zoho_webhook_handler
from src.sync_engine.zoho_watch_channel import (
    ZohoWatchChannelNotConfiguredError,
    build_zoho_client_from_env,
    renew_zoho_watch_channel,
)

logger = logging.getLogger(__name__)

_DOCUMENT_CATEGORIES = ("見積書", "申込書", "契約書")

_JST = timezone(timedelta(hours=9))


def _today_jst() -> date:
    return datetime.now(_JST).date()


def _parse_date_param(value: str | None, *, param_name: str) -> date:
    if value is None:
        return _today_jst()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid {param_name}: {value!r} (expected YYYY-MM-DD)"
        ) from exc


def _cors_allowed_origins() -> list[str]:
    raw = os.environ.get("DASHBOARD_FRONTEND_ORIGIN", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


app = FastAPI(title="CRM/SFA Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed_origins(),
    allow_credentials=True,
    # POSTは/api/settings/revenue-target-sheet（事業計画スプレッドシート連携設定の保存）
    # 向けに追加。他エンドポイントは全てGETのまま。
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    # ブラウザ側のfetch（dashboard/）から/api/documents/generateのカスタムヘッダーを
    # 読めるようにする（未指定だとContent-Dispositionを含め非単純ヘッダーは既定で見えない）。
    expose_headers=["X-Document-Notes", "Content-Disposition"],
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _wiring_dependency() -> ProductionSyncWiring:
    """本番用のDispatcher一式（`src/sync_engine/production_wiring.py`）を返すFastAPI依存性。

    プロセス内シングルトンのため毎リクエストで作り直さない
    （`dashboard_service.py`のモジュールレベルキャッシュと同じ流儀）。テストでは
    `app.dependency_overrides[_wiring_dependency]`で差し替える。
    """
    return get_production_wiring()


async def _lambda_event_from_request(request: Request) -> dict[str, Any]:
    """FastAPIの`Request`を、Webhookハンドラ（Lambda形式）が期待する`event`辞書へ変換する。

    `query_params`はkintone_webhook.pyのクエリパラメータ方式の共有シークレット検証
    （`verify_webhook_query_param()`、kintoneのWebhook機能がカスタムHTTPヘッダーを
    送信できないための代替手段）で使う。他のハンドラは無視して構わない。
    """
    body = await request.body()
    return {
        "headers": dict(request.headers),
        "body": body.decode("utf-8"),
        "query_params": dict(request.query_params),
    }


def _partial_skip_summary(dispatcher: Any) -> list[dict[str, Any]] | None:
    """`SkipTrackingDispatcher.last_result`（直近のdispatch()結果）から、意図した書き込み先
    ツールのうち実際には反映されなかったものがあるプロパティの一覧を組み立てる。

    無ければNoneを返す（`dispatcher`がlast_resultを持たない場合・部分スキップが無い場合の
    いずれも含む）。obasan-quality/shirokuma-secレビュー: 「同期スキップが成功として見える」
    問題への対応として、warningログだけでなくWebhookレスポンス自体にも反映する。
    """
    last_result = getattr(dispatcher, "last_result", None)
    if last_result is None or not getattr(last_result, "has_partial_skips", False):
        return None
    return [
        {
            "property": p.property_name,
            "written_tools": sorted(t.value for t in p.written_tools),
            "skipped_tools": sorted(t.value for t in p.skipped_tools),
        }
        for p in last_result.properties
        if p.skipped_tools
    ]


def _lambda_result_to_response(result: dict[str, Any], *, dispatcher: Any = None) -> Response:
    """Webhookハンドラが返す`{"statusCode":..., "body":...}`をFastAPIの`Response`へ変換する。

    `dispatcher`（`SkipTrackingDispatcher`）を渡すと、直近のdispatch()で意図した書き込み先
    ツールのうち実際には反映されなかったものがあった場合、レスポンスボディへ
    `partial_sync_skipped`フィールドとして追記する（ログだけでなくレスポンスからも
    後から追えるようにするため）。
    """
    body = result.get("body", "")
    if dispatcher is not None:
        partial_skip = _partial_skip_summary(dispatcher)
        if partial_skip is not None:
            try:
                body_data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                body_data = {}
            body_data["partial_sync_skipped"] = partial_skip
            body = json.dumps(body_data, ensure_ascii=False)
    return Response(
        content=body,
        status_code=result["statusCode"],
        media_type="application/json",
    )


# --- Webhook受信エンドポイント（リアルタイム連携） ------------------------------------------
# 認証は各handler内部の共有シークレット検証で行う（多くはX-Webhook-Secretヘッダー、
# src/sync_engine/webhook_handlers/_common.pyのverify_webhook_secret）。ただしZoho
# （カスタムヘッダー・bodyへの任意フィールド追加のいずれも不可、body内tokenフィールド方式、
# verify_webhook_body_token）・kintone（カスタムヘッダー不可、URLクエリパラメータ方式、
# verify_webhook_query_param）は、外部ツール側のWebhook機能の制約により別方式を使う。
#
# 2026-08-14時点の各ツール側Webhook購読登録状況: Zohoは本番登録済み・稼働中
# （docs/zoho_webhook_activation_note.md参照）。kintoneはkintone→Notion方向を有効化する
# 方針となり、本モジュール側の実装は完了（docs/kintone_webhook_activation_note.md参照）だが
# kintone管理画面側での購読登録はこの時点ではまだ手動作業が残っている。


@app.post("/api/webhooks/notion")
async def webhook_notion(
    request: Request, wiring: ProductionSyncWiring = Depends(_wiring_dependency)
) -> Response:
    """Notion API Webhooksの受信エンドポイント。

    実際のNotion API Webhooksのペイロードはページ全体を含まないため、
    `handler_with_proxy()`（ページ全体をNotion APIから再取得するプロキシ層）を使う。
    """
    event = await _lambda_event_from_request(request)
    if wiring.notion_page_client is None:
        logger.error(
            "webhook_notion: NOTION_API_KEY等が未設定のためNotion同期が構成されておらず、"
            "Webhookを処理できません"
        )
        return Response(
            content=json.dumps({"error": "notion sync is not configured"}),
            status_code=500,
            media_type="application/json",
        )
    result = notion_webhook_handler_with_proxy(
        event,
        context=None,
        notion_client=wiring.notion_page_client,
        dispatcher=wiring.dispatcher,
        calendar_sync=wiring.calendar_sync_callable,
        lead_sync=wiring.lead_sync_callable,
        project_mirror_sync=wiring.project_mirror_sync_callable,
        client_name_index_sync=wiring.client_name_index_sync_callable,
    )
    return _lambda_result_to_response(result, dispatcher=wiring.dispatcher)


@app.post("/api/webhooks/kintone")
async def webhook_kintone(
    request: Request, wiring: ProductionSyncWiring = Depends(_wiring_dependency)
) -> Response:
    event = await _lambda_event_from_request(request)
    # id_mapping_store/notion_client: 取引先マスターリレーションの「後勝ち」上書き防止ガード用
    # （2026-08-25、GPT-5.6クロスレビュー指摘対応。kintone_webhook.pyのモジュールdocstring
    # 参照）。wiring.notion_page_client未設定（NOTION_API_KEY未設定）の場合はNoneのまま渡され、
    # ガード自体が無効化される（kintone_webhook側は既存の挙動にフォールバックする）。
    result = kintone_webhook_handler(
        event,
        context=None,
        dispatcher=wiring.dispatcher,
        id_mapping_store=wiring.id_mapping_store,
        notion_client=wiring.notion_page_client,
    )
    return _lambda_result_to_response(result, dispatcher=wiring.dispatcher)


@app.post("/api/webhooks/zoho")
async def webhook_zoho(
    request: Request, wiring: ProductionSyncWiring = Depends(_wiring_dependency)
) -> Response:
    event = await _lambda_event_from_request(request)
    # id_mapping_store/notion_client/zoho_client: ⑥アクション履歴DBの取引先マスターリレーション
    # 自動解決・「後勝ち」上書き防止ガード用（2026-08-25、Round2。kintone側と同じ設計、
    # zoho_webhook.pyのモジュールdocstring参照）。wiring.notion_page_client/zoho_action_client
    # が未設定（NOTION_API_KEY/Zoho認証情報未設定）の場合はNoneのまま渡され、当該機能自体が
    # 無効化される（zoho_webhook側は既存の挙動にフォールバックする）。
    result = zoho_webhook_handler(
        event,
        context=None,
        dispatcher=wiring.dispatcher,
        id_mapping_store=wiring.id_mapping_store,
        notion_client=wiring.notion_page_client,
        zoho_client=wiring.zoho_action_client,
    )
    return _lambda_result_to_response(result, dispatcher=wiring.dispatcher)


@app.post("/api/webhooks/spreadsheet")
async def webhook_spreadsheet(
    request: Request, wiring: ProductionSyncWiring = Depends(_wiring_dependency)
) -> Response:
    event = await _lambda_event_from_request(request)
    result = spreadsheet_webhook_handler(event, context=None, dispatcher=wiring.dispatcher)
    return _lambda_result_to_response(result, dispatcher=wiring.dispatcher)


@app.post("/api/webhooks/web-engagement")
async def webhook_web_engagement(request: Request) -> Response:
    """web-engagement-tool（別リポジトリ）からのリードのホットリード化・新規識別通知の受信。

    `Dispatcher`/`IdMappingStore`は経由しない設計（`web_engagement_webhook.handler`の
    docstring参照）のため、`_wiring_dependency`（Dispatcher一式）には依存しない。
    """
    event = await _lambda_event_from_request(request)
    result = web_engagement_webhook_handler(event, context=None)
    return _lambda_result_to_response(result)


@app.post("/api/webhooks/web-engagement-meeting")
async def webhook_web_engagement_meeting(request: Request) -> Response:
    """web-engagement-tool（別リポジトリ）からのGoogleカレンダー商談イベント通知の受信。

    `Dispatcher`/`IdMappingStore`は経由しない設計（`web_engagement_meeting_webhook.handler`の
    docstring参照）のため、`_wiring_dependency`（Dispatcher一式）には依存しない。マッチした
    案件があればSlackへ承認依頼を投稿するのみで、この時点ではまだNotionへ書き込まない。
    """
    event = await _lambda_event_from_request(request)
    result = web_engagement_meeting_webhook_handler(event, context=None)
    return _lambda_result_to_response(result)


@app.post("/api/webhooks/gmail-push")
async def webhook_gmail_push(request: Request) -> Response:
    """Google Cloud Pub/Subからの Gmail Push通知(`users.watch()`登録済みの新着メール検知)の
    受信エンドポイント。

    `Dispatcher`/`IdMappingStore`は経由しない設計(`gmail_push_webhook.handler`の
    docstring参照)のため、`_wiring_dependency`(Dispatcher一式)には依存しない。担当者が
    見つからない・処理中の例外いずれも、Pub/Subの再送ループを防ぐため常に200を返す。
    """
    event = await _lambda_event_from_request(request)
    result = gmail_push_webhook_handler(event, context=None)
    return _lambda_result_to_response(result)


@app.post("/api/webhooks/lead-inquiry")
async def webhook_lead_inquiry(request: Request) -> Response:
    """lead-researcher（別リポジトリ、問い合わせメール自動調査Slackボット）からの
    リード情報受信。

    `Dispatcher`/`IdMappingStore`は経由しない設計（`lead_inquiry_webhook.handler`の
    docstring参照）のため、`_wiring_dependency`（Dispatcher一式）には依存しない。
    """
    event = await _lambda_event_from_request(request)
    result = lead_inquiry_webhook_handler(event, context=None)
    return _lambda_result_to_response(result)


@app.post("/api/webhooks/slack-interactions")
async def webhook_slack_interactions(request: Request) -> Response:
    """Slack interactivity（承認/対象外ボタンの押下）の受信。

    `webhook_web_engagement_meeting`がSlackへ投稿した承認依頼メッセージへのコールバック。
    署名検証は共有トークン方式ではなくSlack標準の署名方式（`slack_interaction_webhook`
    内で実施）。承認時のみNotionアクション履歴DBへ実際に書き込む。
    """
    event = await _lambda_event_from_request(request)
    result = slack_interaction_webhook_handler(event, context=None)
    return _lambda_result_to_response(result)


# --- 定期実行バッチ（日報・週報） -----------------------------------------------------------


@app.get("/api/cron/daily-batch", dependencies=[Depends(verify_cron_secret)])
def run_daily_batch() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、日報・週報配信バッチのエントリポイント。

    日報は毎日、週報は金曜日のみ配信する（`src.reports.batch.run_report_batch`参照）。
    """
    return run_report_batch()


@app.get("/api/cron/token-encryption-healthcheck", dependencies=[Depends(verify_cron_secret)])
def run_token_encryption_healthcheck_cron() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、`TOKEN_ENCRYPTION_KEY`の自己診断エントリポイント
    (2026-08-18)。詳細は`src/api/token_encryption_healthcheck.py`のモジュールdocstring参照。
    """
    return run_token_encryption_healthcheck()


@app.get("/api/cron/gmail-sync", dependencies=[Depends(verify_cron_secret)])
def run_gmail_sync() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、Gmail連携(src/gmail_sync/)の同期エントリポイント。

    Gmail連携済みの営業担当ごとに直近のメールをポーリングし、連絡先DBとメアド一致した
    ものだけをEmailLogへ記録・Notion連絡先ページの「最終メール日時」を更新する。
    対応するweb-engagement-tool側のLeadがあれば、あわせてWebhookで通知する
    (`src/gmail_sync/notify.py`、未設定なら通知はスキップされ同期処理自体は継続する)。
    """
    return sync_all()


@app.get("/api/cron/gmail-watch-renewal", dependencies=[Depends(verify_cron_secret)])
def run_gmail_watch_renewal() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、Gmail Push通知(`users.watch()`)の自動延長エントリ
    ポイント(2026-08-16)。

    Gmailのwatchは登録・延長時点から最大7日で失効し、放置すると`/api/webhooks/gmail-push`
    への通知が無音で止まる(この場合も日次の`sync_all()`セーフティネットが拾うため即座に
    データが失われるわけではないが、リアルタイム性が失われる)。`renew_all_watches()`は
    失効が近い(残り2日以内)/未登録の担当者だけを対象に登録・延長する。

    Pub/Subトピック(`GMAIL_PUBSUB_TOPIC_NAME`)が未設定の場合は、成功したように見える
    no-opにせず明確な500エラーとして表面化させる(`renew_zoho_watch_channel()`と同じ方針)。
    """
    try:
        return renew_all_watches()
    except GmailWatchNotConfiguredError as exc:
        logger.error("gmail watch renewal failed (not configured): %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/cron/incident-digest", dependencies=[Depends(verify_cron_secret)])
def run_incident_digest_cron() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、インシデント・アクシデント検知
    (`src/incident_detection/`)の中優先度日次ダイジェスト配信エントリポイント(2026-08-16)。

    高優先度(スコア8点以上)は`src/gmail_sync/sync.py`側で受信メール記録時に即座に
    Slack通知される。このcronは中優先度(4〜7点)を直近24時間分まとめて1通で配信する。
    """
    return run_incident_digest()


@app.get("/api/cron/email-reminder-check", dependencies=[Depends(verify_email_reminder_cron_secret)])
def run_email_reminder_check() -> dict[str, Any]:
    """GitHub Actionsのscheduled workflow(`.github/workflows/email-reminder-check.yml`、
    1時間おき)から呼ばれる、未返信メールリマインド(`src/email_reminders/`)のエントリ
    ポイント(2026-08-16)。

    Vercel Hobbyプランのcron制約(1日1回まで)では1時間おきの実行が組めないため、
    `vercel.json`には登録せず、GitHub Actions側から専用シークレット
    (`EMAIL_REMINDER_CRON_SECRET`、GitHub Secrets側と対になる値)付きで直接叩く方式にする。
    既存の`CRON_SECRET`(Vercel Cron専用に運用中の値)とは意図的に分離している
    (`verify_email_reminder_cron_secret`参照)。
    """
    return run_reminder_check()


@app.get(
    "/api/cron/document-approval-poll",
    dependencies=[Depends(verify_document_approval_cron_secret)],
)
def run_document_approval_poll() -> dict[str, Any]:
    """GitHub Actionsのscheduled workflow(`.github/workflows/document-approval-poll.yml`、
    1時間おき)から呼ばれる、見積書承認リクエスト(`src/document_generation/approval_poll.py`)の
    状態確定ポーリングエントリポイント(2026-08-18)。

    Drive Approvalsはpush通知を持たないため、`email-reminder-check`と同じ理由
    （Vercel Hobbyプランのcron制約(1日1回まで)では1時間おきの実行が組めない）で
    `vercel.json`には登録せず、GitHub Actions側から専用シークレット
    (`DOCUMENT_APPROVAL_CRON_SECRET`)付きで直接叩く方式にする。
    """
    return poll_document_approvals()


@app.get("/api/cron/zoho-webhook-renewal", dependencies=[Depends(verify_cron_secret)])
def run_zoho_webhook_renewal() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、Zoho CRM Notifications（watch）チャンネルの
    自動延長（`PUT /crm/v3/actions/watch`）エントリポイント。

    Zohoのwatchチャンネルは登録・延長時点から最大1日で失効し、放置すると`/api/webhooks/zoho`
    への通知が無音で止まる（`docs/zoho_webhook_activation_note.md`参照）。Vercel Hobbyプランの
    制約でcronは1日1回しか実行できないため、`renew_zoho_watch_channel()`は毎回、Zoho上限の
    24hではなく21h先のchannel_expiryを要求し、3時間分の安全マージンを確保する
    （`expiry_days`未指定時の既定値`CRON_RENEWAL_EXPIRY_DAYS`）。対象モジュールも省略時は
    `DEFAULT_MODULES`（`Deals`/`CustomModule3`/`CustomModule2`/`Accounts`/`Contacts`/`Products`
    の6モジュール）全てを1つのwatchチャンネルでまとめて延長する。実際の延長ロジック・
    channel_idの一次情報源（環境変数`ZOHO_WATCH_CHANNEL_ID`）の設計判断は
    `src/sync_engine/zoho_watch_channel.py`の`renew_zoho_watch_channel()`を参照。

    延長対象のchannel_idが未設定、またはZoho API呼び出し自体が失敗した場合は、
    成功したように見えるno-opにせず、明確な500エラー（Vercel Cronからは失敗実行として
    検知される）として表面化させる。
    """
    try:
        client = build_zoho_client_from_env()
        result = renew_zoho_watch_channel(client, token=os.environ.get("ZOHO_WEBHOOK_SECRET"))
    except ZohoWatchChannelNotConfiguredError as exc:
        logger.error("zoho watch channel renewal failed (not configured): %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ZohoApiError as exc:
        logger.error("zoho watch channel renewal failed (zoho api error): %s", exc)
        raise HTTPException(status_code=502, detail=f"zoho api error: {exc}") from exc
    except Exception:
        # 上記2種類以外の想定外の例外（Zohoレスポンスの形が想定外だった場合の取りこぼし等）が
        # 生のトレースバック形状のままHTTP層へ漏れないようにする。
        # src/sync_engine/webhook_handlers/zoho_webhook.py の handler() の
        # `except Exception: logger.exception(...)` パターンと同じ方針（本エンドポイントには
        # 同種のガードが無かったため、後追いで揃える）。
        logger.exception("zoho watch channel renewal failed (unexpected error)")
        raise HTTPException(
            status_code=500, detail="internal error during zoho webhook renewal"
        ) from None

    logger.info(
        "zoho watch channel renewed: channel_id=%s channel_expiry=%s",
        result["channel_id"],
        result["channel_expiry"],
    )
    return {
        "status": "success",
        "channel_id": result["channel_id"],
        "channel_expiry": result["channel_expiry"],
    }


@app.get("/api/cron/project-mirror-reconcile", dependencies=[Depends(verify_cron_secret)])
def run_project_mirror_reconcile(
    wiring: ProductionSyncWiring = Depends(_wiring_dependency),
) -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、案件管理DBのPostgresミラー（`ProjectMirror`）の夜間
    reconciliationエントリポイント（2026-08-17）。

    Webhook経由のリアルタイム同期（`project_mirror_sync`）だけでは、Webhook購読登録前の
    既存データ・Webhook配信失敗・ページ削除等を取りこぼしうるため、`refresh_all_projects()`
    （初回バックフィルと共通の全件反映処理）をフル実行して整合させる。

    `PROJECT_MIRROR_SYNC_ENABLED`（既定false）が未設定の場合は書き込みをスキップする
    （shirokuma-sec/obasan-qualityレビューWARN対応、2026-08-17）。cronの`vercel.json`登録
    自体は「インフラ整備のみ」段階でも行うため、このガードが無いと環境変数を何も設定して
    いなくてもcron登録した時点で毎晩`ProjectMirror`への書き込みが始まってしまい、計画上の
    「インフラ整備のみでは本番挙動は変わらない」前提と食い違う
    （`build_project_mirror_sync_callable`と同じ「未設定なら無効化」パターンに揃える）。
    """
    if os.environ.get("PROJECT_MIRROR_SYNC_ENABLED", "").strip().lower() != "true":
        return {"skipped": "PROJECT_MIRROR_SYNC_ENABLED is not set"}
    if wiring.notion_page_client is None:
        logger.error(
            "run_project_mirror_reconcile: NOTION_API_KEY等が未設定のため実行できません"
        )
        raise HTTPException(status_code=500, detail="notion sync is not configured")
    # NotionUserDirectory()はNotionDataSourceの`_cached("user_directory", ...)`（プロセス内
    # 使い回し）とは意図的に異なり、cron実行のたびに新規構築する。本cronは1日1回・低頻度
    # である一方、担当メンバー名の解決結果（ワークスペースメンバー一覧）を毎回最新化したい
    # ため（コールドスタートを跨いだ古いキャッシュに固定されたくない）。
    user_directory = NotionUserDirectory(
        max_rate_limit_retries=INTERACTIVE_MAX_RATE_LIMIT_RETRIES
    )
    return refresh_all_projects(
        notion_client=wiring.notion_page_client, user_directory=user_directory
    )


@app.get("/api/cron/relation-sync-reconcile", dependencies=[Depends(verify_cron_secret)])
def run_relation_sync_reconcile(
    wiring: ProductionSyncWiring = Depends(_wiring_dependency),
) -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、取引先マスターDBの正規化取引先名→Notion page ID
    インデックス（`ClientNameIndex`）の夜間reconciliationエントリポイント（2026-08-25、
    shirokuma-sec/obasan-qualityレビューBLOCKER対応: ClientNameIndexへの投入経路が本番に
    一切配線されていなかった問題への対応。`run_project_mirror_reconcile`と同じ設計）。

    Webhook経由のリアルタイム同期（`client_name_index_sync`）だけでは、Webhook購読登録前の
    既存データ・Webhook配信失敗・ページ削除等を取りこぼしうるため、`refresh_all_client_names()`
    （初回バックフィルと共通の全件反映処理）をフル実行して整合させる。

    `RELATION_SYNC_ENABLED`（既定false）が未設定の場合は書き込みをスキップする
    （`run_project_mirror_reconcile`と同じ理由: cronの`vercel.json`登録自体は「インフラ整備
    のみ」段階でも行うため、このガードが無いと環境変数を何も設定していなくてもcron登録した
    時点で毎晩`ClientNameIndex`への書き込みが始まってしまい、「インフラ整備のみでは本番挙動は
    変わらない」前提と食い違う）。
    """
    if os.environ.get("RELATION_SYNC_ENABLED", "").strip().lower() != "true":
        return {"skipped": "RELATION_SYNC_ENABLED is not set"}
    if wiring.notion_page_client is None:
        logger.error(
            "run_relation_sync_reconcile: NOTION_API_KEY等が未設定のため実行できません"
        )
        raise HTTPException(status_code=500, detail="notion sync is not configured")
    return refresh_all_client_names(notion_client=wiring.notion_page_client)


@app.get("/api/dashboard/summary", dependencies=[Depends(verify_dashboard_api_token)])
def get_dashboard_summary() -> dict[str, Any]:
    return build_dashboard_summary()


@app.get("/api/reports/daily", dependencies=[Depends(verify_dashboard_api_token)])
def get_daily_report(date: str | None = None) -> dict[str, Any]:
    report_date = _parse_date_param(date, param_name="date")
    return build_daily_report(report_date)


@app.get("/api/members/performance", dependencies=[Depends(verify_dashboard_api_token)])
def get_member_performance(as_of: str | None = None) -> dict[str, Any]:
    as_of_date = _parse_date_param(as_of, param_name="as_of")
    return build_member_performance(as_of_date)


@app.get("/api/alerts/manager", dependencies=[Depends(verify_dashboard_api_token)])
def get_manager_alerts(as_of: str | None = None) -> dict[str, Any]:
    as_of_date = _parse_date_param(as_of, param_name="as_of")
    return build_manager_alerts(as_of_date)


@app.get("/api/tasks", dependencies=[Depends(verify_dashboard_api_token)])
def get_tasks() -> dict[str, Any]:
    return build_tasks()


@app.get("/api/projects/search", dependencies=[Depends(verify_dashboard_api_token)])
def get_project_search(q: str = "") -> dict[str, Any]:
    """書類自動生成画面の案件選択UIから呼ばれる、案件名の部分一致検索。"""
    if not q.strip():
        return {"projects": [], "total_matched": 0}
    return search_projects(q)


@app.get("/api/clients/search", dependencies=[Depends(verify_dashboard_api_token)])
def get_client_search(q: str = "") -> dict[str, Any]:
    """顧客360度ビュー画面の取引先選択UIから呼ばれる、取引先名の部分一致検索。"""
    if not q.strip():
        return {"clients": [], "truncated": False}
    return search_clients(q)


@app.get("/api/contacts/search", dependencies=[Depends(verify_dashboard_api_token)])
def get_contact_search(q: str = "") -> dict[str, Any]:
    """連絡先名の部分一致検索（将来の拡張用。現行UIでは未使用）。"""
    if not q.strip():
        return {"contacts": [], "truncated": False}
    return search_contacts(q)


@app.get("/api/clients/{client_id}/360", dependencies=[Depends(verify_dashboard_api_token)])
def get_client_360_view(client_id: str) -> dict[str, Any]:
    """顧客360度ビュー: 取引先1社の概要・案件・連絡先・アクション履歴をまとめて返す。"""
    result = get_client_360(client_id)
    if result is None:
        raise HTTPException(status_code=404, detail="取引先が見つかりません")
    return result


@app.get("/api/documents/generate", dependencies=[Depends(verify_dashboard_api_token)])
def generate_document(
    notion_project_id: str,
    category: str,
    memo: str | None = None,
    client_name: str | None = None,
    service_name: str | None = None,
    initial_fee: str | None = None,
    monthly_fee: str | None = None,
    creator_name: str | None = None,
) -> Response:
    """案件データから見積書(PDF)・申込書(Excel)・契約書(Word)を生成し、バイナリを返す。

    `memo`〜`creator_name`は書類作成画面の手動入力欄(2026-08-19追加)。見積書
    (`category == "見積書"`)にのみ適用され、他カテゴリでは無視する（申込書・契約書は
    このスコープ外のため、`QuoteOverrides`を渡さずNotion案件データのみで生成する）。
    """
    if category == "見積書":
        overrides = QuoteOverrides(
            memo=memo,
            client_name=client_name,
            service_name=service_name,
            initial_fee=initial_fee,
            monthly_fee=monthly_fee,
            creator_name=creator_name,
        )
        generator = functools.partial(generate_quote, overrides=overrides)
    elif category == "申込書":
        generator = generate_application
    elif category == "契約書":
        generator = generate_contract
    else:
        raise HTTPException(
            status_code=422,
            detail=f"invalid category: {category!r} (expected one of {_DOCUMENT_CATEGORIES})",
        )

    try:
        result = generator(notion_project_id)
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TemplateSheetNotFoundError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ContractGenerationError as exc:
        # 契約書の宛先プレースホルダ置換件数が想定外だった場合。利用者側の入力ミスでは
        # ないがサーバ内部エラーでもなく、テンプレート構成に起因する処理不能な状態のため
        # 422（Unprocessable Entity）とする。
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NotionApiError as exc:
        # 存在しない案件ID等、利用者側の入力に起因するエラーは404/422系として返し、
        # サーバ内部エラー（500）と区別する。
        status_code = 404 if exc.status_code == 404 else 422
        raise HTTPException(status_code=status_code, detail=f"notion api error: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="failed to generate document") from exc

    # Content-Disposition/HTTPヘッダーはlatin-1でしかエンコードできないため、file_nameに
    # 日本語（案件名由来）を含む場合に備え、RFC 5987のfilename*形式（UTF-8パーセントエンコード）
    # で指定する（生の日本語ファイル名をそのままheadersへ渡すとUnicodeEncodeErrorになる）。
    # safe=""で"/"も含めて全てパーセントエンコードする（RFC 5987のattr-charに"/"は含まれない）。
    encoded_file_name = quote(result.file_name, safe="")
    # DocumentResult.notes（先頭タブ使用の警告・宛先未反映等、生成物をそのまま送付してよいか
    # 利用者が判断するための注意事項）は、これまでレスポンスのどこにも含まれておらず生成
    # 結果と一緒に破棄されていた（obasan-qualityレビュー: BLOCKER指摘を反映）。ヘッダー値は
    # HTTP上ASCII/latin-1に限られるため、Content-Dispositionのfilename*と同様にJSON化した上で
    # UTF-8パーセントエンコード（safe=""で全文字エンコード）して返す。
    encoded_notes = quote(json.dumps(result.notes, ensure_ascii=False), safe="")
    return Response(
        content=result.content,
        media_type=result.mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_file_name}",
            "X-Document-Notes": encoded_notes,
        },
    )


class QuoteApprovalRequest(BaseModel):
    """`POST /api/documents/quote/request-approval`のリクエストボディ。

    `requested_by_email`はダッシュボード側(Next.js)がログイン中セッションから注入する値を
    信頼する想定（クライアントが任意のメールアドレスを詐称してDrive接続を借用できないよう、
    ダッシュボード側プロキシルートでサーバーセッションの値へ上書きする設計。
    `dashboard/app/gmail/oauth/callback/route.ts`のrepEmail扱いと同じ方針）。
    """

    project_id: str
    approver_email: str
    requested_by_email: str
    message: str = ""
    # 書類作成画面の手動入力欄(2026-08-19追加)。全項目任意。
    memo: str | None = None
    client_name: str | None = None
    service_name: str | None = None
    initial_fee: str | None = None
    monthly_fee: str | None = None
    creator_name: str | None = None


@app.post(
    "/api/documents/quote/request-approval", dependencies=[Depends(verify_dashboard_api_token)]
)
def request_document_quote_approval(payload: QuoteApprovalRequest) -> dict[str, Any]:
    """見積書を生成して一時格納フォルダへ保存し、Google Drive純正の「承認をリクエスト」機能で
    承認者へ送信する(2026-08-18、`src/document_generation/quote_generator.request_quote_approval`
    参照)。
    """
    overrides = QuoteOverrides(
        memo=payload.memo,
        client_name=payload.client_name,
        service_name=payload.service_name,
        initial_fee=payload.initial_fee,
        monthly_fee=payload.monthly_fee,
        creator_name=payload.creator_name,
    )
    try:
        result = request_quote_approval(
            payload.project_id,
            approver_email=payload.approver_email,
            requested_by_email=payload.requested_by_email,
            message=payload.message,
            overrides=overrides,
        )
    except DriveNotConnectedError as exc:
        # 依頼者本人のDrive OAuth接続(RepDriveConnection)が未接続の場合。フロント側で
        # 「Drive連携が必要です」+設定画面への導線を出せるよう、422で明確に区別する。
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InvalidApproverEmailError as exc:
        # approver_emailがDocumentApproverに未登録(active=trueで存在しない)の場合。
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DuplicateApprovalRequestError as exc:
        # 同じ案件・カテゴリで既にin_progressの承認リクエストが存在する場合。
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TemplateSheetNotFoundError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NotionApiError as exc:
        status_code = 404 if exc.status_code == 404 else 422
        raise HTTPException(status_code=status_code, detail=f"notion api error: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="failed to request quote approval") from exc

    return {
        "drive_file_id": result.drive_file_id,
        "drive_approval_id": result.drive_approval_id,
        "document_approval_id": result.document_approval_id,
    }


# --- 事業計画スプレッドシート連携設定（/api/settings/revenue-target-sheet） -----------------------
# 目標値の永続化方針（値そのものはNotionに複製せず、スプレッドシートへのポインタのみ保持する）は
# src/reports/revenue_target_sheet.py・src/reports/revenue_target_settings.pyのモジュール
# docstring参照。


_SPREADSHEET_URL_ID_PATTERN = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")

# 実際のGoogle スプレッドシートIDの形はこのレンジに収まる（英数字・アンダースコア・ハイフンのみ、
# 概ね40〜50文字程度）。ここでは多少の余裕を持たせつつ、`/`・`.`・空白等を含む値は一律拒否する。
# この値は`RevenueTargetSheetSettingsStore`経由でNotionへ永続化され、以後のバッチ実行のたびに
# `revenue_target_sheet.py`のリクエストURLへ直接埋め込まれるため、ここで弾かないと
# `../../drive/v3/files`のようなパストラバーサル的な値が保存・再利用され続けてしまう
# （shirokuma-secレビュー: WARN。confused deputy的な脆弱性）。
_SPREADSHEET_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{20,60}$")


def _extract_spreadsheet_id(value: str) -> str:
    """Google スプレッドシートのURL（`https://docs.google.com/spreadsheets/d/{ID}/edit?gid=...`）
    またはIDそのものから、スプレッドシートIDを取り出す。URL形式でなければ、入力全体を
    トリムしたものをIDそのものとみなす。

    どちらの経路で取り出した値も`_SPREADSHEET_ID_PATTERN`で検証し、実在しうるスプレッドシートID
    の形をしていなければ422を返す（`_parse_date_param`等、本ファイルの他の入力検証と同様、
    ここで直接`HTTPException`を送出する）。
    """
    stripped = value.strip()
    match = _SPREADSHEET_URL_ID_PATTERN.search(stripped)
    candidate = match.group(1) if match else stripped
    if not _SPREADSHEET_ID_PATTERN.match(candidate):
        raise HTTPException(
            status_code=422,
            detail="スプレッドシートのURLまたはIDの形式が正しくありません",
        )
    return candidate


def _pointer_to_dict(pointer: RevenueTargetSheetPointer) -> dict[str, Any]:
    return {
        "spreadsheet_id": pointer.spreadsheet_id,
        "mrr_sheet_name": pointer.mrr_sheet_name,
        "unit_count_sheet_name": pointer.unit_count_sheet_name,
    }


class RevenueTargetSheetSettingsRequest(BaseModel):
    """`POST /api/settings/revenue-target-sheet`のリクエストボディ。

    スプレッドシートURLをそのまま貼り付ける想定のUIのため、フルURL・裸のIDのどちらでも
    受け付ける（`_extract_spreadsheet_id`参照）。mrr_sheet_name／unit_count_sheet_nameは
    どちらか一方だけの運用を許容するため任意（`RevenueTargetSheetPointer`と同じ）。
    """

    spreadsheet_url_or_id: str
    mrr_sheet_name: str | None = None
    unit_count_sheet_name: str | None = None


@app.get(
    "/api/settings/revenue-target-sheet", dependencies=[Depends(verify_dashboard_api_token)]
)
def get_revenue_target_sheet_settings() -> dict[str, Any]:
    """現在設定されている事業計画スプレッドシートへのポインタを返す（未設定ならNone）。"""
    store = build_revenue_target_settings_store()
    if store is None:
        return {"configured": False, "pointer": None, "updated_at": None}

    try:
        record = store.get()
    except ApiError as exc:
        raise HTTPException(status_code=502, detail=f"notion api error: {exc}") from exc

    if record is None:
        return {"configured": False, "pointer": None, "updated_at": None}
    return {
        "configured": True,
        "pointer": _pointer_to_dict(record.pointer),
        "updated_at": record.updated_at.isoformat(),
    }


@app.post(
    "/api/settings/revenue-target-sheet", dependencies=[Depends(verify_dashboard_api_token)]
)
def save_revenue_target_sheet_settings(
    payload: RevenueTargetSheetSettingsRequest,
) -> dict[str, Any]:
    """ポインタを保存した上で、即座に`fetch_all_targets()`を1回試し、シートの形式が正しく
    読めるかを検証する（保存とテストを1リクエストにまとめ、設定画面が別途「テスト」ボタンで
    往復する必要をなくす）。

    検証（シート読み取り）が失敗しても保存自体は取り消さない。事業計画スプレッドシートは
    人手で日常的に編集されるため、保存時点では正しくても後から一時的にレイアウトが崩れる
    ケースがあり得る一方、それを理由に保存そのものを失敗させると「ポインタは合っているが
    シートが一時的に壊れている」状態を設定できなくなる（`src.reports.batch`側は目標値解決時に
    同じエラーへ環境変数フォールバックで対応するため、保存だけ先に済ませておける方が運用上
    都合が良い）。
    """
    if not payload.spreadsheet_url_or_id.strip():
        raise HTTPException(status_code=422, detail="spreadsheet_url_or_id is empty")
    spreadsheet_id = _extract_spreadsheet_id(payload.spreadsheet_url_or_id)

    try:
        store = RevenueTargetSettingsStore()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    pointer = RevenueTargetSheetPointer(
        spreadsheet_id=spreadsheet_id,
        mrr_sheet_name=payload.mrr_sheet_name or None,
        unit_count_sheet_name=payload.unit_count_sheet_name or None,
    )

    try:
        record = store.upsert(pointer)
    except ApiError as exc:
        raise HTTPException(status_code=502, detail=f"notion api error: {exc}") from exc

    validation_success = True
    validation_error: str | None = None
    # mrr_month_count/unit_count_month_countは、対応するsheet_nameが未設定の場合は必ずNoneの
    # ままにする（=「このソースでは追跡しない」）。以前は`fetch_all_targets()`を呼び、
    # 未設定側は空dict（`len()==0`）が返ってくる仕様を利用してそのままlen()を代入していたため、
    # 「未設定」と「設定済みだが0ヶ月分しか読めなかった」が両方0として区別不能になっていた
    # （BLOCKER: finding #2。`RevenueTargetSheetPointer`のdocstring・`fetch_all_targets`の
    # docstring「mrr_sheet_name／unit_count_sheet_nameが未設定の場合、対応する辞書は空のまま
    # 返す」参照）。設定されている方だけ個別に`fetch_mrr_targets`/`fetch_unit_count_targets`を
    # 呼ぶことで、Noneと0件を意味的に分離する。
    mrr_month_count: int | None = None
    unit_count_month_count: int | None = None
    try:
        if pointer.mrr_sheet_name:
            mrr_month_count = len(
                fetch_mrr_targets(pointer.spreadsheet_id, pointer.mrr_sheet_name)
            )
        if pointer.unit_count_sheet_name:
            unit_count_month_count = len(
                fetch_unit_count_targets(pointer.spreadsheet_id, pointer.unit_count_sheet_name)
            )
    except (RevenueTargetSheetFormatError, ApiError, ValueError, RuntimeError) as exc:
        # ValueError/RuntimeErrorは、Google認証情報が未設定の場合に
        # get_google_access_token()（src/document_generation/google_auth.py）が送出しうる
        # エラーも含む。RevenueTargetSheetFormatErrorはValueErrorのサブクラスだが、
        # どちらの経路で来ても「テスト結果としてエラーメッセージを表示する」という
        # 扱いは同じため個別のフォールバック処理はしない。
        validation_success = False
        validation_error = str(exc)

    return {
        "pointer": _pointer_to_dict(record.pointer),
        "updated_at": record.updated_at.isoformat(),
        "validation_success": validation_success,
        "validation_error": validation_error,
        "mrr_month_count": mrr_month_count,
        "unit_count_month_count": unit_count_month_count,
    }



