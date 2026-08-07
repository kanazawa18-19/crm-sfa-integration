"""ダッシュボード（管理画面）向けREST APIのFastAppアプリケーション本体。

社内限定・簡易認証のWebアプリ（`dashboard/`、別エージェントが並行実装中）から呼び出される
バックエンドAPI。CORSは`DASHBOARD_FRONTEND_ORIGIN`環境変数で指定したoriginのみ許可する
fail-closed設計（未設定時は一切許可しない）。
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

from src.api.auth import verify_dashboard_api_token
from src.api.dashboard_service import (
    build_daily_report,
    build_dashboard_summary,
    build_member_performance,
    search_projects,
)
from src.document_generation.application_generator import generate_application
from src.document_generation.common import ContractGenerationError, TemplateNotFoundError
from src.document_generation.contract_generator import generate_contract
from src.document_generation.quote_generator import generate_quote
from src.sync_engine.clients.notion_client import NotionApiError

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
