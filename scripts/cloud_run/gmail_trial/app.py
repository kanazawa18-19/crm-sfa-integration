"""外部業務データに触れない、隔離環境専用のログ到達・監視プローブ。"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from src.gmail_sync.watch_result import WatchRenewalProgress
from src.infrastructure.cron_result_log import WatchRenewalLog

app = FastAPI()
SERVICE = "crm-gmail-watch-trial"
NO_RERUN = "結果不明です。本文取得や確認のために業務処理を再実行しないでください。"


def emit_monitor(event, **fields):
    severity = "ERROR" if event in ("missing_finish", "monitor_error") else "INFO"
    print(json.dumps({"monitor_job": "gmail-watch-trial-monitor", "event": event,
                      "severity": severity, **fields}), flush=True)


def settings():
    project = os.environ["GCP_PROJECT_ID"]
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
        raise ValueError("検証プロジェクト指定が不正")
    if os.environ.get("TRIAL_SERVICE_NAME") != SERVICE:
        raise ValueError("検証専用サービス以外は検索不可")
    grace = int(os.environ.get("GRACE_SECONDS", "300"))
    lookback = int(os.environ.get("LOOKBACK_SECONDS", "86400"))
    if not 1 <= grace < lookback <= 604800:
        raise ValueError("検索期間が不正")
    return project, grace, lookback


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("時刻が不正")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("時刻にタイムゾーンがない")
    return parsed


def missing_runs(entries, now, grace_seconds):
    """全ページ取得後に実行IDで突合する。終了が後のページでも未終了にしない。"""
    started, finished, run_starts = {}, set(), {}
    for entry in entries:
        record = entry["jsonPayload"]
        if record.get("job") != "gmail-watch-renewal":
            raise ValueError("対象外ログ")
        run_id = record["run_id"]
        if not isinstance(run_id, str) or str(UUID(run_id)) != run_id:
            raise ValueError("実行IDが不正")
        event = record["event"]
        if event not in ("started", "finished"):
            raise ValueError("イベントが不正")
        at = timestamp(record["started_at"])
        recorded = timestamp(record["recorded_at"])
        if at > recorded or at > now + timedelta(seconds=60):
            raise ValueError("時刻の順序が不正")
        if run_id in run_starts and run_starts[run_id] != at:
            raise ValueError("開始時刻が不一致")
        run_starts[run_id] = at
        if event == "started":
            started[run_id] = at
        else:
            finished.add(run_id)
    return sorted(run_id for run_id, at in started.items()
                  if run_id not in finished and (now - at).total_seconds() >= grace_seconds)


def request_json(request, *, timeout):
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def read_entries(project, since, until, request_fn=request_json, clock=time.monotonic):
    deadline = clock() + 40

    def fetch(request):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("ログ検索の制限時間を超過")
        response = request_fn(request, timeout=min(20, remaining))
        if clock() >= deadline:
            raise TimeoutError("ログ検索の制限時間を超過")
        return response

    token = fetch(Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    ))["access_token"]
    if not isinstance(token, str) or not token:
        raise ValueError("認証応答が不正")
    query = (f'resource.type="cloud_run_revision" resource.labels.project_id="{project}" '
             f'resource.labels.service_name="{SERVICE}" '
             'jsonPayload.job="gmail-watch-renewal" '
             '(jsonPayload.event="started" OR jsonPayload.event="finished") '
             f'timestamp>="{since.isoformat()}" timestamp<="{until.isoformat()}"')
    body = {"resourceNames": [f"projects/{project}/locations/global/buckets/crm-gmail-watch-trial/views/_AllLogs"], "filter": query,
            "pageSize": 1000, "orderBy": "timestamp asc"}
    entries, seen = [], set()
    for _ in range(100):
        page = fetch(Request(
            "https://logging.googleapis.com/v2/entries:list",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        ))
        if not isinstance(page, dict) or not isinstance(page.get("entries", []), list):
            raise ValueError("検索応答が不正")
        if len(entries) + len(page.get("entries", [])) > 20000:
            raise ValueError("検索件数上限")
        entries.extend(page.get("entries", []))
        next_token = page.get("nextPageToken", "")
        if not isinstance(next_token, str):
            raise ValueError("ページ指定が不正")
        if not next_token:
            return entries
        if next_token in seen:
            raise ValueError("検索ページが循環")
        seen.add(next_token)
        body["pageToken"] = next_token
    raise ValueError("検索ページ上限")


@app.get("/api/healthz")
def health():
    return {"ok": True, "trial_only": True}


@app.post("/probe/{scenario}")
def probe(scenario: Literal["success", "partial_failure", "http500", "unfinished"]):
    log = WatchRenewalLog()
    log.emit("started")
    failed = scenario == "partial_failure"
    log.update(WatchRenewalProgress(total=2 if failed else 1, attempted=2 if failed else 1,
                                   renewed=1, failed=int(failed)))
    if scenario == "unfinished":
        return {"run_id": log.run_id, "trial_only": True, "message": NO_RERUN}
    record = log.emit("finished", status=log.progress.outcome(), completed=True)
    return JSONResponse(record, status_code=500 if scenario == "http500" else 200)


@app.get("/check")
def check():
    stage = "configuration"
    try:
        project, grace, lookback = settings()
        now = datetime.now(timezone.utc)
        stage = "retrieval"
        entries = read_entries(project, now - timedelta(seconds=lookback), now)
        stage = "matching"
        missing = missing_runs(entries, now, grace)
        for run_id in missing:
            emit_monitor("missing_finish", run_id=run_id, message=NO_RERUN)
        emit_monitor("check_completed", missing_count=len(missing), entry_count=len(entries))
        return {"ok": True, "missing_count": len(missing), "entry_count": len(entries),
                "message": NO_RERUN if missing else ("照合完了" if entries else "ログ0件。到達は未確認です。")}
    except Exception as error:
        error_kind = "other"
        details = {}
        if isinstance(error, HTTPError):
            error_kind = "http"
            if type(error.code) is int and 100 <= error.code <= 599:
                details["http_status"] = error.code
        elif isinstance(error, TimeoutError) or (isinstance(error, URLError) and isinstance(error.reason, TimeoutError)):
            error_kind = "timeout"
        elif isinstance(error, (ValueError, KeyError, TypeError, AttributeError)):
            error_kind = "invalid"
        # 認証応答・検索エラー本文・不正なログ本文は返さない。
        emit_monitor("monitor_error", stage=stage, error_kind=error_kind, **details)
        return JSONResponse({"ok": False, "error": "monitor_error", "stage": stage}, status_code=500)
