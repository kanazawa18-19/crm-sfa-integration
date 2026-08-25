from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.sync_headers import HEADER_NAME
from src.sync_engine.webhook_handlers.kintone_webhook import handler, kintone_payload_to_sync_event

APP_ID_MAP = {"123": "project", "456": "client_master", "789": "action"}


def _payload(app_id: str = "123", record: dict | None = None) -> dict:
    return {
        "type": "record.updated",
        "app": {"id": app_id},
        "record": record
        if record is not None
        else {
            "$id": {"type": "__ID__", "value": "45"},
            "$revision": {"type": "__REVISION__", "value": "3"},
            "レコード番号": {"type": "RECORD_NUMBER", "value": "45"},
            "作成日時": {"type": "CREATED_TIME", "value": "2026-08-01T00:00:00Z"},
            "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
            "作成者": {"type": "CREATOR", "value": {"code": "user1"}},
            "更新者": {"type": "MODIFIER", "value": {"code": "user1"}},
            # 実際のkintoneフィールドコード（ラベルではない。2026-08-14、実API検証済み、
            # KINTONE_FIELD_TRANSFORMS参照）。
            "ドロップダウン_2": {"type": "DROP_DOWN", "value": "商談中（B）"},  # ラベル: 契約進捗状況
            "初期費用": {"type": "NUMBER", "value": "500000"},  # ラベル: 提案料金（イニシャル）
        },
    }


def test_kintone_payload_to_sync_event_builds_expected_event() -> None:
    event = kintone_payload_to_sync_event(_payload(), {}, app_id_to_db_key=APP_ID_MAP)

    assert event.source_tool is Tool.KINTONE
    assert event.db_key == "project"
    assert event.external_id == "45"
    assert event.occurred_at == datetime(2026, 8, 5, 9, 0, 0, tzinfo=timezone.utc)
    # KINTONE_FIELD_TRANSFORMSにより、kintoneフィールドコードからNotionプロパティ名へ
    # 変換される（"契約進捗状況"→"営業ステータス"、"商談中（B）"はaliasテーブルにより
    # "アポ"へ正規化される。src/migration/project_mapping.py参照）。「初期費用」はNUMBER型
    # のため文字列ではなくfloatへ変換される（shirokuma-sec/obasan-qualityレビューBLOCKER
    # 対応、2026-08-14）。
    assert event.properties == {
        "営業ステータス": "アポ",
        "初期費用": 500000.0,
    }
    assert event.sync_system_id is None


def test_kintone_payload_to_sync_event_builds_client_master_event() -> None:
    # obasan-qualityレビューWARN対応（2026-08-14）: project以外のdb_keyもend-to-endで
    # 検証する（従来はprojectしかペイロード全体を通した回帰テストが無かった）。
    record = {
        "$id": {"type": "__ID__", "value": "10"},
        "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
        "顧客名": {"type": "SINGLE_LINE_TEXT", "value": "テスト商事"},  # ラベル: 顧客名（法人・個人・施設）
        "顧客種別": {"type": "DROP_DOWN", "value": "ホテル・旅館"},
        "都道府県名": {"type": "DROP_DOWN", "value": "東京都"},
        "TEL": {"type": "SINGLE_LINE_TEXT", "value": "03-1234-5678"},
        # リレーション解決が必要なため意図的に対象外のフィールド（コード==ラベル）。
        "本部名": {"type": "SINGLE_LINE_TEXT", "value": "テストチェーン"},
    }

    event = kintone_payload_to_sync_event(
        _payload(app_id="456", record=record), {}, app_id_to_db_key=APP_ID_MAP
    )

    assert event.db_key == "client_master"
    assert event.properties == {
        "取引先名": "テスト商事",
        "顧客種別": "ホテル・旅館",
        "都道府県": "東京都",
        "TEL": "03-1234-5678",
    }
    assert "本部名" not in event.properties


def test_kintone_payload_to_sync_event_builds_action_event() -> None:
    record = {
        "$id": {"type": "__ID__", "value": "77"},
        "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
        "actionContent": {"type": "DROP_DOWN", "value": "電話"},  # ラベル: アクション内容
        "comment": {"type": "MULTI_LINE_TEXT", "value": "折り返し予定"},  # ラベル: コメント
        # リレーション解決が必要なため意図的に対象外のフィールド。
        "cnctorMember": {"type": "USER_SELECT", "value": [{"code": "yamada"}]},  # ラベル: 対応者
        "toPerson": {"type": "SINGLE_LINE_TEXT", "value": "先方 太郎"},  # ラベル: 担当者名
    }

    event = kintone_payload_to_sync_event(
        _payload(app_id="789", record=record), {}, app_id_to_db_key=APP_ID_MAP
    )

    assert event.db_key == "action"
    # "電話"はaliasテーブルにより"テレアポ"へ正規化される（src/migration/action_mapping.py）。
    assert event.properties == {
        "アクション種別": "テレアポ",
        "履歴メモ": "折り返し予定",
    }
    assert "cnctorMember" not in event.properties
    assert "toPerson" not in event.properties


