"""ダッシュボード（管理画面）向けREST APIのFastAppアプリケーション本体。

社内限定・簡易認証のWebアプリ（`dashboard/`、別エージェントが並行実装中）から呼び出される
バックエンドAPI。CORSは`DASHBOARD_FRONTEND_ORIGIN`環境変数で指定したoriginのみ許可する
fail-closed設計（未設定時は一切許可しない）。
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from src.api.auth import verify_dashboard_api_token
from src.api.dashboard_service import (
    build_daily_report,
    build_dashboard_summary,
    build_member_performance,
)

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
