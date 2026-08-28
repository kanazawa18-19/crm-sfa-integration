"""HttpNotionClientの単体テスト（実HTTP通信はrequests_mockでモック）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import requests

from src.db_schema.base import PropertyType
from src.sync_engine.clients import notion_client as notion_client_module
from src.db_schema.registry import get_schema
from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY
from src.sync_engine.clients.notion_client import (
    HttpNotionClient,
    NotionApiError,
    build_notion_properties,
    build_notion_property_value,
)

DB_KEY = "client_master"
DATABASE_ID = "26d6f1e2-1111-1111-1111-111111111111"
PAGE_ID = "26d6f1e2-0000-0000-0000-000000000000"


@pytest.fixture
def client() -> HttpNotionClient:
    return HttpNotionClient(DB_KEY, DATABASE_ID, api_key="secret-notion-key")


# --- 認証情報未設定時のエラー -------------------------------------------------------------------


def test_raises_value_error_when_api_key_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)

    with pytest.raises(ValueError, match="NOTION_API_KEY"):
        HttpNotionClient(DB_KEY, DATABASE_ID)


# --- get_page ----------------------------------------------------------------------------


def test_get_page_returns_flat_properties_dict(requests_mock, client: HttpNotionClient) -> None:
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        json={
            "id": PAGE_ID,
            "properties": {
                "取引先ID": {"type": "title", "title": [{"plain_text": "CLI-001"}]},
                "取引先名": {"type": "rich_text", "rich_text": [{"plain_text": "株式会社サンプル"}]},
                "顧客種別": {"type": "select", "select": {"name": "ホテル・旅館"}},
                "営業ステータス": {"type": "status", "status": {"name": "商談中"}},
                "チェーン": {"type": "relation", "relation": [{"id": "chain-1"}]},
            },
        },
    )

    record = client.get_page(PAGE_ID)

    assert record == {
        "取引先ID": "CLI-001",
        "取引先名": "株式会社サンプル",
        "顧客種別": "ホテル・旅館",
        "営業ステータス": "商談中",
        "チェーン": ["chain-1"],
    }


def test_get_page_includes_parsed_updated_at_from_last_edited_time(
    requests_mock, client: HttpNotionClient
) -> None:
    """05_同期・競合制御のコンフリクト判定がNotion側の実際の更新日時を参照できるよう、
    生レスポンスのトップレベル`last_edited_time`（`properties`とは別物）を
    `NOTION_LAST_EDITED_TIME_KEY`キーでdatetimeへ変換して合成することを確認する
    （従来はここが常に欠落しており、dispatcher.pyの`.get("updated_at", event.occurred_at)`が
    常にフォールバックへ落ちてしまっていたバグの回帰テスト）。
    """
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        json={
            "id": PAGE_ID,
            "last_edited_time": "2026-08-11T00:49:00.000Z",
            "properties": {
                "取引先ID": {"type": "title", "title": [{"plain_text": "CLI-001"}]},
            },
        },
    )

    record = client.get_page(PAGE_ID)

    assert record[NOTION_LAST_EDITED_TIME_KEY] == datetime(
        2026, 8, 11, 0, 49, 0, tzinfo=timezone.utc
    )
    assert record["取引先ID"] == "CLI-001"


def test_get_page_omits_updated_at_key_when_last_edited_time_missing(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        json={"id": PAGE_ID, "properties": {}},
    )

    record = client.get_page(PAGE_ID)

    assert NOTION_LAST_EDITED_TIME_KEY not in record


def test_get_page_skips_unparseable_property_types(
    requests_mock, client: HttpNotionClient, caplog: pytest.LogCaptureFixture
) -> None:
    """実運用で発生した`ValueError: unsupported Notion property type: 'formula'`の回帰テスト。

    案件管理DBの粗利/契約スピード等（FORMULA型）・添付ファイル（FILES型）のような
    parse_notion_property_value()未対応のプロパティが混在しても、get_page()全体は
    失敗せず、それらのプロパティだけが結果から単純に欠落する（キー自体が無い）。
    スキーマ/実データの型乖離（本番障害の原因）を見逃さないよう、warningレベルで
    ログに残ることも確認する（shirokuma-secレビューWARN対応: 以前はdebugレベルで
    実質見えなくなっていた）。
    """
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        json={
            "id": PAGE_ID,
            "properties": {
                "取引先ID": {"type": "title", "title": [{"plain_text": "CLI-001"}]},
                "顧客種別": {"type": "select", "select": {"name": "ホテル・旅館"}},
                "初期費用（イニシャル）": {"type": "number", "number": 500000},
                "粗利": {"type": "formula", "formula": {"type": "number", "number": 123456}},
                "契約スピード": {
                    "type": "rollup",
                    "rollup": {"type": "number", "number": 7},
                },
                "見積書": {
                    "type": "files",
                    "files": [{"name": "見積書.pdf", "type": "external"}],
                },
                "作成日時": {"type": "created_time", "created_time": "2026-08-05T09:00:00.000Z"},
            },
        },
    )

    with caplog.at_level("WARNING"):
        record = client.get_page(PAGE_ID)

    assert record == {
        "取引先ID": "CLI-001",
        "顧客種別": "ホテル・旅館",
        "初期費用（イニシャル）": 500000,
    }
    assert "粗利" not in record
    assert "契約スピード" not in record
    assert "見積書" not in record
    assert "作成日時" not in record
    for skipped_property in ("粗利", "契約スピード", "見積書", "作成日時"):
        assert any(
            log_record.levelname == "WARNING" and skipped_property in log_record.getMessage()
            for log_record in caplog.records
        )


def test_get_page_returns_none_on_404(requests_mock, client: HttpNotionClient) -> None:
    requests_mock.get(f"https://api.notion.com/v1/pages/{PAGE_ID}", status_code=404)

    assert client.get_page(PAGE_ID) is None


def test_get_page_sends_bearer_token_and_notion_version_header(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}", json={"id": PAGE_ID, "properties": {}}
    )

    client.get_page(PAGE_ID)

    sent_headers = requests_mock.last_request.headers
    assert sent_headers["Authorization"] == "Bearer secret-notion-key"
    assert sent_headers["Notion-Version"] == "2022-06-28"


def test_get_page_raises_notion_api_error_on_5xx(
    requests_mock, client: HttpNotionClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        status_code=500,
        json={"message": "internal error"},
    )

    with pytest.raises(NotionApiError) as exc_info:
        client.get_page(PAGE_ID)
    assert exc_info.value.status_code == 500


def test_get_page_raises_notion_api_error_on_200_when_properties_is_not_a_dict(
    requests_mock, client: HttpNotionClient
) -> None:
    """shirokuma-secレビューBLOCKER対応（2026-08-28）: HTTP 200だが`properties`が想定した
    辞書形式ではない異常応答の場合、生のAttributeErrorではなく正規化されたNotionApiErrorに
    なることを確認する。"""
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        status_code=200,
        json={"id": PAGE_ID, "properties": "not-a-dict"},
    )

    with pytest.raises(NotionApiError) as exc_info:
        client.get_page(PAGE_ID)
    assert exc_info.value.status_code == 200


def test_get_page_raises_notion_api_error_on_200_when_property_value_is_not_a_dict(
    requests_mock, client: HttpNotionClient
) -> None:
    """個々のプロパティ値が辞書でない異常応答の場合も同様に正規化されること。"""
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        status_code=200,
        json={"id": PAGE_ID, "properties": {"取引先ID": "not-a-dict"}},
    )

    with pytest.raises(NotionApiError):
        client.get_page(PAGE_ID)


# --- get_raw_page -------------------------------------------------------------------------


def test_get_raw_page_returns_response_json_unmodified(
    requests_mock, client: HttpNotionClient
) -> None:
    raw_page = {
        "id": PAGE_ID,
        "parent": {"type": "database_id", "database_id": DATABASE_ID},
        "last_edited_time": "2026-08-05T09:00:00.000Z",
        "properties": {
            "取引先ID": {"type": "title", "title": [{"plain_text": "CLI-001"}]},
        },
    }
    requests_mock.get(f"https://api.notion.com/v1/pages/{PAGE_ID}", json=raw_page)

    assert client.get_raw_page(PAGE_ID) == raw_page


def test_get_raw_page_raises_notion_api_error_on_404(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        status_code=404,
        json={"message": "not found"},
    )

    with pytest.raises(NotionApiError) as exc_info:
        client.get_raw_page(PAGE_ID)
    assert exc_info.value.status_code == 404


def test_get_raw_page_raises_notion_api_error_on_5xx(
    requests_mock, client: HttpNotionClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        status_code=500,
        json={"message": "internal error"},
    )

    with pytest.raises(NotionApiError) as exc_info:
        client.get_raw_page(PAGE_ID)
    assert exc_info.value.status_code == 500


def test_get_raw_page_sends_bearer_token_and_notion_version_header(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}", json={"id": PAGE_ID, "properties": {}}
    )

    client.get_raw_page(PAGE_ID)

    sent_headers = requests_mock.last_request.headers
    assert sent_headers["Authorization"] == "Bearer secret-notion-key"
    assert sent_headers["Notion-Version"] == "2022-06-28"


# --- create_page ---------------------------------------------------------------------------


def test_create_page_sends_correct_body_and_returns_id(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.post("https://api.notion.com/v1/pages", json={"id": "new-page-id"})

    page_id = client.create_page({"取引先名": "株式会社サンプル", "顧客種別": "宿泊施設"})

    assert page_id == "new-page-id"
    sent_body = requests_mock.last_request.json()
    assert sent_body["parent"] == {"database_id": DATABASE_ID}
    assert sent_body["properties"]["取引先名"] == {
        "title": [{"type": "text", "text": {"content": "株式会社サンプル"}}]
    }
    assert sent_body["properties"]["顧客種別"] == {"select": {"name": "宿泊施設"}}


def test_create_page_does_not_retry_on_5xx(
    requests_mock, client: HttpNotionClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WARN対応: 作成系（非冪等）操作はサーバー側で処理済みの可能性があるため、
    5xxでもリトライせず即座にエラーとして返す（重複ページ作成を避ける）。
    """
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.post(
        "https://api.notion.com/v1/pages", status_code=500, json={"message": "internal error"}
    )

    with pytest.raises(NotionApiError):
        client.create_page({"取引先名": "株式会社サンプル"})

    assert requests_mock.call_count == 1