def test_kintone_payload_to_sync_event_resolves_action_client_name_relation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2026-08-25: 取引先マスターリレーション解決(src/relation_sync/)のkintone webhook配線。
    from src.sync_engine.webhook_handlers import kintone_field_transforms as transforms_module

    monkeypatch.setattr(
        transforms_module,
        "resolve_client_master_relation",
        lambda raw_name, **kwargs: "notion-page-1" if kwargs["source_record_id"] == "77" else None,
    )
    record = {
        "$id": {"type": "__ID__", "value": "77"},
        "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
        "client_name": {"type": "SINGLE_LINE_TEXT", "value": "テスト商事"},  # ラベル: 顧客名
    }

    event = kintone_payload_to_sync_event(
        _payload(app_id="789", record=record), {}, app_id_to_db_key=APP_ID_MAP
    )

    assert event.properties == {"👨‍👩‍👧‍👦 取引先マスター": "notion-page-1"}


def test_kintone_payload_to_sync_event_skips_action_client_name_when_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 未解決(SKIP_FIELD)の場合、プロパティ自体をpropertiesへ含めない(既存リレーションを
    # 上書き・クリアしない)。
    from src.sync_engine.webhook_handlers import kintone_field_transforms as transforms_module

    monkeypatch.setattr(
        transforms_module, "resolve_client_master_relation", lambda raw_name, **kwargs: None
    )
    record = {
        "$id": {"type": "__ID__", "value": "77"},
        "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
        "client_name": {"type": "SINGLE_LINE_TEXT", "value": "曖昧な会社名"},
        "comment": {"type": "MULTI_LINE_TEXT", "value": "折り返し予定"},
    }

    event = kintone_payload_to_sync_event(
        _payload(app_id="789", record=record), {}, app_id_to_db_key=APP_ID_MAP
    )

    assert "👨‍👩‍👧‍👦 取引先マスター" not in event.properties
    assert event.properties["履歴メモ"] == "折り返し予定"


# --- 取引先マスターリレーションの「後勝ち」上書き防止ガード ---------------------------------
# 2026-08-25、GPT-5.6クロスレビュー指摘対応: 人がNotion上で手動修正したリレーションを、
# 後日kintone側のclient_nameが再編集されるたびに黙って上書きしてしまう事故を防ぐ。


class _FakeNotionRelationLookupClient:
    def __init__(self, pages: dict[str, dict | None]) -> None:
        self._pages = pages
        self.get_page_calls: list[str] = []

    def get_page(self, page_id: str) -> dict | None:
        self.get_page_calls.append(page_id)
        return self._pages.get(page_id)


class _FailingNotionRelationLookupClient:
    def get_page(self, page_id: str) -> dict | None:
        raise RuntimeError("notion api unavailable")


def _client_name_record(*, record_id: str = "77", client_name: str = "テスト商事") -> dict:
    return {
        "$id": {"type": "__ID__", "value": record_id},
        "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
        "client_name": {"type": "SINGLE_LINE_TEXT", "value": client_name},
    }


def _resolve_to(monkeypatch: pytest.MonkeyPatch, page_id: str | None) -> None:
    from src.sync_engine.webhook_handlers import kintone_field_transforms as transforms_module

    monkeypatch.setattr(
        transforms_module, "resolve_client_master_relation", lambda raw_name, **kwargs: page_id
    )


def test_kintone_payload_to_sync_event_drops_relation_when_already_set_on_notion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolve_to(monkeypatch, "notion-page-1")
    store = SQLiteIdMappingStore(":memory:")
    store.upsert(IdMapping(notion_key="notion-page-1", db_key="action", kintone_id="77"))
    notion_client = _FakeNotionRelationLookupClient(
        {"notion-page-1": {"👨‍👩‍👧‍👦 取引先マスター": ["existing-client-page"]}}
    )

    event = kintone_payload_to_sync_event(
        _payload(app_id="789", record=_client_name_record()),
        {},
        app_id_to_db_key=APP_ID_MAP,
        id_mapping_store=store,
        notion_client=notion_client,
    )

    store.close()
    assert "👨‍👩‍👧‍👦 取引先マスター" not in event.properties
    assert notion_client.get_page_calls == ["notion-page-1"]


def test_kintone_payload_to_sync_event_keeps_relation_when_not_yet_set_on_notion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolve_to(monkeypatch, "notion-page-1")
    store = SQLiteIdMappingStore(":memory:")
    store.upsert(IdMapping(notion_key="notion-page-1", db_key="action", kintone_id="77"))
    notion_client = _FakeNotionRelationLookupClient(
        {"notion-page-1": {"👨‍👩‍👧‍👦 取引先マスター": []}}
    )

    event = kintone_payload_to_sync_event(
        _payload(app_id="789", record=_client_name_record()),
        {},
        app_id_to_db_key=APP_ID_MAP,
        id_mapping_store=store,
        notion_client=notion_client,
    )

    store.close()
    assert event.properties == {"👨‍👩‍👧‍👦 取引先マスター": "notion-page-1"}


def test_kintone_payload_to_sync_event_keeps_relation_when_record_not_yet_migrated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IdMappingStoreにマッピングが無い(=対応するNotionページ自体が無い)場合、上書きの
    心配が無いため自動解決した値をそのまま通すこと。"""
    _resolve_to(monkeypatch, "notion-page-1")
    store = SQLiteIdMappingStore(":memory:")
    notion_client = _FakeNotionRelationLookupClient({})

    event = kintone_payload_to_sync_event(
        _payload(app_id="789", record=_client_name_record()),
        {},
        app_id_to_db_key=APP_ID_MAP,
        id_mapping_store=store,
        notion_client=notion_client,
    )

    store.close()
    assert event.properties == {"👨‍👩‍👧‍👦 取引先マスター": "notion-page-1"}
    assert notion_client.get_page_calls == []


def test_kintone_payload_to_sync_event_drops_relation_when_current_value_check_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """現在値の確認自体に失敗した場合、安全側に倒して書き込みをスキップすること。"""
    _resolve_to(monkeypatch, "notion-page-1")
    store = SQLiteIdMappingStore(":memory:")
    store.upsert(IdMapping(notion_key="notion-page-1", db_key="action", kintone_id="77"))

    with caplog.at_level("WARNING"):
        event = kintone_payload_to_sync_event(
            _payload(app_id="789", record=_client_name_record()),
            {},
            app_id_to_db_key=APP_ID_MAP,
            id_mapping_store=store,
            notion_client=_FailingNotionRelationLookupClient(),
        )

    store.close()
    assert "👨‍👩‍👧‍👦 取引先マスター" not in event.properties
    assert any("client-master relation" in r.getMessage() for r in caplog.records)


def test_kintone_payload_to_sync_event_keeps_relation_when_notion_page_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notionページ自体が見つからない(削除済み等)場合は上書き防止ガードの対象外とし、
    dispatcher側の通常の書き込み処理に委ねること。"""
    _resolve_to(monkeypatch, "notion-page-1")
    store = SQLiteIdMappingStore(":memory:")
    store.upsert(IdMapping(notion_key="notion-page-1", db_key="action", kintone_id="77"))
    notion_client = _FakeNotionRelationLookupClient({"notion-page-1": None})

    event = kintone_payload_to_sync_event(
        _payload(app_id="789", record=_client_name_record()),
        {},
        app_id_to_db_key=APP_ID_MAP,
        id_mapping_store=store,
        notion_client=notion_client,
    )

    store.close()
    assert event.properties == {"👨‍👩‍👧‍👦 取引先マスター": "notion-page-1"}


def test_kintone_payload_to_sync_event_skips_guard_when_store_and_client_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """id_mapping_store/notion_client未注入時は既存の挙動のまま(自動解決した値をそのまま
    使う)であることを確認する(後方互換)。"""
    _resolve_to(monkeypatch, "notion-page-1")

    event = kintone_payload_to_sync_event(
        _payload(app_id="789", record=_client_name_record()), {}, app_id_to_db_key=APP_ID_MAP
    )

    assert event.properties == {"👨‍👩‍👧‍👦 取引先マスター": "notion-page-1"}


def test_kintone_payload_to_sync_event_skips_fields_not_in_transform_table() -> None:
    # obasan-qualityレビューWARN対応（2026-08-14）: 架空のフィールド名ではなく、実際に
    # リレーション解決が必要なため意図的に対象外とされているフィールドコード（KINTONE_
    # FIELD_TRANSFORMSに存在しない）で検証する（コード"店舗名"、ラベル「施設名（会社名）」）。
    payload = _payload()
    payload["record"]["店舗名"] = {"type": "SINGLE_LINE_TEXT", "value": "何か"}

    event = kintone_payload_to_sync_event(payload, {}, app_id_to_db_key=APP_ID_MAP)

    assert "店舗名" not in event.properties
    assert "営業ステータス" in event.properties


def test_kintone_payload_to_sync_event_skips_field_when_value_normalization_fails() -> None:
    payload = _payload()
    payload["record"]["ドロップダウン_2"] = {"type": "DROP_DOWN", "value": "存在しないステータス"}

    event = kintone_payload_to_sync_event(payload, {}, app_id_to_db_key=APP_ID_MAP)

    assert "営業ステータス" not in event.properties
    # 他のフィールドの処理は継続する。
    assert event.properties["初期費用"] == 500000.0


def test_kintone_payload_to_sync_event_excludes_system_fields() -> None:
    event = kintone_payload_to_sync_event(_payload(), {}, app_id_to_db_key=APP_ID_MAP)

    for system_field in ("$id", "$revision", "レコード番号", "作成日時", "更新日時", "作成者", "更新者"):
        assert system_field not in event.properties


def test_kintone_payload_to_sync_event_reads_sync_system_id_header() -> None:
    event = kintone_payload_to_sync_event(
        _payload(), {HEADER_NAME: "自社CRM-Engine"}, app_id_to_db_key=APP_ID_MAP
    )

    assert event.sync_system_id == "自社CRM-Engine"


def test_kintone_payload_to_sync_event_unknown_app_id_raises() -> None:
    with pytest.raises(ValueError):
        kintone_payload_to_sync_event(_payload(), {}, app_id_to_db_key={})


def test_kintone_payload_to_sync_event_uses_env_var_app_id_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KINTONE_APP_ID_PROJECT", "123")

    event = kintone_payload_to_sync_event(_payload(), {})

    assert event.db_key == "project"


def test_handler_dispatches_to_injected_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
        lambda: APP_ID_MAP,
    )
    store = SQLiteIdMappingStore(":memory:")
    dispatcher = Dispatcher(store, {})
    event = {"body": json.dumps(_payload()), "headers": {}, "query_params": {}}

    response = handler(event, context=None, dispatcher=dispatcher)

    store.close()
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": True}


