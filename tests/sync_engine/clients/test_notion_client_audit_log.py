"""HttpNotionClient.create_page/update_pageの監査ログフック(src/audit_log/)の単体テスト。

record_notion_write()自体の差分抽出・DB書き込みロジックはtests/audit_log/test_recorder.pyで
検証済みのため、ここでは「HttpNotionClient側がrecord_notion_write()へ正しい引数
（before/after/db_key/notion_page_id/action）を渡すか」「update_page直前のGET失敗時に本来の
PATCH処理を妨げないか」のみを検証する（record_notion_write自体はモックする）。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.sync_engine.clients import notion_client as notion_client_module
from src.sync_engine.clients.notion_client import HttpNotionClient

DB_KEY = "client_master"
DATABASE_ID = "26d6f1e2-1111-1111-1111-111111111111"
PAGE_ID = "26d6f1e2-0000-0000-0000-000000000000"


@pytest.fixture
def client() -> HttpNotionClient:
    return HttpNotionClient(DB_KEY, DATABASE_ID, api_key="secret-notion-key")


class _RecordedCall:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture
def recorded_calls(monkeypatch: pytest.MonkeyPatch) -> list[_RecordedCall]:
    calls: list[_RecordedCall] = []

    def _fake_record_notion_write(**kwargs: Any) -> None:
        calls.append(_RecordedCall(**kwargs))

    monkeypatch.setattr(notion_client_module, "record_notion_write", _fake_record_notion_write)
    return calls


def test_create_page_records_write_with_before_none(
    requests_mock, client: HttpNotionClient, recorded_calls: list[_RecordedCall]
) -> None:
    requests_mock.post("https://api.notion.com/v1/pages", json={"id": "new-page-id"})

    client.create_page({"取引先名": "株式会社サンプル"})

    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    assert call.kwargs["db_key"] == DB_KEY
    assert call.kwargs["notion_page_id"] == "new-page-id"
    assert call.kwargs["action"] == "create"
    assert call.kwargs["before"] is None
    assert call.kwargs["after"] == {"取引先名": "株式会社サンプル"}


def test_update_page_fetches_current_values_and_records_before_after(
    requests_mock, client: HttpNotionClient, recorded_calls: list[_RecordedCall]
) -> None:
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        json={
            "id": PAGE_ID,
            "properties": {
                "住所": {"type": "rich_text", "rich_text": [{"plain_text": "旧住所"}]},
            },
        },
    )
    requests_mock.patch(f"https://api.notion.com/v1/pages/{PAGE_ID}", json={"id": PAGE_ID})

    client.update_page(PAGE_ID, {"住所": "更新後の住所"})

    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    assert call.kwargs["db_key"] == DB_KEY
    assert call.kwargs["notion_page_id"] == PAGE_ID
    assert call.kwargs["action"] == "update"
    assert call.kwargs["before"] == {"住所": "旧住所"}
    assert call.kwargs["after"] == {"住所": "更新後の住所"}


def test_update_page_still_patches_when_pre_read_get_fails(
    requests_mock, client: HttpNotionClient, recorded_calls: list[_RecordedCall]
) -> None:
    """更新前のGET（監査ログ用の現在値取得）が失敗しても、本来のPATCH処理自体は
    実行されること（監査ログはあくまで副次的な記録という設計方針）。GETをモック
    登録していないため、requests_mockが例外を送出する。"""
    requests_mock.patch(f"https://api.notion.com/v1/pages/{PAGE_ID}", json={"id": PAGE_ID})

    client.update_page(PAGE_ID, {"住所": "更新後の住所"})

    sent_body = requests_mock.last_request.json()
    assert sent_body == {
        "properties": {"住所": {"rich_text": [{"type": "text", "text": {"content": "更新後の住所"}}]}}
    }
    # 現在値取得に失敗しているため、record_notion_writeにはbefore=Noneで渡る
    # （recorder.record_notion_write側で記録自体をスキップする設計、test_recorder.py参照）。
    assert len(recorded_calls) == 1
    assert recorded_calls[0].kwargs["before"] is None


def test_update_page_does_not_record_when_get_page_returns_none(
    requests_mock, client: HttpNotionClient, recorded_calls: list[_RecordedCall]
) -> None:
    """対象ページが404（削除済み等）の場合、get_page()はNoneを返す
    （HttpNotionClient.get_page()の既存仕様）。この場合もbefore=Noneとして渡す。"""
    requests_mock.get(f"https://api.notion.com/v1/pages/{PAGE_ID}", status_code=404)
    requests_mock.patch(f"https://api.notion.com/v1/pages/{PAGE_ID}", json={"id": PAGE_ID})

    client.update_page(PAGE_ID, {"住所": "更新後の住所"})

    assert recorded_calls[0].kwargs["before"] is None


def test_update_page_does_not_record_when_patch_itself_fails(
    requests_mock, client: HttpNotionClient, recorded_calls: list[_RecordedCall]
) -> None:
    from src.sync_engine.clients.notion_client import NotionApiError

    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}", json={"id": PAGE_ID, "properties": {}}
    )
    requests_mock.patch(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        status_code=400,
        json={"message": "validation failed"},
    )

    with pytest.raises(NotionApiError):
        client.update_page(PAGE_ID, {"住所": "更新後の住所"})

    # PATCH自体が失敗した場合、record_notion_write()は呼ばれない
    # （実際には更新されていないため、監査ログを残さない）。
    assert recorded_calls == []


# --- RELATION/USER型プロパティの表示名解決（obasan-qualityレビューWARN対応） ----------------


def test_create_page_resolves_relation_property_to_target_title(
    requests_mock,
    client: HttpNotionClient,
    recorded_calls: list[_RecordedCall],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # notion_display_resolver.py内部で参照先DB("chain")用に新規HttpNotionClientを構築する
    # 際、NOTION_API_KEY環境変数からapi_keyを読む（本fixtureのclientはapi_keyを直接
    # 渡しているため、この環境変数は別途必要）。
    monkeypatch.setenv("NOTION_API_KEY", "secret-notion-key")
    requests_mock.post("https://api.notion.com/v1/pages", json={"id": "new-page-id"})
    requests_mock.get(
        "https://api.notion.com/v1/pages/chain-page-1",
        json={
            "id": "chain-page-1",
            "properties": {"グループ名": {"type": "title", "title": [{"plain_text": "サンプルチェーン"}]}},
        },
    )

    client.create_page({"取引先名": "株式会社サンプル", "チェーン": ["chain-page-1"]})

    assert recorded_calls[0].kwargs["after"] == {
        "取引先名": "株式会社サンプル",
        "チェーン": ["サンプルチェーン"],
    }
    # 解決に使うのはrecord_notion_write()へ渡す表示用の値のみで、Notion APIへ実際に
    # 送信するPATCH/POSTボディの生のページIDには影響しない。
    sent_relation = requests_mock.request_history[0].json()["properties"]["チェーン"]
    assert sent_relation == {"relation": [{"id": "chain-page-1"}]}


def test_create_page_skips_relation_resolution_when_actor_is_migration(
    requests_mock, client: HttpNotionClient, recorded_calls: list[_RecordedCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.audit_log.actor_context import set_actor

    requests_mock.post("https://api.notion.com/v1/pages", json={"id": "new-page-id"})

    with set_actor("migration"):
        client.create_page({"チェーン": ["chain-page-1"]})

    assert recorded_calls[0].kwargs["after"] == {"チェーン": ["chain-page-1"]}
    # チェーンDB側への追加GETが発生していないこと（POST /pages 1回のみ）。
    assert requests_mock.call_count == 1