def test_create_page_raises_notion_api_error_on_200_missing_id_key(
    requests_mock, client: HttpNotionClient
) -> None:
    """shirokuma-secレビューWARN対応（2026-08-27）: 200応答で`id`キー自体を欠く想定外の
    ボディ形状でも、生のKeyErrorではなくNotionApiErrorへ正規化されること。"""
    requests_mock.post(
        "https://api.notion.com/v1/pages", status_code=200, json={"unexpected": "shape"}
    )

    with pytest.raises(NotionApiError):
        client.create_page({"取引先名": "株式会社サンプル"})


# --- update_page ---------------------------------------------------------------------------


def test_update_page_sends_patch_with_properties(requests_mock, client: HttpNotionClient) -> None:
    requests_mock.patch(f"https://api.notion.com/v1/pages/{PAGE_ID}", json={"id": PAGE_ID})

    client.update_page(PAGE_ID, {"住所": "更新後の住所"})

    sent_body = requests_mock.last_request.json()
    assert sent_body == {
        "properties": {
            "住所": {"rich_text": [{"type": "text", "text": {"content": "更新後の住所"}}]}
        }
    }
    assert "parent" not in sent_body


def test_update_page_raises_notion_api_error_on_400(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.patch(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        status_code=400,
        json={"message": "validation failed"},
    )

    with pytest.raises(NotionApiError):
        client.update_page(PAGE_ID, {"取引先名": "更新後の名称"})


# --- archive_page --------------------------------------------------------------------------


def test_archive_page_sends_patch_with_archived_true(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.patch(f"https://api.notion.com/v1/pages/{PAGE_ID}", json={"id": PAGE_ID})

    client.archive_page(PAGE_ID)

    assert requests_mock.last_request.json() == {"archived": True}


# --- タイムアウト・リトライ ------------------------------------------------------------------


def test_get_page_retries_on_429_then_succeeds(
    requests_mock, client: HttpNotionClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        [
            {"status_code": 429},
            {"json": {"id": PAGE_ID, "properties": {}}, "status_code": 200},
        ],
    )

    record = client.get_page(PAGE_ID)

    assert record == {}
    assert requests_mock.call_count == 2


def test_max_rate_limit_retries_is_honored(
    requests_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """shirokuma-secレビューWARN対応の回帰テスト。

    `HttpNotionClient`の`max_rate_limit_retries`引数が実際に`request_with_retry()`へ渡され、
    指定した回数でリトライを打ち切ることを固定化する。この引数が無い/伝播しないと、
    ダッシュボード/タスクAPIのような対話的な呼び出し元が`INTERACTIVE_MAX_RATE_LIMIT_RETRIES`
    （小さい値）を指定しても効かず、移行スクリプト向けの既定値（30回・最大30秒/回）が
    そのまま使われ、Notionが移行処理でレート制限されている最中に通常のリクエストが
    最悪15分近くブロックされる恐れがある。
    """
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    client = HttpNotionClient(
        DB_KEY, DATABASE_ID, api_key="secret-notion-key", max_rate_limit_retries=1
    )
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        [{"status_code": 429}, {"status_code": 429}, {"status_code": 429}],
    )

    with pytest.raises(NotionApiError):
        client.get_page(PAGE_ID)

    assert requests_mock.call_count == 2


# --- プロパティ形式の相互変換ロジック（内部値 -> Notion形式） --------------------------------


@pytest.mark.parametrize(
    ("property_type", "value", "expected"),
    [
        (PropertyType.TITLE, "サンプル", {"title": [{"type": "text", "text": {"content": "サンプル"}}]}),
        (PropertyType.TITLE, None, {"title": []}),
        (PropertyType.TEXT, "本文", {"rich_text": [{"type": "text", "text": {"content": "本文"}}]}),
        (PropertyType.SELECT, "A", {"select": {"name": "A"}}),
        (PropertyType.SELECT, None, {"select": None}),
        (PropertyType.STATUS, "提案中", {"status": {"name": "提案中"}}),
        (
            PropertyType.MULTI_SELECT,
            ["リピッテ", "メイリー"],
            {"multi_select": [{"name": "リピッテ"}, {"name": "メイリー"}]},
        ),
        (PropertyType.MULTI_SELECT, None, {"multi_select": []}),
        (PropertyType.NUMBER, 500000, {"number": 500000}),
        (PropertyType.CURRENCY, 1000, {"number": 1000}),
        (PropertyType.DATE, "2026-08-05", {"date": {"start": "2026-08-05"}}),
        (PropertyType.DATE, None, {"date": None}),
        (PropertyType.EMAIL, "a@example.com", {"email": "a@example.com"}),
        (PropertyType.PHONE, "03-1234-5678", {"phone_number": "03-1234-5678"}),
        (PropertyType.URL, "https://example.com", {"url": "https://example.com"}),
        (PropertyType.CHECKBOX, True, {"checkbox": True}),
        (PropertyType.USER, ["user-1", "user-2"], {"people": [{"id": "user-1"}, {"id": "user-2"}]}),
        (PropertyType.USER, "user-1", {"people": [{"id": "user-1"}]}),
        (PropertyType.RELATION, ["rel-1"], {"relation": [{"id": "rel-1"}]}),
        (PropertyType.RELATION, None, {"relation": []}),
        (PropertyType.JSON_TEXT, '{"k": 1}', {"rich_text": [{"type": "text", "text": {"content": '{"k": 1}'}}]}),
        (
            PropertyType.FILES,
            [{"name": "見積書.pdf", "url": "https://drive.google.com/file/d/abc/view"}],
            {
                "files": [
                    {
                        "type": "external",
                        "name": "見積書.pdf",
                        "external": {"url": "https://drive.google.com/file/d/abc/view"},
                    }
                ]
            },
        ),
        (PropertyType.FILES, None, {"files": []}),
    ],
)
def test_build_notion_property_value(property_type: PropertyType, value, expected) -> None:
    assert build_notion_property_value(property_type, value) == expected


def test_build_notion_property_value_rejects_unsupported_type() -> None:
    with pytest.raises(ValueError):
        build_notion_property_value("not_a_real_type", "x")  # type: ignore[arg-type]


def test_build_notion_properties_uses_schema_to_determine_type() -> None:
    schema = get_schema(DB_KEY)

    result = build_notion_properties({"取引先名": "株式会社サンプル", "顧客種別": "宿泊施設"}, schema)

    assert result["取引先名"] == {"title": [{"type": "text", "text": {"content": "株式会社サンプル"}}]}
    assert result["顧客種別"] == {"select": {"name": "宿泊施設"}}


def test_build_notion_properties_raises_key_error_for_unknown_property() -> None:
    schema = get_schema(DB_KEY)

    with pytest.raises(KeyError):
        build_notion_properties({"存在しないプロパティ": "x"}, schema)


# --- create_page の作成後回収（2026-08-28、external_id=62172の実例対応） -----------------------


def test_create_page_recovers_page_id_when_response_times_out(
    requests_mock, client: HttpNotionClient
) -> None:
    """レスポンスが返る前にタイムアウトしても、直前に作られたページを1件だけ特定できれば
    そのIDを返すこと（IdMapping未登録による2枚目の作成を防ぐ）。"""
    requests_mock.post(
        "https://api.notion.com/v1/pages", exc=requests.exceptions.ReadTimeout("read timed out")
    )
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={"results": [{"id": "recovered-page-id"}]},
    )

    page_id = client.create_page({"取引先名": "株式会社サンプル"})

    assert page_id == "recovered-page-id"
    # 照会はタイトル完全一致＋作成時刻の窓のANDであること。
    query_body = requests_mock.request_history[-1].json()
    conditions = query_body["filter"]["and"]
    assert {"property": "取引先名", "title": {"equals": "株式会社サンプル"}} in conditions
    assert any(c.get("timestamp") == "created_time" for c in conditions)


def test_create_page_raises_when_no_page_was_created(
    requests_mock, client: HttpNotionClient
) -> None:
    """回収照会が0件（＝そもそも作られていない）なら、元の例外をそのまま伝播させること。"""
    requests_mock.post(
        "https://api.notion.com/v1/pages", exc=requests.exceptions.ReadTimeout("read timed out")
    )
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query", json={"results": []}
    )

    with pytest.raises(requests.exceptions.ReadTimeout):
        client.create_page({"取引先名": "株式会社サンプル"})


