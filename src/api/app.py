"""ダッシュボード（管理画面）向けREST APIのFastAppアプリケーション本体。

社内限定・簡易認証のWebアプリ（`dashboard/`、別エージェントが並行実装中）から呼び出される
バックエンドAPI。CORSは`DASHBOARD_FRONTEND_ORIGIN`環境変数で指定したoriginのみ許可する
fail-closed設計（未設定時は一切許可しない）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from src.api.auth import verify_cron_secret, verify_dashboard_api_token
from src.api.dashboard_service import (
    build_daily_report,
    build_dashboard_summary,
    build_member_performance,
    search_projects,
)
from src.api.task_service import build_tasks
from src.document_generation.application_generator import generate_application
from src.document_generation.common import (
    ContractGenerationError,
    TemplateNotFoundError,
    TemplateSheetNotFoundError,
)
from src.document_generation.contract_generator import generate_contract
from src.document_generation.quote_generator import generate_quote
from src.reports.batch import run_report_batch
from src.sync_engine.clients.notion_client import NotionApiError
from src.sync_engine.production_wiring import ProductionSyncWiring, get_production_wiring
from src.sync_engine.webhook_handlers.kintone_webhook import handler as kintone_webhook_handler
from src.sync_engine.webhook_handlers.notion_webhook import (
    handler_with_proxy as notion_webhook_handler_with_proxy,
)
from src.sync_engine.webhook_handlers.spreadsheet_webhook import (
    handler as spreadsheet_webhook_handler,
)
from src.sync_engine.webhook_handlers.zoho_webhook import handler as zoho_webhook_handler

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
    allow_methods=["GET"],
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
    """FastAPIの`Request`を、Webhookハンドラ（Lambda形式）が期待する`event`辞書へ変換する。"""
    body = await request.body()
    return {"headers": dict(request.headers), "body": body.decode("utf-8")}


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
# 認証は各handler内部の共有シークレット検証（X-Webhook-Secretヘッダー、
# src/sync_engine/webhook_handlers/_common.pyのverify_webhook_secret）で行う。
# 実際にkintone/Zoho/Notion/スプレッドシート側でこれらのURLをWebhook購読登録する作業は、
# 本番データ移行が完了するまで意図的に行わない（登録するとここへ通知が飛び始める）。


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
    )
    return _lambda_result_to_response(result, dispatcher=wiring.dispatcher)


@app.post("/api/webhooks/kintone")
async def webhook_kintone(
    request: Request, wiring: ProductionSyncWiring = Depends(_wiring_dependency)
) -> Response:
    event = await _lambda_event_from_request(request)
    result = kintone_webhook_handler(event, context=None, dispatcher=wiring.dispatcher)
    return _lambda_result_to_response(result, dispatcher=wiring.dispatcher)


@app.post("/api/webhooks/zoho")
async def webhook_zoho(
    request: Request, wiring: ProductionSyncWiring = Depends(_wiring_dependency)
) -> Response:
    event = await _lambda_event_from_request(request)
    result = zoho_webhook_handler(event, context=None, dispatcher=wiring.dispatcher)
    return _lambda_result_to_response(result, dispatcher=wiring.dispatcher)


@app.post("/api/webhooks/spreadsheet")
async def webhook_spreadsheet(
    request: Request, wiring: ProductionSyncWiring = Depends(_wiring_dependency)
) -> Response:
    event = await _lambda_event_from_request(request)
    result = spreadsheet_webhook_handler(event, context=None, dispatcher=wiring.dispatcher)
    return _lambda_result_to_response(result, dispatcher=wiring.dispatcher)


# --- 定期実行バッチ（日報・週報） -----------------------------------------------------------


@app.get("/api/cron/daily-batch", dependencies=[Depends(verify_cron_secret)])
def run_daily_batch() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、日報・週報配信バッチのエントリポイント。

    日報は毎日、週報は金曜日のみ配信する（`src.reports.batch.run_report_batch`参照）。
    """
    return run_report_batch()


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


@app.get("/api/tasks", dependencies=[Depends(verify_dashboard_api_token)])
def get_tasks() -> dict[str, Any]:
    return build_tasks()


@app.get("/api/projects/search", dependencies=[Depends(verify_dashboard_api_token)])
def get_project_search(q: str = "") -> dict[str, Any]:
    """書類自動生成画面の案件選択UIから呼ばれる、案件名の部分一致検索。"""
    if not q.strip():
        return {"projects": [], "total_matched": 0}
    return search_projects(q)


@app.get("/api/documents/generate", dependencies=[Depends(verify_dashboard_api_token)])
def generate_document(notion_project_id: str, category: str) -> Response:
    """案件データから見積書(PDF)・申込書(Excel)・契約書(Word)を生成し、バイナリを返す。"""
    if category == "見積書":
        generator = generate_quote
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
