"""有効時だけ対象経路をDB依存解決より前に受け取るASGI入口。"""

from __future__ import annotations

import json
import os
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.auth import verify_cron_secret
from src.sync_capacity.domain import MAX_PAYLOAD_BYTES, PayloadConflict, submission
from src.sync_capacity.firestore_store import enabled, get_store
from src.sync_engine.webhook_handlers._common import (
    get_header, verify_notion_webhook_signature, verify_webhook_body_token,
    verify_webhook_query_param, verify_webhook_secret,
)

TARGETS = {f"/api/webhooks/{source}": ("POST", source)
           for source in ("notion", "kintone", "zoho", "spreadsheet")}
TARGETS["/api/cron/spreadsheet-outbox-drain"] = ("GET", "spreadsheet-outbox-drain")
router = APIRouter()


def authenticate(source: str, request: Request, body: str, payload: dict) -> None:
    if source == "spreadsheet-outbox-drain":
        verify_cron_secret(request.headers.get("authorization"),
                           request.headers.get("x-cron-secret"),
                           request.headers.get("x-cloud-scheduler"))
        return
    # 永続キューではローカル用の署名省略フラグも認めない。
    if not os.environ.get(f"{source.upper()}_WEBHOOK_SECRET"):
        raise HTTPException(503, "webhook authentication not configured")
    if source == "notion":
        valid = verify_notion_webhook_signature(request.headers, body)
    elif source == "kintone":
        valid = verify_webhook_query_param(request.query_params, param_name="secret",
                                          env_var="KINTONE_WEBHOOK_SECRET")
    elif source == "zoho":
        valid = verify_webhook_body_token(payload, token_field="token", env_var="ZOHO_WEBHOOK_SECRET")
    else:
        valid = verify_webhook_secret(request.headers, "SPREADSHEET_WEBHOOK_SECRET")
    if not valid:
        raise HTTPException(401, "unauthorized")


class CapacityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        target = TARGETS.get(scope.get("path", ""))
        if (scope["type"] != "http" or target is None
                or scope.get("method") != target[0]):
            await self.app(scope, receive, send)
            return
        try:
            active = enabled()
        except Exception:
            await JSONResponse({"error": "invalid queue flag"}, status_code=503)(scope, receive, send)
            return
        if not active:
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_PAYLOAD_BYTES:
                    raise HTTPException(413, "payload too large")
            try:
                body = raw.decode("utf-8")
                payload = json.loads(body) if body else {}
            except (ValueError, UnicodeError):
                raise HTTPException(400, "invalid JSON") from None
            if not isinstance(payload, dict):
                raise HTTPException(400, "request body must be an object")
            source = target[1]
            authenticate(source, request, body, payload)
            now = time.time()
            if source == "spreadsheet-outbox-drain":
                # 定期実行は同じbodyでも別の仕事。HTTP再送が重なっても共通枠内で処理する。
                payload = {"invocation_id": uuid.uuid4().hex}
            item = submission(source, payload, get_header(request.headers, "X-Sync-System-ID"),
                              now, receipt_id=uuid.uuid4().hex)
            def save():
                return get_store().enqueue(item, now)
            state = await run_in_threadpool(save)
            response = JSONResponse({"accepted": True, "job_id": item.job_id, "state": state})
        except HTTPException as exc:
            response = JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        except PayloadConflict:
            response = JSONResponse({"error": "event ID content conflict"}, status_code=409)
        except Exception:
            # SDK例外に接続先・認証情報が混ざるため本文も例外も記録しない。
            response = JSONResponse({"error": "queue unavailable; request not acknowledged"}, status_code=503)
        await response(scope, receive, send)


@router.get("/api/cron/sync-capacity-drain", dependencies=[Depends(verify_cron_secret)])
def capacity_drain():
    from src.sync_capacity.worker import run_worker
    try:
        if not enabled():
            raise RuntimeError("sync capacity queue disabled")
        return run_worker()
    except Exception:
        raise HTTPException(503, "worker outcome unconfirmed; inspect retained jobs") from None