def test_create_page_does_not_recover_when_multiple_pages_match(
    requests_mock, client: HttpNotionClient
) -> None:
    """同名ページが複数あるときは回収しないこと。

    どれが今作ったものか判別できず、無関係なページを掴むとIdMappingが誤って結び付き、
    以後の同期で互いの値を上書きし合う。回収できないこと（人が目視で確認する）より、
    間違ったページを掴むことの方が明確に有害。
    """
    requests_mock.post(
        "https://api.notion.com/v1/pages", exc=requests.exceptions.ReadTimeout("read timed out")
    )
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={"results": [{"id": "page-a"}, {"id": "page-b"}]},
    )

    with pytest.raises(requests.exceptions.ReadTimeout):
        client.create_page({"取引先名": "株式会社サンプル"})


def test_create_page_raises_original_error_when_recovery_query_also_fails(
    requests_mock, client: HttpNotionClient
) -> None:
    """回収照会自体が失敗しても、新しい例外で元の失敗を覆い隠さないこと。"""
    requests_mock.post(
        "https://api.notion.com/v1/pages", exc=requests.exceptions.ReadTimeout("read timed out")
    )
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        status_code=500,
        json={"message": "internal error"},
    )

    with pytest.raises(requests.exceptions.ReadTimeout):
        client.create_page({"取引先名": "株式会社サンプル"})


def test_create_page_uses_longer_timeout_than_the_default(
    requests_mock, client: HttpNotionClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ページ作成だけは既定(10秒)より長いタイムアウトで送ること。"""
    captured: dict[str, float] = {}
    original = notion_client_module.request_with_retry

    def spy(*args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return original(*args, **kwargs)

    monkeypatch.setattr(notion_client_module, "request_with_retry", spy)
    requests_mock.post("https://api.notion.com/v1/pages", json={"id": "new-page-id"})

    client.create_page({"取引先名": "株式会社サンプル"})

    assert captured["timeout"] > 10.0