def test_handler_forwards_id_mapping_store_and_notion_client_to_sync_event_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """handler()に注入したid_mapping_store/notion_clientが、取引先マスターリレーションの
    「後勝ち」上書き防止ガード用にkintone_payload_to_sync_event()へそのまま渡されること
    （2026-08-25、GPT-5.6クロスレビュー指摘対応）。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
        lambda: APP_ID_MAP,
    )
    captured: dict = {}
    original = kintone_payload_to_sync_event

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.kintone_webhook.kintone_payload_to_sync_event", _spy
    )
    sentinel_store = object()
    sentinel_notion_client = object()
    event = {"body": json.dumps(_payload()), "headers": {}, "query_params": {}}

    handler(
        event,
        context=None,
        id_mapping_store=sentinel_store,
        notion_client=sentinel_notion_client,
    )

    assert captured["id_mapping_store"] is sentinel_store
    assert captured["notion_client"] is sentinel_notion_client


# --- BLOCKER5: 不正・欠損ペイロード時のエラーハンドリング -------------------------------


def test_handler_returns_400_for_malformed_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": "{not valid json", "headers": {}, "query_params": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_missing_required_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
        lambda: APP_ID_MAP,
    )
    payload = _payload()
    del payload["record"]["$id"]
    event = {"body": json.dumps(payload), "headers": {}, "query_params": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_unknown_app_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": json.dumps(_payload()), "headers": {}, "query_params": {}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
            lambda: {},
        )
        response = handler(event, context=None)

    assert response["statusCode"] == 400


# --- BLOCKER7: 共有シークレット検証 -----------------------------------------------------
# kintoneのWebhook設定画面はカスタムHTTPヘッダーを送信できないため（2026-08-14確認）、
# 共有シークレットはヘッダーではなくURLクエリパラメータ（?secret=...）で検証する。


def test_handler_returns_401_when_secret_mismatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KINTONE_WEBHOOK_SECRET", "correct-secret")
    event = {
        "body": json.dumps(_payload()),
        "headers": {},
        "query_params": {"secret": "wrong-secret"},
    }

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_secret_query_param_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KINTONE_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {}, "query_params": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_succeeds_when_secret_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KINTONE_WEBHOOK_SECRET", "correct-secret")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
        lambda: APP_ID_MAP,
    )
    event = {
        "body": json.dumps(_payload()),
        "headers": {},
        "query_params": {"secret": "correct-secret"},
    }

    response = handler(event, context=None)

    assert response["statusCode"] == 200
