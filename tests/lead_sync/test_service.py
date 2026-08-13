"""sync_contact_to_leadの単体テスト。

`lead_sync_client`にはフェイクのスタブを注入する。`notion_client`にも「取引先マスター」relation
先ページ取得用のフェイクを注入する（実Notion APIへは一切アクセスしない）。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.lead_sync import service as service_module
from src.lead_sync.service import sync_contact_to_lead


class _FakeLeadSyncClient:
    """upsert_lead()に渡された引数を記録するフェイクの`lead_sync_client`。"""

    def __init__(self, return_value: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._return_value = return_value if return_value is not None else {
            "token": "tok-1",
            "lead_id": "lead-1",
        }

    def upsert_lead(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._return_value


class _FakeNotionPageClient:
    """`取引先マスター`relation先ページ取得用のフェイク。"""

    def __init__(self, pages_by_id: dict[str, dict[str, Any]] | None = None) -> None:
        self._pages_by_id = pages_by_id or {}

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        if page_id not in self._pages_by_id:
            raise RuntimeError(f"notion api unavailable for page_id={page_id!r}")
        return self._pages_by_id[page_id]


NOTION_PAGE_ID = "cnt-0000-0000-0000-000000000000"
CLIENT_MASTER_PAGE_ID = "clt-0000-0000-0000-000000000000"


def _properties(**overrides: Any) -> dict[str, Any]:
    base = {
        "名前": "山田太郎",
        "メールアドレス": "yamada@example.com",
        "取引先マスター": [CLIENT_MASTER_PAGE_ID],
        "携帯番号": "090-0000-0000",
        "直通TEL": "03-0000-0000",
    }
    base.update(overrides)
    return base


def _client_master_page(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": CLIENT_MASTER_PAGE_ID,
        "properties": {
            "取引先名": {"type": "title", "title": [{"plain_text": "株式会社サンプル"}]},
        },
    }
    base.update(overrides)
    return base


# --- email: 同期スキップ条件 -----------------------------------------------------------------


def test_returns_none_when_email_key_missing() -> None:
    properties = _properties()
    del properties["メールアドレス"]
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient()

    result = sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert result is None
    assert lead_sync_client.calls == []


def test_logs_debug_when_email_missing_so_skip_is_greppable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """obasan-qualityレビューWARN対応: メールアドレス未設定によるスキップは正常系だが、
    debugログでnotion_page_idが確認できることを検証する。"""
    properties = _properties()
    del properties["メールアドレス"]
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient()

    with caplog.at_level("DEBUG", logger="src.lead_sync.service"):
        sync_contact_to_lead(
            properties,
            NOTION_PAGE_ID,
            notion_client=notion_client,
            lead_sync_client=lead_sync_client,
        )

    assert any(NOTION_PAGE_ID in record.getMessage() for record in caplog.records)


def test_returns_none_when_email_is_empty_string() -> None:
    properties = _properties(**{"メールアドレス": ""})
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient()

    result = sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert result is None
    assert lead_sync_client.calls == []


def test_returns_none_when_email_is_none() -> None:
    properties = _properties(**{"メールアドレス": None})
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient()

    result = sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert result is None
    assert lead_sync_client.calls == []


# --- last_name: 分割せずそのまま渡す・first_nameは送らない ------------------------------------


def test_uses_whole_name_as_last_name_without_splitting() -> None:
    properties = _properties()
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient({CLIENT_MASTER_PAGE_ID: _client_master_page()})

    sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert lead_sync_client.calls[0]["last_name"] == "山田太郎"
    assert "first_name" not in lead_sync_client.calls[0]


# --- company: 取引先マスターrelation解決 -------------------------------------------------------


def test_resolves_company_from_first_related_client_master_page() -> None:
    properties = _properties()
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient({CLIENT_MASTER_PAGE_ID: _client_master_page()})

    sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert lead_sync_client.calls[0]["company"] == "株式会社サンプル"


def test_uses_only_first_related_company_when_multiple_present() -> None:
    second_page_id = "clt-1111-1111-1111-111111111111"
    properties = _properties(**{"取引先マスター": [CLIENT_MASTER_PAGE_ID, second_page_id]})
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient(
        {
            CLIENT_MASTER_PAGE_ID: _client_master_page(),
            second_page_id: _client_master_page(
                properties={
                    "取引先名": {"type": "title", "title": [{"plain_text": "別会社"}]}
                }
            ),
        }
    )

    sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert lead_sync_client.calls[0]["company"] == "株式会社サンプル"


def test_omits_company_when_relation_is_empty_list() -> None:
    properties = _properties(**{"取引先マスター": []})
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient()

    sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert "company" not in lead_sync_client.calls[0]


def test_omits_company_when_relation_key_missing() -> None:
    properties = _properties()
    del properties["取引先マスター"]
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient()

    sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert "company" not in lead_sync_client.calls[0]


def test_omits_company_when_fetch_fails() -> None:
    properties = _properties()
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient({})  # get_raw_page raises for any id

    result = sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert result is not None
    assert "company" not in lead_sync_client.calls[0]


def test_logs_warning_when_company_resolution_fails(caplog: pytest.LogCaptureFixture) -> None:
    """obasan-qualityレビューWARN対応: 会社名解決の失敗（Notion API呼び出し失敗）が
    無音で握りつぶされず、related_page_idと例外の型名を含むwarningログを残すことを検証する。"""
    properties = _properties()
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient({})  # get_raw_page raises for any id

    with caplog.at_level("WARNING", logger="src.lead_sync.service"):
        sync_contact_to_lead(
            properties,
            NOTION_PAGE_ID,
            notion_client=notion_client,
            lead_sync_client=lead_sync_client,
        )

    assert any(CLIENT_MASTER_PAGE_ID in record.getMessage() for record in caplog.records)
    assert any("RuntimeError" in record.getMessage() for record in caplog.records)


def test_omits_company_when_related_page_has_no_title_property() -> None:
    properties = _properties()
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient(
        {CLIENT_MASTER_PAGE_ID: _client_master_page(properties={})}
    )

    sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert "company" not in lead_sync_client.calls[0]


# --- phone: 携帯番号優先、直通TELへフォールバック ----------------------------------------------


def test_prefers_mobile_phone_over_direct_line() -> None:
    properties = _properties()
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient({CLIENT_MASTER_PAGE_ID: _client_master_page()})

    sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert lead_sync_client.calls[0]["phone"] == "090-0000-0000"


def test_falls_back_to_direct_line_when_mobile_phone_missing() -> None:
    properties = _properties(**{"携帯番号": None})
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient({CLIENT_MASTER_PAGE_ID: _client_master_page()})

    sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert lead_sync_client.calls[0]["phone"] == "03-0000-0000"


def test_omits_phone_when_both_mobile_and_direct_line_missing() -> None:
    properties = _properties(**{"携帯番号": None, "直通TEL": None})
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient({CLIENT_MASTER_PAGE_ID: _client_master_page()})

    sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert "phone" not in lead_sync_client.calls[0]


# --- assigned_rep_email: 常に省略 --------------------------------------------------------------


def test_never_sends_assigned_rep_email() -> None:
    properties = _properties()
    lead_sync_client = _FakeLeadSyncClient()
    notion_client = _FakeNotionPageClient({CLIENT_MASTER_PAGE_ID: _client_master_page()})

    sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert "assigned_rep_email" not in lead_sync_client.calls[0]


# --- 正常系全体 -------------------------------------------------------------------------------


def test_calls_upsert_lead_with_expected_arguments_and_returns_result() -> None:
    properties = _properties()
    lead_sync_client = _FakeLeadSyncClient({"token": "tok-1", "lead_id": "lead-1"})
    notion_client = _FakeNotionPageClient({CLIENT_MASTER_PAGE_ID: _client_master_page()})

    result = sync_contact_to_lead(
        properties, NOTION_PAGE_ID, notion_client=notion_client, lead_sync_client=lead_sync_client
    )

    assert result == {"token": "tok-1", "lead_id": "lead-1"}
    assert lead_sync_client.calls == [
        {
            "email": "yamada@example.com",
            "company": "株式会社サンプル",
            "last_name": "山田太郎",
            "phone": "090-0000-0000",
        }
    ]


# --- 例外は握りつぶさず伝播させる（呼び出し元のwebhookハンドラ層の責務） --------------------------


def test_propagates_exception_raised_by_lead_sync_client() -> None:
    class _FailingLeadSyncClient:
        def upsert_lead(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("lead sync api boom")

    properties = _properties()
    notion_client = _FakeNotionPageClient({CLIENT_MASTER_PAGE_ID: _client_master_page()})

    with pytest.raises(RuntimeError, match="lead sync api boom"):
        sync_contact_to_lead(
            properties,
            NOTION_PAGE_ID,
            notion_client=notion_client,
            lead_sync_client=_FailingLeadSyncClient(),
        )


# --- lead_sync_client省略時のデフォルト構築 ----------------------------------------------------


def test_defaults_to_constructing_web_engagement_tool_lead_sync_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[Any] = []

    class _StubClient:
        def upsert_lead(self, **kwargs: Any) -> dict[str, Any]:
            return {"token": "tok-1", "lead_id": "lead-1"}

    def _stub_constructor() -> _StubClient:
        stub = _StubClient()
        constructed.append(stub)
        return stub

    monkeypatch.setattr(
        service_module, "WebEngagementToolLeadSyncClient", _stub_constructor
    )
    notion_client = _FakeNotionPageClient({CLIENT_MASTER_PAGE_ID: _client_master_page()})

    result = sync_contact_to_lead(_properties(), NOTION_PAGE_ID, notion_client=notion_client)

    assert result == {"token": "tok-1", "lead_id": "lead-1"}
    assert len(constructed) == 1
