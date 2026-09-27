"""Webhook監視用の読み取り観測。業務データ・購読・通知状態は変更しない。"""
from __future__ import annotations

import os
import time
from urllib.parse import urlencode
from datetime import datetime, timezone
from typing import Any

import psycopg
import requests
from psycopg.rows import dict_row

from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.clients.zoho_client import HttpZohoClient
from src.sync_engine.zoho_watch_channel import DEFAULT_MODULES, DEFAULT_WATCH_API_BASE_URL

SOURCES = ("notion", "zoho", "kintone", "spreadsheet")
TIMEOUT = 5


def utc(value: datetime | str | None) -> datetime | None:
    """DBのタイムゾーンなし日時はUTC。外部の不正な日時は監視不能にする。"""
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def stamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def check(key: str, status: str, reason: str, **evidence: Any) -> dict[str, Any]:
    return {"key": key, "status": status, "reason": reason, **evidence}


def evaluate_watch(body: Any, *, channel_id: str, callback: str, now: datetime) -> dict[str, Any]:
    """購読一覧を評価する純粋関数。HTTP失敗と購読消失を混同しない。"""
    key = "zoho_subscription"
    if not isinstance(body, dict) or not isinstance(body.get("watch"), list):
        return check(key, "unknown", "購読APIの応答形式を確認できません")
    info = body.get("info", {})
    if not isinstance(info, dict):
        return check(key, "unknown", "購読APIの応答形式を確認できません")
    if info.get("more_records"):
        return check(key, "unknown", "購読一覧が全件取得できていません")
    entries = body["watch"]
    if any(not isinstance(e, dict) for e in entries):
        return check(key, "unknown", "購読APIの応答形式を確認できません")
    entries = [e for e in entries if str(e.get("channel_id")) == channel_id]
    if not entries:
        return check(key, "critical", "対象の通知購読がありません")
    required = {f"{module}.{event}" for module in DEFAULT_MODULES for event in ("create", "edit", "delete")}
    covered: set[str] = set()
    expiries = []
    try:
        for entry in entries:
            if entry.get("notify_url") != callback:
                return check(key, "critical", "通知先が本プロジェクトの受信URLと一致しません")
            expiry = utc(entry.get("channel_expiry"))
            if not expiry or not isinstance(entry.get("events"), list):
                return check(key, "unknown", "購読期限または対象イベントを確認できません")
            expiries.append(expiry)
            covered.update(entry["events"])
        if not required.issubset(covered):
            return check(key, "critical", "必要なモジュールまたは操作の購読が不足しています")
        expiry = min(expiries)
        hours = (expiry - now).total_seconds() / 3600
        if hours <= 0:
            return check(key, "critical", "通知購読の期限が切れています", expires_at=stamp(expiry))
        if hours <= 6:
            return check(key, "warning", "通知購読の期限まで6時間以内です", expires_at=stamp(expiry))
        return check(key, "ok", "6モジュールの購読・通知先・期限を確認", expires_at=stamp(expiry))
    except (TypeError, ValueError):
        return check(key, "unknown", "購読APIの値を確認できません")


def evaluate_delivery(source: str, receipt: datetime | None, activity: datetime | None,
                      *, now: datetime, complete: bool = True) -> dict[str, Any]:
    """変更なしは異常にしない。連続未着の確定・通知抑制は監視側が担当する。"""
    evidence = {"last_received_at": stamp(receipt), "activity_at": stamp(activity)}
    key = source + "_delivery"
    if any(t and t > now for t in (receipt, activity)):
        return check(key, "unknown", "観測時刻が未来のため判定できません", **evidence)
    if not complete:
        return check(key, "unknown", "変更履歴の取得に失敗しました", **evidence)
    if activity and (not receipt or activity > receipt):
        return check(key, "pending", "変更後の受信を待っています", **evidence)
    if not receipt:
        return check(key, "unverified", "受信記録がなく、生死は未確認です", **evidence)
    return check(key, "ok", "受信記録あり・最終受信後の変更は未検出", **evidence)


def _read_database() -> tuple[dict, datetime | None]:
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=TIMEOUT, row_factory=dict_row) as conn:
        conn.read_only = True
        conn.execute("SET LOCAL statement_timeout = '5000ms'")
        conn.execute("SET LOCAL TIME ZONE 'UTC'")
        rows = conn.execute('SELECT source, "lastReceivedAt", "receiptCount" FROM "WebhookReceipt"').fetchall()
        receipts = {r["source"]: {"last": utc(r["lastReceivedAt"]), "count": int(r["receiptCount"])} for r in rows}
        # 最初の未着変更を使う。編集が続くたびに猶予が延びることを避ける。
        last = receipts.get("notion", {}).get("last")
        first = conn.execute('''SELECT min("createdAt") AS first FROM "AuditLog"
            WHERE "createdAt" > coalesce(%s::timestamp, (now() AT TIME ZONE 'UTC') - interval '7 days')''',
                            (last.replace(tzinfo=None) if last else None,)).fetchone()["first"]
        return receipts, utc(first)


