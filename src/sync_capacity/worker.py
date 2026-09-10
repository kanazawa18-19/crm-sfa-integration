"""既存同期ハンドラの呼出し。認証済みジョブだけを内部引数で渡す。"""

from __future__ import annotations

import json
import logging
import threading

from src.sync_capacity.telemetry import emit
from src.sync_capacity.deadline import Deadline, WORKER_SECONDS, current_deadline
from src.sync_capacity.application import drain_one
from src.sync_capacity.domain import Claim, SAFE_SKIPS
from src.sync_capacity.firestore_store import get_store

# 同じプロセスのsingleton dispatcherに同時に触れない。全配備の上限はFirestoreが担当。
_WORKER_LOCK = threading.Lock()


class ObservedDispatcher:
    def __init__(self, dispatcher, *, source: str = ""):
        self.source = source
        self.dispatcher = dispatcher
        self.partial = False

    def dispatch(self, event):
        result = self.dispatcher.dispatch(event)
        if (result.has_partial_skips
                or (result.skipped and (result.reason not in SAFE_SKIPS
                    or (self.source == "notion" and result.reason == "stale_event")))):
            self.partial = True
        return result


def prepare(claim: Claim):
    if claim.source == "spreadsheet-outbox-drain":
        from src.sync_engine.spreadsheet_outbox_drain import (
            drain_spreadsheet_outbox, DEFAULT_BUDGET_SECONDS)
        def execute_outbox():
            deadline = current_deadline.get()
            budget = min(DEFAULT_BUDGET_SECONDS, deadline.require()) if deadline else DEFAULT_BUDGET_SECONDS
            result = drain_spreadsheet_outbox(budget_seconds=budget)
            partial = result.get("status") != "success" or bool(result.get("gave_up"))
            return {"statusCode": 200, "body": json.dumps({})}, partial
        return execute_outbox

    from src.sync_engine.production_wiring import get_production_wiring
    from src.sync_engine.webhook_handlers import (
        kintone_webhook, notion_webhook, spreadsheet_webhook, zoho_webhook,
    )
    wiring = get_production_wiring()
    observed = ObservedDispatcher(wiring.dispatcher, source=claim.source)
    if claim.source == "notion" and wiring.any_db_page_client is None:
        raise RuntimeError("notion sync is not configured")

    def execute():
        from src.sync_engine.webhook_receipts import record_webhook_receipt
        record_webhook_receipt(claim.source)
        kwargs = {"context": None, "dispatcher": observed, "trusted_queue": True}
        if claim.source == "notion":
            result = notion_webhook.handler_with_proxy(
                claim.event, **kwargs, notion_client=wiring.any_db_page_client,
                calendar_sync=wiring.calendar_sync_callable, lead_sync=wiring.lead_sync_callable,
                project_mirror_sync=wiring.project_mirror_sync_callable,
                client_name_index_sync=wiring.client_name_index_sync_callable)
        elif claim.source == "spreadsheet":
            result = spreadsheet_webhook.handler(claim.event, **kwargs)
        elif claim.source in {"kintone", "zoho"}:
            kwargs.update(id_mapping_store=wiring.id_mapping_store,
                          notion_client=wiring.any_db_page_client)
            handler = kintone_webhook.handler
            if claim.source == "zoho":
                handler = zoho_webhook.handler
                kwargs["zoho_client"] = wiring.zoho_action_client
            result = handler(claim.event, **kwargs)
        else:
            raise ValueError("unknown job source")
        return result, observed.partial
    return execute


def run_worker():
    deadline = Deadline.after(WORKER_SECONDS)
    # ローカル待ち行列でHTTP実行枠を消費しない。次回の定期drainで拾う。
    if not _WORKER_LOCK.acquire(blocking=False):
        emit("local_worker_busy")
        return {"state": "local_worker_busy"}
    try:
        try:
            store = get_store()
        except Exception:
            emit("worker_store_unavailable", level=logging.WARNING)
            raise
        return drain_one(store, prepare, deadline=deadline)
    finally:
        _WORKER_LOCK.release()
