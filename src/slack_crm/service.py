"""Slack操作台帳からNotion正本へ変更を反映する。"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from src.audit_log.actor_context import set_actor
from src.audit_log.recorder import record_notion_write
from src.sync_engine.record_sync_lock import _connect_direct, lock_key, RecordSyncBusy
from src.db_schema.action import ACTION_SCHEMA
from src.db_schema.project import PROJECT_SCHEMA
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.db_schema.base import Tool
from src.sync_engine.sync_event import SyncEvent
from src.slack_crm import storage
from src.slack_crm.domain import validate_changes

logger = logging.getLogger(__name__)


@contextmanager
def _target_lock(kind: str, target_id: str):
    """既存の同期と同じ鍵を、検証済みの直接接続で保持する。"""
    key = lock_key("project" if kind in {"project_update", "action_create"} else "action", target_id)
    conn = _connect_direct()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS locked", (key,))
            if not cur.fetchone()["locked"]:
                raise RecordSyncBusy("同じ対象を別の同期が処理中です")
        yield
    finally:
        conn.close()


def _client(kind: str) -> HttpNotionClient:
    schema = PROJECT_SCHEMA if kind == "project_update" else ACTION_SCHEMA
    return HttpNotionClient(schema.key, schema.notion_database_id)


def _state_for_update(client: HttpNotionClient, operation: dict[str, Any]) -> tuple[str, str | None]:
    current = client.get_page(operation["targetId"])
    if current is None:
        return "failed", "not_found"
    changes = validate_changes(operation["kind"], operation["changes"])
    expected = operation["expected"]
    if not set(changes) <= set(expected):
        return "failed", "invalid_expected"
    if any(current.get(key) != value for key, value in expected.items() if key not in changes):
        return "conflict", "relation_changed"
    if all(current.get(key) == value for key, value in changes.items()):
        return "done", None
    if any(current.get(key) != expected[key] for key in changes):
        return "conflict", "value_changed"
    try:
        client.update_page(operation["targetId"], changes)
    except Exception:
        # PATCHの応答不明では再送せず、現在値を再読して結果を確かめる。
        latest = client.get_page(operation["targetId"])
        if latest and all(latest.get(key) == value for key, value in changes.items()):
            return "done", None
        return "unknown", "update_response_unknown"
    return "done", None


def _state_for_create(client: HttpNotionClient, operation: dict[str, Any]) -> tuple[str, str | None, str | None]:
    project = _client("project_update").get_page(operation["targetId"])
    if project is None:
        return "failed", "project_not_found", None
    changes = validate_changes("action_create", operation["changes"])
    changes["案件名"] = [operation["targetId"]]
    # 非冪等の作成は1度だけ。応答不明の場合は人が照合する。
    try:
        page_id = client.create_page_once(changes)
    except Exception:
        return "unknown", "create_response_unknown", None
    record_notion_write(db_key=ACTION_SCHEMA.key, notion_page_id=page_id, action="create",
                        before=None, after=changes)
    return "done", None, page_id


def process_one(operation_id: str | None = None, *, notify: bool = True) -> dict[str, Any] | None:
    operation = storage.claim(operation_id)
    if operation is None:
        return None
    state, error_code, page_id = "failed", "unexpected_error", None
    try:
        with _target_lock(operation["kind"], operation["targetId"]), set_actor("slack_crm", label=operation["actorId"]):
            client = _client(operation["kind"])
            if operation["kind"] == "action_create":
                state, error_code, page_id = _state_for_create(client, operation)
            else:
                state, error_code = _state_for_update(client, operation)
    except RecordSyncBusy:
        if operation["attempts"] >= 12:
            storage.finish(operation["id"], state="failed", error_code="record_busy")
            if notify:
                notify_pending(operation["id"])
            return {"id": operation["id"], "state": "failed", "error_code": "record_busy"}
        storage.requeue(operation["id"])
        return {"id": operation["id"], "state": "queued"}
    except Exception:
        logger.exception("Slack CRM操作の処理に失敗: id=%s", operation["id"])
    storage.finish(operation["id"], state=state, error_code=error_code, result_page_id=page_id)
    result = {"id": operation["id"], "state": state, "error_code": error_code, "page_id": page_id}
    if notify:
        if state == "done":
            process_sync(operation["id"])
        notify_pending(operation["id"])
    return result


def process_sync(operation_id: str | None = None) -> dict[str, str] | None:
    """Notionボット自身のWebhookは除外されるため、保存済みの差分を既存同期へ渡す。"""
    operation = storage.claim_sync(operation_id)
    if operation is None:
        return None
    page_id = operation["resultPageId"] or operation["targetId"]
    db_key = "project" if operation["kind"] == "project_update" else "action"
    try:
        from src.sync_engine.production_wiring import get_production_wiring
        wiring = get_production_wiring()
        client = _client(operation["kind"])
        current = client.get_page(page_id)
        if current is None:
            raise ValueError("Notion page missing")
        if any(current.get(key) != value for key, value in operation["changes"].items()):
            storage.finish_sync(operation["id"], state="partial", error="notion_value_changed")
            return {"id": operation["id"], "state": "partial"}
        raw = client.get_raw_page(page_id)
        occurred_raw = raw.get("last_edited_time") if raw else None
        occurred_at = (datetime.fromisoformat(occurred_raw.replace("Z", "+00:00"))
                       if occurred_raw else datetime.now(timezone.utc))
        event = SyncEvent(source_tool=Tool.NOTION, db_key=db_key, external_id=page_id,
                          occurred_at=occurred_at, properties=dict(operation["changes"]))
        if db_key == "project":
            if wiring.project_mirror_sync_callable is not None:
                wiring.project_mirror_sync_callable(event.properties, page_id)
            if "次回アクション日" in event.properties and wiring.calendar_sync_callable is not None:
                wiring.calendar_sync_callable(event.properties, page_id)
        result = wiring.dispatcher.dispatch(event)
        if result.skipped:
            state = "unmapped" if result.reason == "unknown_record" else "partial"
            storage.finish_sync(operation["id"], state=state, error=result.reason)
            return {"id": operation["id"], "state": state}
        if result.has_partial_skips:
            # 相手先の版競合も含まれる。自動再配送で新しい編集を上書きしない。
            state = "partial"
            storage.finish_sync(operation["id"], state=state, error="partial_skip")
            return {"id": operation["id"], "state": state}
        state = "done"
        storage.finish_sync(operation["id"], state=state)
        return {"id": operation["id"], "state": state}
    except RecordSyncBusy:
        storage.finish_sync(operation["id"], state="queued", error="record_busy")
        return {"id": operation["id"], "state": "queued"}
    except Exception:
        logger.exception("Slack CRM同期に失敗: id=%s", operation["id"])
        state = "failed" if operation["syncAttempts"] >= 3 else "queued"
        storage.finish_sync(operation["id"], state=state, error="sync_failed")
        return {"id": operation["id"], "state": state}


def notify_pending(operation_id: str | None = None, *, limit: int = 10) -> int:
    from src.slack_crm.slack_api import send_result
    sent = 0
    for _ in range(limit):
        operation = storage.claim_notification(operation_id)
        if operation is None:
            break
        try:
            send_result(operation["actorId"], operation, {
                "id": operation["id"], "state": operation["state"],
                "error_code": operation["errorCode"], "page_id": operation["resultPageId"],
            })
        except Exception:
            logger.exception("Slack CRM結果通知に失敗: id=%s", operation["id"])
            storage.finish_notification(operation["id"], success=False)
        else:
            storage.finish_notification(operation["id"], success=True)
            sent += 1
        if operation_id:
            break
    return sent


def drain(*, limit: int = 10) -> dict[str, int]:
    recovered = storage.recover_stale()
    processed = 0
    for _ in range(limit):
        if process_one(notify=False) is None:
            break
        processed += 1
    synced = 0
    for _ in range(limit):
        if process_sync() is None:
            break
        synced += 1
    notified = notify_pending(limit=limit)
    return {"recovered": recovered, "processed": processed, "synced": synced, "notified": notified}