def _notion_activity() -> list[datetime]:
    result = []
    headers = {"Authorization": "Bearer " + os.environ["NOTION_API_KEY"], "Notion-Version": "2022-06-28"}
    for schema in ALL_SCHEMAS:
        response = requests.post(f"https://api.notion.com/v1/databases/{schema.notion_database_id}/query",
                                 headers=headers, json={"page_size": 1, "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}]},
                                 timeout=TIMEOUT)
        response.raise_for_status()
        pages = response.json()["results"]
        for page in pages:
            result.append(utc(page["last_edited_time"]))
    return result


def _kintone_activity() -> list[datetime]:
    result = []
    for suffix in ("CLIENT", "PROJECT", "ACTION"):
        response = requests.get(f"https://{os.environ['KINTONE_DOMAIN']}/k/v1/records.json",
                                headers={"X-Cybozu-API-Token": os.environ[f"KINTONE_API_TOKEN_{suffix}"]},
                                params={"app": os.environ[f"KINTONE_APP_ID_{suffix}"],
                                        "query": "order by 更新日時 desc limit 1", "fields[0]": "更新日時"}, timeout=TIMEOUT)
        response.raise_for_status()
        for record in response.json()["records"]:
            result.append(utc(record["更新日時"]["value"]))
    return result


def _read_watch(now: datetime) -> dict:
    try:
        channel = os.environ["ZOHO_WATCH_CHANNEL_ID"]
        base = os.environ["ZOHO_WEBHOOK_BASE_URL"].rstrip("/")
        client = HttpZohoClient(accounts_base_url=os.environ.get("ZOHO_ACCOUNTS_BASE_URL", "https://accounts.zoho.jp"),
                               timeout=TIMEOUT, max_retries=0)
        response = client.request("GET", DEFAULT_WATCH_API_BASE_URL + "/actions/watch?" + urlencode({"channel_id": channel}),
                                  idempotent=True)
        if response.status_code == 204:
            return check("zoho_subscription", "critical", "対象の通知購読がありません")
        if response.status_code != 200:
            return check("zoho_subscription", "unknown", "購読APIの照会に失敗しました")
        return evaluate_watch(response.json(), channel_id=channel, callback=base + "/api/webhooks/zoho", now=now)
    except Exception:
        # HTTP例外・応答本文・URLには秘密値を含みうるため、外へ返さない。
        return check("zoho_subscription", "unknown", "購読APIの照会に失敗しました（設定・認証・接続を確認）")


def collect_webhook_health() -> dict:
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    checks = []
    try:
        receipts, first_write = _read_database()
        checks.append(check("receipt_store", "ok", "受信記録を取得しました"))
    except Exception:
        receipts, first_write = {}, None
        checks.append(check("receipt_store", "unknown", "受信記録を取得できません（設定・接続を確認）"))
    db_ok = checks[0]["status"] == "ok"
    for source, reader in (("notion", _notion_activity), ("kintone", _kintone_activity)):
        last = receipts.get(source, {}).get("last")
        try:
            activities = reader()
            if source == "notion" and first_write:
                activities.append(first_write)
            newer = [a for a in activities if a and (not last or a > last)]
            activity = min(newer) if newer else None
            checks.append(evaluate_delivery(source, last, activity, now=datetime.now(timezone.utc), complete=db_ok))
        except Exception:
            checks.append(evaluate_delivery(source, last, None, now=now, complete=False))
    checks.append(_read_watch(datetime.now(timezone.utc)))
    for source in ("zoho", "spreadsheet"):
        last = receipts.get(source, {}).get("last")
        result = evaluate_delivery(source, last, None, now=datetime.now(timezone.utc), complete=db_ok)
        if result["status"] == "ok":
            result["reason"] = "受信記録あり・変更有無は未確認です"
        checks.append(result)
    return {"schema_version": 1, "observed_at": stamp(datetime.now(timezone.utc)),
            "elapsed_ms": round((time.monotonic() - started) * 1000), "checks": checks,
            "receipts": {s: {"last_received_at": stamp(receipts.get(s, {}).get("last")),
                             "count": receipts.get(s, {}).get("count")} for s in SOURCES}}
