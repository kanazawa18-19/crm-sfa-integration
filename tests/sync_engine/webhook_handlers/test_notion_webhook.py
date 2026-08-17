from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.db_schema.base import Tool
from src.db_schema.registry import ALL_SCHEMAS
from src.lead_sync.web_engagement_tool_client import LeadSyncApiError
from src.sync_engine.clients.notion_client import HttpNotionClient, NotionApiError
from src.sync_engine.dispatcher import Dispatcher, DispatchResult
from src.sync_engine.id_mapping import SQLiteIdMappingStore
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_headers import HEADER_NAME
from src.sync_engine.webhook_handlers._common import WEBHOOK_SECRET_HEADER
from src.sync_engine.webhook_handlers.notion_webhook import (
    NotionPageClient,
    PARSEABLE_NOTION_PROPERTY_TYPES,
    _default_db_id_to_db_key,
    fetch_and_normalize_notion_page,
    handler,
    handler_with_proxy,
    notion_payload_to_sync_event,
    parse_notion_property_value,
)

DB_ID_MAP = {"26d6f1e2-1111-1111-1111-111111111111": "project"}


def _payload() -> dict:
    return {
        "event_id": "evt_xxx",
        "type": "page.updated",
        "page_id": "26d6f1e2-0000-0000-0000-000000000000",
        "database_id": "26d6f1e2-1111-1111-1111-111111111111",
        "last_edited_time": "2026-08-05T09:00:00.000Z",
        "properties": {
            "案件名": {"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]},
            "営業ステータス": {"type": "status", "status": {"name": "提案中"}},
            "初期費用": {"type": "number", "number": 500000},
        },
    }


# --- _default_db_id_to_db_key: shirokuma-secレビューWARN対応（.notion_db_ids.jsonキャッシュ廃止）


def test_default_db_id_to_db_key_resolves_all_schemas_from_registry_without_cache_file() -> None:
    """.notion_db_ids.jsonキャッシュファイルを読まず、ALL_SCHEMASのnotion_database_idから
    直接database_id -> db_keyの逆引き表を組み立てられることを検証する。"""
    resolver = _default_db_id_to_db_key()

    assert len(resolver) == len(ALL_SCHEMAS)
    for schema in ALL_SCHEMAS:
        assert resolver[schema.notion_database_id] == schema.key


# --- parse_notion_property_value --------------------------------------------------------


@pytest.mark.parametrize(
    ("prop", "expected"),
    [
        ({"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]}, "MSA-PJ-001"),
        ({"type": "rich_text", "rich_text": [{"plain_text": "備考テキスト"}]}, "備考テキスト"),
        ({"type": "title", "title": []}, None),
        ({"type": "select", "select": {"name": "ホテル・旅館"}}, "ホテル・旅館"),
        ({"type": "select", "select": None}, None),
        ({"type": "status", "status": {"name": "提案中"}}, "提案中"),
        ({"type": "number", "number": 500000}, 500000),
        ({"type": "checkbox", "checkbox": True}, True),
        ({"type": "date", "date": {"start": "2026-08-05"}}, "2026-08-05"),
        ({"type": "date", "date": None}, None),
        ({"type": "email", "email": "a@example.com"}, "a@example.com"),
        ({"type": "phone_number", "phone_number": "090-0000-0000"}, "090-0000-0000"),
        ({"type": "url", "url": "https://example.com"}, "https://example.com"),
        ({"type": "relation", "relation": [{"id": "rel-1"}, {"id": "rel-2"}]}, ["rel-1", "rel-2"]),
        ({"type": "people", "people": [{"id": "user-1"}]}, ["user-1"]),
        (
            {"type": "multi_select", "multi_select": [{"name": "リピッテ"}, {"name": "メイリー"}]},
            ["リピッテ", "メイリー"],
        ),
        ({"type": "multi_select", "multi_select": []}, []),
    ],
)
def test_parse_notion_property_value(prop: dict, expected: object) -> None:
    assert parse_notion_property_value(prop) == expected


def test_parse_notion_property_value_unsupported_type_raises() -> None:
    with pytest.raises(ValueError):
        parse_notion_property_value({"type": "files", "files": []})


def test_parseable_notion_property_types_matches_parse_notion_property_value_branches() -> None:
    """`PARSEABLE_NOTION_PROPERTY_TYPES`とparse_notion_property_value()の実装がズレていないことを
    保証する回帰テスト。

    以前は`src/sync_engine/clients/notion_client.py`側にも同じ内容の型リストが重複定義
    されており、parse_notion_property_value()が対応する型を増減した際に片方だけ更新漏れが
    起きるとクラッシュ（whitelistが狭すぎる）または元のバグ（whitelistが広すぎて未対応型を
    渡してしまう）が再発する恐れがあった。今は`notion_client.py`がこの定数を直接importして
    単一の情報源にしているが、それでも「定数」と「parse_notion_property_value()のif分岐の
    実装」自体がズレる可能性は残るため、期待する型集合をハードコードし固定化する。
    """
    expected_types = {
        "title",
        "rich_text",
        "select",
        "status",
        "multi_select",
        "number",
        "checkbox",
        "date",
        "email",
        "phone_number",
        "url",
        "relation",
        "people",
    }
    assert PARSEABLE_NOTION_PROPERTY_TYPES == expected_types

    # 定数に型を足しただけで対応する分岐を実装し忘れる（またはその逆）ケースを検知するため、
    # 各型を実際にparse_notion_property_value()へ通して例外にならないことも確認する。
    minimal_props: dict[str, dict] = {
        "title": {"type": "title", "title": []},
        "rich_text": {"type": "rich_text", "rich_text": []},
        "select": {"type": "select", "select": None},
        "status": {"type": "status", "status": None},
        "multi_select": {"type": "multi_select", "multi_select": []},
        "number": {"type": "number", "number": None},
        "checkbox": {"type": "checkbox", "checkbox": False},
        "date": {"type": "date", "date": None},
        "email": {"type": "email", "email": None},
        "phone_number": {"type": "phone_number", "phone_number": None},
        "url": {"type": "url", "url": None},
        "relation": {"type": "relation", "relation": []},
        "people": {"type": "people", "people": []},
    }
    assert set(minimal_props) == expected_types
    for prop_type, prop in minimal_props.items():
        parse_notion_property_value(prop)  # 未対応の型であればValueErrorが送出される


# --- notion_payload_to_sync_event -------------------------------------------------------


def test_notion_payload_to_sync_event_builds_expected_event() -> None:
    event = notion_payload_to_sync_event(_payload(), {}, db_id_to_db_key=DB_ID_MAP)

    assert event.source_tool is Tool.NOTION
    assert event.db_key == "project"
    assert event.external_id == "26d6f1e2-0000-0000-0000-000000000000"
    assert event.occurred_at == datetime(2026, 8, 5, 9, 0, 0, tzinfo=timezone.utc)
    assert event.properties == {
        "案件名": "MSA-PJ-001",
        "営業ステータス": "提案中",
        "初期費用": 500000,
    }
    assert event.sync_system_id is None


def test_notion_payload_to_sync_event_reads_sync_system_id_header_case_insensitively() -> None:
    event = notion_payload_to_sync_event(
        _payload(), {HEADER_NAME.lower(): "自社CRM-Engine"}, db_id_to_db_key=DB_ID_MAP
    )

    assert event.sync_system_id == "自社CRM-Engine"


def test_notion_payload_to_sync_event_unknown_database_id_raises() -> None:
    with pytest.raises(ValueError):
        notion_payload_to_sync_event(_payload(), {}, db_id_to_db_key={})


def _real_data_shaped_project_payload() -> dict:
    """実データの案件管理DBを模した、読み取り専用プロパティ（rollup/formula/button/
    unique_id/created_time/last_edited_time/created_by）を複数含むペイロード。"""
    return {
        "event_id": "evt_xxx",
        "type": "page.updated",
        "page_id": "26d6f1e2-0000-0000-0000-000000000000",
        "database_id": "26d6f1e2-1111-1111-1111-111111111111",
        "last_edited_time": "2026-08-05T09:00:00.000Z",
        "properties": {
            "案件名": {"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]},
            "営業ステータス": {"type": "status", "status": {"name": "提案中"}},
            "提案サービス": {
                "type": "multi_select",
                "multi_select": [{"name": "リピッテ"}, {"name": "メイリー"}],
            },
            # 以下は読み取り専用（is_writable=False）のためスキップされるべきプロパティ。
            "粗利": {"type": "formula", "formula": {"type": "number", "number": 100000}},
            "アクションログ": {"type": "rollup", "rollup": {"type": "array", "array": []}},
            "案件ID": {"type": "unique_id", "unique_id": {"prefix": "MSA-PJ", "number": 1}},
            "作成日時": {"type": "created_time", "created_time": "2026-08-01T00:00:00.000Z"},
            "最終更新日時": {
                "type": "last_edited_time",
                "last_edited_time": "2026-08-05T09:00:00.000Z",
            },
        },
    }


def test_notion_payload_to_sync_event_skips_read_only_properties() -> None:
    event = notion_payload_to_sync_event(
        _real_data_shaped_project_payload(), {}, db_id_to_db_key=DB_ID_MAP
    )

    assert event.properties == {
        "案件名": "MSA-PJ-001",
        "営業ステータス": "提案中",
        "提案サービス": ["リピッテ", "メイリー"],
    }


def test_notion_payload_to_sync_event_skips_files_property_without_raising() -> None:
    """shirokuma-sec/kuma-qa指摘: 案件管理DBの「申込書・契約書」「見積書」はFILES型かつ
    is_writable=Trueだが、parse_notion_property_value()が未対応のため、例外を送出せず
    ホワイトリスト外としてスキップされる必要がある。"""
    payload = _real_data_shaped_project_payload()
    payload["properties"]["申込書・契約書"] = {
        "type": "files",
        "files": [{"name": "契約書.pdf", "file": {"url": "https://example.com/a.pdf"}}],
    }
    payload["properties"]["見積書"] = {"type": "files", "files": []}

    event = notion_payload_to_sync_event(payload, {}, db_id_to_db_key=DB_ID_MAP)

    assert "申込書・契約書" not in event.properties
    assert "見積書" not in event.properties
    assert event.properties == {
        "案件名": "MSA-PJ-001",
        "営業ステータス": "提案中",
        "提案サービス": ["リピッテ", "メイリー"],
    }


def test_notion_payload_to_sync_event_parses_multi_select_property() -> None:
    event = notion_payload_to_sync_event(
        _real_data_shaped_project_payload(), {}, db_id_to_db_key=DB_ID_MAP
    )

    assert event.properties["提案サービス"] == ["リピッテ", "メイリー"]


def test_notion_payload_to_sync_event_ignores_unknown_property_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _payload()
    payload["properties"]["未定義プロパティ"] = {"type": "rich_text", "rich_text": []}

    with caplog.at_level("WARNING"):
        event = notion_payload_to_sync_event(payload, {}, db_id_to_db_key=DB_ID_MAP)

    assert "未定義プロパティ" not in event.properties
    assert any("未定義プロパティ" in record.getMessage() for record in caplog.records)


# --- handler -----------------------------------------------------------------------------


def test_handler_without_dispatcher_returns_200_and_no_skip_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": None}


def test_handler_dispatches_to_injected_dispatcher() -> None:
    import json

    store = SQLiteIdMappingStore(":memory:")
    dispatcher = Dispatcher(store, {})
    event = {"body": json.dumps(_payload()), "headers": {}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
        mp.setattr(
            "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
            lambda: DB_ID_MAP,
        )
        response = handler(event, context=None, dispatcher=dispatcher)

    store.close()
    assert response["statusCode"] == 200
    # IDマッピングが存在しないので unknown_record としてスキップされる
    assert json.loads(response["body"]) == {"skipped": True}


# --- BLOCKER5: 不正・欠損ペイロード時のエラーハンドリング -------------------------------


def test_handler_returns_400_for_malformed_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": "{not valid json", "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_missing_required_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    payload = _payload()
    del payload["page_id"]
    event = {"body": json.dumps(payload), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_unknown_database_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": json.dumps(_payload()), "headers": {}}
    # _default_db_id_to_db_key()をmonkeypatchしないため、実在しないdatabase_idとして扱われる
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
            lambda: {},
        )
        response = handler(event, context=None)

    assert response["statusCode"] == 400


# --- BLOCKER7: 共有シークレット検証 -----------------------------------------------------


def test_handler_returns_401_when_secret_mismatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {WEBHOOK_SECRET_HEADER: "wrong-secret"}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_secret_header_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_succeeds_when_secret_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", "correct-secret")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    event = {
        "body": json.dumps(_payload()),
        "headers": {WEBHOOK_SECRET_HEADER: "correct-secret"},
    }

    response = handler(event, context=None)

    assert response["statusCode"] == 200


# --- BLOCKER6: プロキシ層スタブ fetch_and_normalize_notion_page ------------------------


class _FakeNotionPageClient:
    def __init__(self, page: dict) -> None:
        self._page = page

    def get_raw_page(self, page_id: str) -> dict:
        assert page_id == self._page["id"]
        return self._page


def test_fetch_and_normalize_notion_page_builds_expected_payload_shape() -> None:
    client: NotionPageClient = _FakeNotionPageClient(
        {
            "id": "26d6f1e2-0000-0000-0000-000000000000",
            "parent": {"type": "database_id", "database_id": "26d6f1e2-1111-1111-1111-111111111111"},
            "last_edited_time": "2026-08-05T09:00:00.000Z",
            "properties": {
                "案件ID": {"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]},
            },
        }
    )

    normalized = fetch_and_normalize_notion_page(
        "26d6f1e2-0000-0000-0000-000000000000", client
    )

    assert normalized["page_id"] == "26d6f1e2-0000-0000-0000-000000000000"
    assert normalized["database_id"] == "26d6f1e2-1111-1111-1111-111111111111"
    assert normalized["last_edited_time"] == "2026-08-05T09:00:00.000Z"
    assert normalized["properties"] == {
        "案件ID": {"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]}
    }


# --- handler_with_proxy -------------------------------------------------------------------


def _lightweight_payload() -> dict:
    return {
        "id": "evt_xxx",
        "timestamp": "2026-08-05T09:00:00.000Z",
        "workspace_id": "ws_xxx",
        "type": "page.properties_updated",
        "entity": {"id": "26d6f1e2-0000-0000-0000-000000000000", "type": "page"},
        "data": {
            "parent": {"id": "26d6f1e2-1111-1111-1111-111111111111", "type": "database"},
            "updated_properties": ["title"],
        },
    }


def _raw_notion_page() -> dict:
    return {
        "id": "26d6f1e2-0000-0000-0000-000000000000",
        "parent": {"type": "database_id", "database_id": "26d6f1e2-1111-1111-1111-111111111111"},
        "last_edited_time": "2026-08-05T09:00:00.000Z",
        "properties": {
            "案件名": {"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]},
            "営業ステータス": {"type": "status", "status": {"name": "提案中"}},
            "初期費用": {"type": "number", "number": 500000},
        },
    }


class _FailingNotionPageClient:
    def get_raw_page(self, page_id: str) -> dict:
        raise RuntimeError("notion api unavailable")


def test_handler_with_proxy_fetches_page_and_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    store = SQLiteIdMappingStore(":memory:")
    dispatcher = Dispatcher(store, {})
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(
        event, context=None, notion_client=client, dispatcher=dispatcher
    )

    store.close()
    assert response["statusCode"] == 200
    # IDマッピングが存在しないので unknown_record としてスキップされる
    assert json.loads(response["body"]) == {"skipped": True}


def test_handler_with_proxy_without_dispatcher_returns_200_and_no_skip_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(event, context=None, notion_client=client)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": None}


def test_handler_with_proxy_returns_400_for_malformed_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    event = {"body": "{not valid json", "headers": {}}

    response = handler_with_proxy(event, context=None, notion_client=client)

    assert response["statusCode"] == 400


def test_handler_with_proxy_returns_400_for_missing_entity_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    payload = _lightweight_payload()
    del payload["entity"]
    event = {"body": json.dumps(payload), "headers": {}}

    response = handler_with_proxy(event, context=None, notion_client=client)

    assert response["statusCode"] == 400
    message = json.loads(response["body"])["error"]
    assert "entity" in message


def test_handler_with_proxy_returns_400_for_missing_entity_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    payload = _lightweight_payload()
    del payload["entity"]["id"]
    event = {"body": json.dumps(payload), "headers": {}}

    response = handler_with_proxy(event, context=None, notion_client=client)

    assert response["statusCode"] == 400
    message = json.loads(response["body"])["error"]
    assert "id" in message
    assert "entity" not in message


def test_handler_with_proxy_400_messages_distinguish_missing_entity_vs_missing_entity_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kuma-qa指摘4: 400系のエラーメッセージが両ケースを区別できることを検証する。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())

    missing_entity_payload = _lightweight_payload()
    del missing_entity_payload["entity"]
    missing_entity_response = handler_with_proxy(
        {"body": json.dumps(missing_entity_payload), "headers": {}},
        context=None,
        notion_client=client,
    )

    missing_id_payload = _lightweight_payload()
    del missing_id_payload["entity"]["id"]
    missing_id_response = handler_with_proxy(
        {"body": json.dumps(missing_id_payload), "headers": {}},
        context=None,
        notion_client=client,
    )

    missing_entity_message = json.loads(missing_entity_response["body"])["error"]
    missing_id_message = json.loads(missing_id_response["body"])["error"]
    assert missing_entity_message != missing_id_message


def test_handler_with_proxy_returns_500_when_notion_api_call_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """kuma-qa指摘4: ステータスコードだけでなく、ログにpage_id等の有用な情報が
    含まれることも検証する。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    with caplog.at_level("ERROR"):
        response = handler_with_proxy(
            event, context=None, notion_client=_FailingNotionPageClient()
        )

    assert response["statusCode"] == 500
    page_id = _lightweight_payload()["entity"]["id"]
    assert any(page_id in record.getMessage() for record in caplog.records)


def test_handler_with_proxy_returns_200_and_skip_reason_when_page_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shirokuma-sec指摘2: ページ削除（404）時は無駄な再送ループを避けるため
    500ではなく200＋skip扱いを返す。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")

    class _NotFoundNotionPageClient:
        def get_raw_page(self, page_id: str) -> dict:
            raise NotionApiError(404, "not found")

    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(
        event, context=None, notion_client=_NotFoundNotionPageClient()
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": "page_not_found"}


def test_handler_with_proxy_returns_500_when_notion_api_call_fails_non_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404以外のAPIエラー（例: 500）は引き続き500として扱われることを確認する。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")

    class _ServerErrorNotionPageClient:
        def get_raw_page(self, page_id: str) -> dict:
            raise NotionApiError(500, "internal error")

    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(
        event, context=None, notion_client=_ServerErrorNotionPageClient()
    )

    assert response["statusCode"] == 500


def test_handler_with_proxy_returns_400_for_unknown_database_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: {},
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(event, context=None, notion_client=client)

    assert response["statusCode"] == 400


def test_handler_with_proxy_returns_401_when_secret_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", "correct-secret")
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    event = {
        "body": json.dumps(_lightweight_payload()),
        "headers": {WEBHOOK_SECRET_HEADER: "wrong-secret"},
    }

    response = handler_with_proxy(event, context=None, notion_client=client)

    assert response["statusCode"] == 401


def test_handler_with_proxy_succeeds_when_secret_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", "correct-secret")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    event = {
        "body": json.dumps(_lightweight_payload()),
        "headers": {WEBHOOK_SECRET_HEADER: "correct-secret"},
    }

    response = handler_with_proxy(event, context=None, notion_client=client)

    assert response["statusCode"] == 200


# --- BLOCKER: SyncEventの中間データ検証（スパイDispatcher） -----------------------------


class _SpyDispatcher:
    """dispatch()に渡されたSyncEventをそのまま記録するフェイクDispatcher。"""

    def __init__(self) -> None:
        self.dispatched_events: list[SyncEvent] = []

    def dispatch(self, event: SyncEvent) -> DispatchResult:
        self.dispatched_events.append(event)
        return DispatchResult(skipped=False)


def test_handler_with_proxy_dispatches_sync_event_with_expected_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kuma-qa指摘3: 最終レスポンスの200/skippedだけでなく、実際に生成されたSyncEventの
    db_key/external_id/properties/occurred_atを直接検証する。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    spy_dispatcher = _SpyDispatcher()
    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(
        event, context=None, notion_client=client, dispatcher=spy_dispatcher
    )

    assert response["statusCode"] == 200
    assert len(spy_dispatcher.dispatched_events) == 1
    dispatched = spy_dispatcher.dispatched_events[0]
    assert dispatched.source_tool is Tool.NOTION
    assert dispatched.db_key == "project"
    assert dispatched.external_id == "26d6f1e2-0000-0000-0000-000000000000"
    assert dispatched.occurred_at == datetime(2026, 8, 5, 9, 0, 0, tzinfo=timezone.utc)
    assert dispatched.properties == {
        "案件名": "MSA-PJ-001",
        "営業ステータス": "提案中",
        "初期費用": 500000,
    }


# --- BLOCKER: HttpNotionClientを実際に注入した統合テスト ---------------------------------
# shirokuma-sec/kuma-qa指摘: HttpNotionClient.get_raw_page()がNotionPageClient Protocolの
# メソッド名（get_page）と一致しておらず配線されていなかった（KeyError: 'id'で500になる）。
# ここでは実際のHttpNotionClientインスタンスをrequests_mockでNotion API生レスポンスを
# モックした状態でhandler_with_proxyへ注入し、正しく動作することを検証する。


def test_handler_with_proxy_works_with_real_http_notion_client(
    requests_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    page_id = "26d6f1e2-0000-0000-0000-000000000000"
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{page_id}",
        json=_raw_notion_page(),
    )
    notion_client = HttpNotionClient(
        "project",
        "26d6f1e2-1111-1111-1111-111111111111",
        api_key="secret-notion-key",
    )
    spy_dispatcher = _SpyDispatcher()
    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(
        event, context=None, notion_client=notion_client, dispatcher=spy_dispatcher
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": False}
    assert len(spy_dispatcher.dispatched_events) == 1
    dispatched = spy_dispatcher.dispatched_events[0]
    assert dispatched.db_key == "project"
    assert dispatched.external_id == page_id
    assert dispatched.properties == {
        "案件名": "MSA-PJ-001",
        "営業ステータス": "提案中",
        "初期費用": 500000,
    }


# --- calendar_sync フック ------------------------------------------------------------------


def test_handler_with_proxy_calls_calendar_sync_when_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    calls: list[tuple[dict, str]] = []

    def _calendar_sync(properties: dict, page_id: str) -> None:
        calls.append((dict(properties), page_id))

    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(
        event, context=None, notion_client=client, calendar_sync=_calendar_sync
    )

    assert response["statusCode"] == 200
    assert len(calls) == 1
    called_properties, called_page_id = calls[0]
    assert called_page_id == "26d6f1e2-0000-0000-0000-000000000000"
    assert called_properties == {
        "案件名": "MSA-PJ-001",
        "営業ステータス": "提案中",
        "初期費用": 500000,
    }


def test_handler_with_proxy_returns_200_even_when_calendar_sync_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())

    def _failing_calendar_sync(properties: dict, page_id: str) -> None:
        raise RuntimeError("calendar sync boom")

    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    with caplog.at_level("ERROR"):
        response = handler_with_proxy(
            event, context=None, notion_client=client, calendar_sync=_failing_calendar_sync
        )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": None}
    assert any("calendar" in record.getMessage() for record in caplog.records)


def test_handler_with_proxy_without_calendar_sync_behaves_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """calendar_sync未注入時は既存の挙動と変わらないことを確認する。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(event, context=None, notion_client=client)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": None}


# --- project_mirror_sync フック -----------------------------------------------------------------


def test_handler_with_proxy_calls_project_mirror_sync_when_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    calls: list[tuple[dict, str]] = []

    def _project_mirror_sync(properties: dict, page_id: str) -> None:
        calls.append((dict(properties), page_id))

    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(
        event, context=None, notion_client=client, project_mirror_sync=_project_mirror_sync
    )

    assert response["statusCode"] == 200
    assert len(calls) == 1
    called_properties, called_page_id = calls[0]
    assert called_page_id == "26d6f1e2-0000-0000-0000-000000000000"
    assert called_properties == {
        "案件名": "MSA-PJ-001",
        "営業ステータス": "提案中",
        "初期費用": 500000,
    }


def test_handler_with_proxy_returns_200_even_when_project_mirror_sync_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())

    def _failing_project_mirror_sync(properties: dict, page_id: str) -> None:
        raise RuntimeError("project mirror sync boom")

    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    with caplog.at_level("ERROR"):
        response = handler_with_proxy(
            event,
            context=None,
            notion_client=client,
            project_mirror_sync=_failing_project_mirror_sync,
        )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": None}
    assert any("project mirror" in record.getMessage() for record in caplog.records)


def test_handler_with_proxy_without_project_mirror_sync_behaves_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """project_mirror_sync未注入時は既存の挙動と変わらないことを確認する。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(event, context=None, notion_client=client)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": None}


# --- lead_sync フック -----------------------------------------------------------------------

_CONTACT_DB_ID = "3b4d8ea8-d4f3-808d-9853-d9cdd3de39ae"
DB_ID_MAP_WITH_CONTACT = {**DB_ID_MAP, _CONTACT_DB_ID: "contact"}


def _contact_lightweight_payload() -> dict:
    return {
        "id": "evt_yyy",
        "timestamp": "2026-08-05T09:00:00.000Z",
        "workspace_id": "ws_xxx",
        "type": "page.properties_updated",
        "entity": {"id": "cnt-0000-0000-0000-000000000000", "type": "page"},
        "data": {
            "parent": {"id": _CONTACT_DB_ID, "type": "database"},
            "updated_properties": ["title"],
        },
    }


def _raw_contact_page() -> dict:
    return {
        "id": "cnt-0000-0000-0000-000000000000",
        "parent": {"type": "database_id", "database_id": _CONTACT_DB_ID},
        "last_edited_time": "2026-08-05T09:00:00.000Z",
        "properties": {
            "名前": {"type": "title", "title": [{"plain_text": "山田太郎"}]},
            "メールアドレス": {"type": "email", "email": "yamada@example.com"},
        },
    }


def test_handler_with_proxy_calls_lead_sync_when_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP_WITH_CONTACT,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_contact_page())
    calls: list[tuple[dict, str]] = []

    def _lead_sync(properties: dict, page_id: str) -> None:
        calls.append((dict(properties), page_id))

    event = {"body": json.dumps(_contact_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(
        event, context=None, notion_client=client, lead_sync=_lead_sync
    )

    assert response["statusCode"] == 200
    assert len(calls) == 1
    called_properties, called_page_id = calls[0]
    assert called_page_id == "cnt-0000-0000-0000-000000000000"
    assert called_properties == {"名前": "山田太郎", "メールアドレス": "yamada@example.com"}


def test_handler_with_proxy_does_not_call_lead_sync_for_project_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lead_syncはdb_key="contact"のイベントのみ発火し、"project"では呼ばれないことを確認する。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP_WITH_CONTACT,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_notion_page())
    calls: list[tuple[dict, str]] = []

    def _lead_sync(properties: dict, page_id: str) -> None:
        calls.append((dict(properties), page_id))

    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(
        event, context=None, notion_client=client, lead_sync=_lead_sync
    )

    assert response["statusCode"] == 200
    assert calls == []


def test_handler_with_proxy_returns_200_even_when_lead_sync_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP_WITH_CONTACT,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_contact_page())

    def _failing_lead_sync(properties: dict, page_id: str) -> None:
        raise RuntimeError("lead sync boom")

    event = {"body": json.dumps(_contact_lightweight_payload()), "headers": {}}

    with caplog.at_level("ERROR"):
        response = handler_with_proxy(
            event, context=None, notion_client=client, lead_sync=_failing_lead_sync
        )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": None}
    assert any("lead" in record.getMessage() for record in caplog.records)


def test_handler_with_proxy_lead_sync_failure_log_does_not_leak_contact_pii(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """shirokuma-secレビューWARN対応: LeadSyncApiErrorのメッセージ（web-engagement-tool側の
    HTTPエラーレスポンス本文由来、連絡先のメールアドレス等を含みうる）が、失敗時のログ出力へ
    そのまま記録されない（例外の型名・status_code・page_idのみが記録される）ことを検証する。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP_WITH_CONTACT,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_contact_page())
    leaked_email = "yamada-pii-leak-check@example.com"

    def _failing_lead_sync(properties: dict, page_id: str) -> None:
        raise LeadSyncApiError(400, f"invalid email: {leaked_email}")

    event = {"body": json.dumps(_contact_lightweight_payload()), "headers": {}}

    with caplog.at_level("ERROR"):
        response = handler_with_proxy(
            event, context=None, notion_client=client, lead_sync=_failing_lead_sync
        )

    assert response["statusCode"] == 200
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert leaked_email not in log_text
    assert "LeadSyncApiError" in log_text
    assert "400" in log_text


def test_handler_with_proxy_without_lead_sync_behaves_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lead_sync未注入時は既存の挙動と変わらないことを確認する。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP_WITH_CONTACT,
    )
    client: NotionPageClient = _FakeNotionPageClient(_raw_contact_page())
    event = {"body": json.dumps(_contact_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(event, context=None, notion_client=client)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": None}


def test_handler_with_proxy_with_real_http_notion_client_returns_200_on_404(
    requests_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    page_id = "26d6f1e2-0000-0000-0000-000000000000"
    requests_mock.get(f"https://api.notion.com/v1/pages/{page_id}", status_code=404)
    notion_client = HttpNotionClient(
        "project",
        "26d6f1e2-1111-1111-1111-111111111111",
        api_key="secret-notion-key",
    )
    event = {"body": json.dumps(_lightweight_payload()), "headers": {}}

    response = handler_with_proxy(event, context=None, notion_client=notion_client)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": "page_not_found"}
