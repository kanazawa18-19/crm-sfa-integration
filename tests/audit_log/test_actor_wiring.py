"""各Webhookハンドラ/同期エントリポイントが、実際にNotionへ書き込む瞬間
（`create_page`/`update_page`が呼ばれる時点）に`set_actor()`で正しい`actorSource`を
設定していることを検証する（`src/audit_log/actor_context.py`参照）。

`HttpNotionClient`自体を経由する部分（差分抽出・DB書き込み）は
`tests/sync_engine/clients/test_notion_client_audit_log.py`/`tests/audit_log/test_recorder.py`
で別途検証済みのため、ここではフェイククライアントの`create_page`/`update_page`内で
`get_actor()`を記録し、各エントリポイントのコンテキスト設定のみを検証する。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import pytest

from src.audit_log.actor_context import Actor, get_actor
from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.sync_targets.base import SyncTarget
from src.sync_engine.webhook_handlers._common import WEBHOOK_SECRET_HEADER
from src.sync_engine.webhook_handlers.kintone_webhook import handler as kintone_handler
from src.sync_engine.webhook_handlers.lead_inquiry_webhook import handler as lead_inquiry_handler
from src.sync_engine.webhook_handlers.slack_interaction_webhook import handler as slack_handler
from src.sync_engine.webhook_handlers.spreadsheet_webhook import handler as spreadsheet_handler
from src.sync_engine.webhook_handlers.web_engagement_webhook import handler as web_engagement_handler
from src.sync_engine.webhook_handlers.zoho_webhook import handler as zoho_handler


class _ActorCapturingNotionTarget(SyncTarget):
    """`Dispatcher`経由のテスト用。upsert_record()呼び出し時点のactorを記録する。"""

    tool = Tool.NOTION

    def __init__(self) -> None:
        self.captured_actors: list[Actor] = []

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        return None

    def upsert_record(
        self,
        external_id: str | None,
        properties: dict[str, Any],
        *,
        db_key: str | None = None,
        expected_version: str | None = None,
    ) -> str:
        self.captured_actors.append(get_actor())
        return external_id or "new-page"

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        pass


# --- kintone_webhook/zoho_webhook/spreadsheet_webhook（Dispatcher経由） -------------------


def test_kintone_webhook_dispatches_with_kintone_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
        lambda: {"123": "project"},
    )
    store = SQLiteIdMappingStore(":memory:")
    store.upsert(IdMapping(notion_key="notion-1", db_key="project", kintone_id="45"))
    notion_target = _ActorCapturingNotionTarget()
    dispatcher = Dispatcher(store, {Tool.NOTION: notion_target})
    payload = {
        "type": "record.updated",
        "app": {"id": "123"},
        "record": {
            "$id": {"type": "__ID__", "value": "45"},
            "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
            "更新者": {"type": "MODIFIER", "value": {"code": "user1", "name": "山田太郎"}},
            "ドロップダウン_2": {"type": "DROP_DOWN", "value": "商談中（B）"},
        },
    }
    event = {"body": json.dumps(payload), "headers": {}, "query_params": {}}

    kintone_handler(event, context=None, dispatcher=dispatcher)
    store.close()

    assert notion_target.captured_actors
    assert all(
        actor == Actor(source="kintone_webhook", label="山田太郎")
        for actor in notion_target.captured_actors
    )


def test_kintone_actor_label_falls_back_to_code_when_name_missing() -> None:
    from src.sync_engine.webhook_handlers.kintone_webhook import _kintone_actor_label

    assert _kintone_actor_label({"更新者": {"type": "MODIFIER", "value": {"code": "user1"}}}) == "user1"


def test_kintone_actor_label_falls_back_to_creator_when_modifier_missing() -> None:
    from src.sync_engine.webhook_handlers.kintone_webhook import _kintone_actor_label

    assert (
        _kintone_actor_label({"作成者": {"type": "CREATOR", "value": {"code": "user2", "name": "鈴木"}}})
        == "鈴木"
    )


def test_kintone_actor_label_returns_none_when_absent() -> None:
    from src.sync_engine.webhook_handlers.kintone_webhook import _kintone_actor_label

    assert _kintone_actor_label({}) is None


class _ActorCapturingSpyDispatcher:
    """`tests/sync_engine/webhook_handlers/test_zoho_webhook.py`の`SpyDispatcher`と同じ、
    dispatchが呼ばれたことだけ（ここではdispatch呼び出し時点のactor）を記録するスタブ。
    """

    def __init__(self) -> None:
        self.captured_actors: list[Actor] = []

    def dispatch(self, event: object) -> None:
        self.captured_actors.append(get_actor())


def test_zoho_webhook_dispatches_with_zoho_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
        lambda: {"Deals": "project"},
    )
    dispatcher = _ActorCapturingSpyDispatcher()
    payload = {
        "server_time": 1754960400000,
        "affected_values": [{"record_id": "4876876000000488001", "values": {"Stage": "商談中(B)"}}],
        "query_params": {},
        "module": "Deals",
        "resource_uri": "https://www.zohoapis.com/crm/v8/Deals",
        "ids": ["4876876000000488001"],
        "operation": "update",
        "channel_id": "1000000068001",
        "token": "correct-secret",
    }
    event = {"body": json.dumps(payload), "headers": {}, "query_params": {}}
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")

    zoho_handler(event, context=None, dispatcher=dispatcher)

    assert dispatcher.captured_actors
    assert all(actor.source == "zoho_webhook" for actor in dispatcher.captured_actors)


def test_spreadsheet_webhook_dispatches_with_spreadsheet_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.spreadsheet_webhook._default_sheet_to_db_key",
        lambda: {"案件管理": "project"},
    )
    store = SQLiteIdMappingStore(":memory:")
    store.upsert(IdMapping(notion_key="notion-1", db_key="project", spreadsheet_row=42))
    notion_target = _ActorCapturingNotionTarget()
    dispatcher = Dispatcher(store, {Tool.NOTION: notion_target})
    payload = {
        "sheet": "案件管理",
        "row": 42,
        "editedAt": "2026-08-05T09:00:00+09:00",
        "values": {"案件ID": "MSA-PJ-001"},
    }
    event = {"body": json.dumps(payload), "headers": {}, "query_params": {}}

    spreadsheet_handler(event, context=None, dispatcher=dispatcher)
    store.close()

    assert notion_target.captured_actors
    assert all(actor.source == "spreadsheet_webhook" for actor in notion_target.captured_actors)


# --- web_engagement_webhook/lead_inquiry_webhook（HttpNotionClient直接呼び出し） -----------


class _ActorCapturingContactClient:
    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        self.pages = pages or []
        self.captured_actors: list[Actor] = []

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.pages

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        return None

    def create_page(self, properties: dict[str, Any]) -> str:
        self.captured_actors.append(get_actor())
        return "new-page-1"

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
        self.captured_actors.append(get_actor())


def test_web_engagement_webhook_uses_web_engagement_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_ENGAGEMENT_WEBHOOK_SECRET", "correct-secret")
    client = _ActorCapturingContactClient()
    payload = {"event_type": "hot_lead", "email": "yamada@example.com", "score": 82}
    event = {
        "body": json.dumps(payload),
        "headers": {WEBHOOK_SECRET_HEADER: "correct-secret"},
    }

    web_engagement_handler(event, context=None, notion_client=client)

    assert client.captured_actors == [Actor(source="web_engagement_webhook", label=None)]


def test_lead_inquiry_webhook_uses_lead_inquiry_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEAD_RESEARCHER_WEBHOOK_SECRET", "correct-secret")
    contact_client = _ActorCapturingContactClient()
    client_master_client = _ActorCapturingContactClient()
    payload = {"company": "", "name": "山田太郎", "email": "yamada@example.com", "phone": ""}
    event = {
        "body": json.dumps(payload),
        "headers": {WEBHOOK_SECRET_HEADER: "correct-secret"},
    }

    lead_inquiry_handler(
        event,
        context=None,
        contact_client=contact_client,
        client_master_client=client_master_client,
    )

    assert contact_client.captured_actors == [Actor(source="lead_inquiry_webhook", label=None)]


# --- slack_interaction_webhook（meeting_sync.slack_approval.handle_interaction経由） -------

_SIGNING_SECRET = "test-signing-secret"


def _signed_event(body: str) -> dict[str, Any]:
    ts = int(time.time())
    basestring = f"v0:{ts}:{body}".encode()
    signature = "v0=" + hmac.new(_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    return {
        "body": body,
        "headers": {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": signature},
    }


def test_slack_interaction_webhook_uses_slack_interaction_actor(
    monkeypatch: pytest.MonkeyPatch, requests_mock
) -> None:
    from src.meeting_sync.slack_approval import APPROVE_ACTION_ID, MeetingCandidate

    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SIGNING_SECRET)
    requests_mock.post("https://hooks.slack.com/actions/T000/000/xxx", json={})

    candidate = MeetingCandidate(
        event_id="event-1",
        title="【商談（訪問）】〇〇ホテル様",
        action_type="訪問商談",
        action_date="2026-08-12",
        project_page_id="project-1",
        project_name="〇〇ホテル様導入案件",
        attendee_display="yamada@example.com",
        rep_email="sales@cnctor.jp",
        rep_slack_user_id="U123",
    )
    payload = {
        "response_url": "https://hooks.slack.com/actions/T000/000/xxx",
        "user": {"id": "U123"},
        "actions": [{"action_id": APPROVE_ACTION_ID, "value": candidate.to_button_value()}],
    }
    body = urlencode({"payload": json.dumps(payload)})

    client = _ActorCapturingContactClient()
    slack_handler(_signed_event(body), context=None, action_client=client)

    assert client.captured_actors == [Actor(source="slack_interaction_webhook", label="U123")]


# --- gmail_sync（src/gmail_sync/sync.py） --------------------------------------------------


def test_gmail_sync_process_message_ref_uses_gmail_sync_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.gmail_sync import sync
    from src.gmail_sync.gmail_client import GmailMessage

    class _FakeContactClient:
        def __init__(self) -> None:
            self.captured_actors: list[Actor] = []

        def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            return [
                {"id": "contact-1", "properties": {"メールアドレス": {"type": "email", "email": "lead@client.example.com"}}}
            ]

        def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
            self.captured_actors.append(get_actor())

    monkeypatch.setattr(sync.db, "email_log_exists", lambda message_id: False)
    monkeypatch.setattr(
        sync.gmail_client,
        "get_message",
        lambda access_token, message_id: GmailMessage(
            id=message_id,
            from_header="lead@client.example.com",
            to_header="rep@cnctor.jp",
            subject="件名",
            snippet="本文",
            date_header="Mon, 16 Aug 2026 09:00:00 +0900",
        ),
    )
    monkeypatch.setattr(sync.db, "insert_email_log", lambda **kwargs: None)
    monkeypatch.setattr(sync, "notify_web_engagement_tool", lambda **kwargs: None)

    contact_client = _FakeContactClient()
    sync._process_message_ref(
        "msg1", "token", "rep@cnctor.jp", contact_client, internal_domains=frozenset({"cnctor.jp"})
    )

    assert contact_client.captured_actors == [Actor(source="gmail_sync", label="rep@cnctor.jp")]


# --- migration（src/migration/migration_pipeline.py、ThreadPoolExecutor経由も含む） -------


def test_migration_materialize_uses_migration_actor() -> None:
    from src.db_schema.registry import SCHEMAS_BY_KEY
    from src.migration.migration_pipeline import MigrationPlan, PreparedRecord, materialize

    class _ActorCapturingClient:
        def __init__(self) -> None:
            self.captured_actors: list[Actor] = []

        def create_page(self, properties: dict[str, Any]) -> str:
            self.captured_actors.append(get_actor())
            return f"page-{len(self.captured_actors)}"

    record = PreparedRecord(db_key="chain", kintone_id="k1", properties={"チェーン名": "テストチェーン"})
    prepared = {key: [] for key in SCHEMAS_BY_KEY}
    prepared["chain"] = [record]
    plan = MigrationPlan(prepared=prepared)
    store = SQLiteIdMappingStore(":memory:")
    client = _ActorCapturingClient()

    materialize(plan, id_mapping_store=store, notion_clients={"chain": client}, dry_run=False)
    store.close()

    assert client.captured_actors == [Actor(source="migration", label=None)]


def test_migration_materialize_uses_migration_actor_via_thread_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """shirokuma-secレビュー対応（2026-08-17）: `notion_client_pools`経由（複数トークン・
    `ThreadPoolExecutor`による並列実行、`src/migration/migration_pipeline.py`参照）でも
    `set_actor("migration")`が正しく効くことを検証する。contextvarsは呼び出し元スレッドから
    `ThreadPoolExecutor`のワーカースレッドへ自動伝播しないため、逐次実行パス
    （`test_migration_materialize_uses_migration_actor`）だけでは検証できない
    （実際に`create_page()`を呼ぶ`_check_create_and_register()`内でwithしている設計、
    同関数のコメント参照）。"""
    import threading

    from src.db_schema.registry import SCHEMAS_BY_KEY
    from src.migration.migration_pipeline import MigrationPlan, PreparedRecord, materialize

    class _ActorCapturingClient:
        def __init__(self) -> None:
            self.captured_actors: list[Actor] = []
            self.threads: list[int] = []
            self._lock = threading.Lock()

        def create_page(self, properties: dict[str, Any]) -> str:
            with self._lock:
                self.captured_actors.append(get_actor())
                self.threads.append(threading.get_ident())
            return f"page-{len(self.captured_actors)}"

    records = [
        PreparedRecord(db_key="chain", kintone_id=f"k{i}", properties={"チェーン名": f"テストチェーン{i}"})
        for i in range(6)
    ]
    prepared = {key: [] for key in SCHEMAS_BY_KEY}
    prepared["chain"] = records
    plan = MigrationPlan(prepared=prepared)
    store = SQLiteIdMappingStore(":memory:")
    pool = [_ActorCapturingClient(), _ActorCapturingClient()]

    materialize(
        plan,
        id_mapping_store=store,
        notion_clients={"chain": pool[0]},
        notion_client_pools={"chain": pool},
        dry_run=False,
    )
    store.close()

    all_captured = pool[0].captured_actors + pool[1].captured_actors
    assert len(all_captured) == 6
    assert all(actor == Actor(source="migration", label=None) for actor in all_captured)
    # 実際に複数ワーカースレッドから呼ばれたこと（テストがたまたま逐次実行と同じ経路を
    # なぞっているだけになっていないことの確認）。
    all_thread_ids = pool[0].threads + pool[1].threads
    assert len(set(all_thread_ids)) > 1
