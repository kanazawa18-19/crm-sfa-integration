from __future__ import annotations

from typing import Any

import requests

from src.api.client_360_service import (
    PROP_取引先マスター_ACTION,
    STATUS_取引先名_解決済み,
    STATUS_取引先名_取得失敗,
    STATUS_取引先名_未取得,
    Client360DataSource,
    _MAX_CLIENT_NAME_LOOKUPS,
    get_client_360,
    search_clients,
    search_contacts,
)
from src.sync_engine.clients.notion_client import NotionApiError


class _FakeUserDirectory:
    def resolve(self, user_id: str) -> str:
        return f"resolved:{user_id}"

    def resolve_many(self, user_ids: list[str]) -> list[str]:
        return [self.resolve(uid) for uid in user_ids]


class _FakeQueryClient:
    """`query_page(filter=..., page_size=...)`のみを持つテスト用スタブ。

    実際に呼ばれたfilter/page_sizeを記録し、テストから検証できるようにする。
    """

    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        self._pages = pages or []
        self.calls: list[dict[str, Any]] = []

    def query_page(
        self, *, page_size: int = 100, filter: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append({"page_size": page_size, "filter": filter})
        return self._pages


class _FakeClientMasterClient(_FakeQueryClient):
    """取引先マスターDB用。`get_raw_page`も持つ（get_client_360で使用）。

    `get_raw_page`の呼び出しをclient_id毎に記録する（`search_contacts`のメモ化を
    検証するため）。
    """

    def __init__(
        self,
        pages: list[dict[str, Any]] | None = None,
        *,
        raw_pages: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(pages)
        self._raw_pages = raw_pages or {}
        self.get_raw_page_calls: list[str] = []

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        self.get_raw_page_calls.append(page_id)
        if page_id not in self._raw_pages:
            raise NotionApiError(404, "not found")
        return self._raw_pages[page_id]


class _RequestExceptionClientMasterClient(_FakeClientMasterClient):
    """`get_raw_page`がHTTPリクエスト自体の失敗（タイムアウト・接続断等）を模したスタブ。

    `NotionApiError`ではなく`requests.exceptions.RequestException`を投げる
    （Notion側のエラーレスポンスに正規化される前の失敗、WARN対応の検証用）。
    """

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        self.get_raw_page_calls.append(page_id)
        raise requests.exceptions.Timeout("timed out")


def _client_master_page(page_id: str = "cli-1", name: str = "サンプルホテル") -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "取引先名": {"type": "title", "title": [{"plain_text": name}]},
        },
    }


def _project_page(page_id: str = "proj-1", name: str = "サンプル案件") -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "案件名": {"type": "title", "title": [{"plain_text": name}]},
            "担当メンバー": {"type": "people", "people": []},
        },
    }


def _contact_page(page_id: str = "cnt-1", name: str = "山田太郎") -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "名前": {"type": "title", "title": [{"plain_text": name}]},
        },
    }


def _contact_page_with_client(
    page_id: str = "cnt-1", name: str = "山田太郎", client_ids: list[str] | None = None
) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "名前": {"type": "title", "title": [{"plain_text": name}]},
            "取引先マスター": {
                "type": "relation",
                "relation": [{"id": cid} for cid in (client_ids or [])],
            },
        },
    }


def _action_page(page_id: str = "act-1", title: str = "【電話】1回目") -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "商談回数・電話回数・メール回数（何回目）": {
                "type": "title",
                "title": [{"plain_text": title}],
            },
            "担当営業": {
                "type": "rollup",
                "rollup": {
                    "type": "array",
                    "array": [
                        {
                            "type": "people",
                            "people": [{"id": "user-1", "name": "田中太郎"}],
                        }
                    ],
                },
            },
        },
    }


class _FakeReplyTimingBuilder:
    """返信傾向の取得(EmailLog/Postgres)を差し替えるスタブ。

    差し替え口が無いと、テストではDB接続の例外が握り潰されて素通りしてしまい、
    360ビューに返信傾向が載っているかを一切検証できない。
    """

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or {}
        self.calls: list[list[str]] = []

    def __call__(self, page_ids: list[str]) -> dict[str, Any]:
        self.calls.append(list(page_ids))
        return self.result


def _data_source(
    *,
    client_master_client: Any | None = None,
    contact_client: Any | None = None,
    project_client: Any | None = None,
    action_client: Any | None = None,
    reply_timing_builder: Any | None = None,
) -> Client360DataSource:
    return Client360DataSource(
        client_master_client=client_master_client or _FakeClientMasterClient(),
        contact_client=contact_client or _FakeQueryClient(),
        project_client=project_client or _FakeQueryClient(),
        action_client=action_client or _FakeQueryClient(),
        user_directory=_FakeUserDirectory(),
        reply_timing_builder=reply_timing_builder or _FakeReplyTimingBuilder(),
    )


# --- search_clients --------------------------------------------------------------------------


def test_search_clients_returns_empty_for_blank_query() -> None:
    data_source = _data_source()

    result = search_clients("", data_source=data_source)

    assert result == {"clients": [], "truncated": False}


def test_search_clients_sends_title_contains_filter_to_notion_api() -> None:
    client_master_client = _FakeClientMasterClient(pages=[_client_master_page()])
    data_source = _data_source(client_master_client=client_master_client)

    result = search_clients("サンプル", data_source=data_source)

    assert client_master_client.calls == [
        {"page_size": 21, "filter": {"property": "取引先名", "title": {"contains": "サンプル"}}}
    ]
    assert result["clients"] == [{"notion_page_id": "cli-1", "取引先名": "サンプルホテル"}]
    assert result["truncated"] is False


def test_search_clients_reports_truncated_when_more_than_max_results() -> None:
    # page_size=21件request時に21件返ってきた場合、実際には21件以上一致した
    # 可能性がある(query_page()は1回のクエリで打ち切るため、真の一致件数は
    # 分からない)。この場合は先頭20件のみ返しつつtruncated=Trueを立てる。
    pages = [_client_master_page(page_id=f"cli-{i}") for i in range(21)]
    client_master_client = _FakeClientMasterClient(pages=pages)
    data_source = _data_source(client_master_client=client_master_client)

    result = search_clients("サンプル", data_source=data_source)

    assert len(result["clients"]) == 20
    assert result["truncated"] is True


# --- search_contacts -------------------------------------------------------------------------


def test_search_contacts_returns_empty_for_blank_query() -> None:
    data_source = _data_source()

    result = search_contacts("", data_source=data_source)

    assert result == {"contacts": [], "truncated": False}


def test_search_contacts_sends_title_contains_filter_to_notion_api() -> None:
    contact_client = _FakeQueryClient(pages=[_contact_page()])
    data_source = _data_source(contact_client=contact_client)

    result = search_contacts("山田", data_source=data_source)

    assert contact_client.calls == [
        {"page_size": 21, "filter": {"property": "名前", "title": {"contains": "山田"}}}
    ]
    assert result["contacts"] == [
        {
            "notion_page_id": "cnt-1",
            "名前": "山田太郎",
            "部署": None,
            "役職": None,
            "取引先": [],
        }
    ]
    assert result["truncated"] is False


def test_search_contacts_reports_truncated_when_more_than_max_results() -> None:
    # search_clientsの同種テストと同型（obasan-qualityレビューWARN対応、2026-09-24。
    # search_contactsだけこの回帰テストが無く、_MAX_SEARCH_RESULTSでの打ち切りが
    # 検証されていなかった）。
    pages = [_contact_page(page_id=f"cnt-{i}") for i in range(21)]
    contact_client = _FakeQueryClient(pages=pages)
    data_source = _data_source(contact_client=contact_client)

    result = search_contacts("山田", data_source=data_source)

    assert len(result["contacts"]) == 20
    assert result["truncated"] is True


def test_search_contacts_resolves_client_name_for_first_relation() -> None:
    contact_page = _contact_page_with_client(client_ids=["cli-1"])
    contact_client = _FakeQueryClient(pages=[contact_page])
    client_master_client = _FakeClientMasterClient(
        raw_pages={"cli-1": _client_master_page(page_id="cli-1", name="サンプルホテル")}
    )
    data_source = _data_source(
        contact_client=contact_client, client_master_client=client_master_client
    )

    result = search_contacts("山田", data_source=data_source)

    assert result["contacts"] == [
        {
            "notion_page_id": "cnt-1",
            "名前": "山田太郎",
            "部署": None,
            "役職": None,
            "取引先": [
                {
                    "notion_page_id": "cli-1",
                    "取引先名": "サンプルホテル",
                    "取引先名_status": STATUS_取引先名_解決済み,
                }
            ],
        }
    ]


def test_search_contacts_marks_second_relation_as_not_fetched() -> None:
    """2件目以降のrelationは設計上APIを引かない。取得失敗と区別できるよう
    `取引先名_status`は`"not_fetched"`になる（`"failed"`とは別、obasan-qualityレビューWARN対応）。
    """
    contact_page = _contact_page_with_client(client_ids=["cli-1", "cli-2"])
    contact_client = _FakeQueryClient(pages=[contact_page])
    client_master_client = _FakeClientMasterClient(
        raw_pages={"cli-1": _client_master_page(page_id="cli-1", name="サンプルホテル")}
    )
    data_source = _data_source(
        contact_client=contact_client, client_master_client=client_master_client
    )

    result = search_contacts("山田", data_source=data_source)

    assert result["contacts"][0]["取引先"] == [
        {
            "notion_page_id": "cli-1",
            "取引先名": "サンプルホテル",
            "取引先名_status": STATUS_取引先名_解決済み,
        },
        {"notion_page_id": "cli-2", "取引先名": None, "取引先名_status": STATUS_取引先名_未取得},
    ]
    # 2件目のrelationはAPIを引いていない(client_master_clientへの呼び出しはcli-1の1回のみ)。
    assert client_master_client.get_raw_page_calls == ["cli-1"]


def test_search_contacts_leaves_client_name_none_when_client_unset() -> None:
    contact_page = _contact_page_with_client(client_ids=[])
    contact_client = _FakeQueryClient(pages=[contact_page])
    data_source = _data_source(contact_client=contact_client)

    result = search_contacts("山田", data_source=data_source)

    assert result["contacts"] == [
        {
            "notion_page_id": "cnt-1",
            "名前": "山田太郎",
            "部署": None,
            "役職": None,
            "取引先": [],
        }
    ]


def test_search_contacts_leaves_client_name_none_when_client_lookup_fails() -> None:
    contact_page = _contact_page_with_client(client_ids=["missing-cli"])
    contact_client = _FakeQueryClient(pages=[contact_page])
    # raw_pages未登録=get_raw_page()がNotionApiError(404)を投げる
    client_master_client = _FakeClientMasterClient(raw_pages={})
    data_source = _data_source(
        contact_client=contact_client, client_master_client=client_master_client
    )

    result = search_contacts("山田", data_source=data_source)

    assert result["contacts"] == [
        {
            "notion_page_id": "cnt-1",
            "名前": "山田太郎",
            "部署": None,
            "役職": None,
            "取引先": [
                {
                    "notion_page_id": "missing-cli",
                    "取引先名": None,
                    "取引先名_status": STATUS_取引先名_取得失敗,
                }
            ],
        }
    ]


def test_search_contacts_falls_back_to_none_on_request_exception() -> None:
    """`NotionApiError`に正規化される前のHTTPリクエスト自体の失敗（タイムアウト等）でも
    検索全体を落とさずNoneへフォールバックする（obasan-qualityレビューWARN対応、2026-09-24。
    20件中1件のタイムアウトで検索全体が落ちていた）。
    """
    contact_page = _contact_page_with_client(client_ids=["cli-1"])
    contact_client = _FakeQueryClient(pages=[contact_page])
    client_master_client = _RequestExceptionClientMasterClient()
    data_source = _data_source(
        contact_client=contact_client, client_master_client=client_master_client
    )

    result = search_contacts("山田", data_source=data_source)

    assert result["contacts"] == [
        {
            "notion_page_id": "cnt-1",
            "名前": "山田太郎",
            "部署": None,
            "役職": None,
            "取引先": [
                {
                    "notion_page_id": "cli-1",
                    "取引先名": None,
                    "取引先名_status": STATUS_取引先名_取得失敗,
                }
            ],
        }
    ]


def test_search_contacts_resolves_shared_client_id_only_once() -> None:
    """同じ会社の社員が複数ヒットした場合、同じclient_idへの`get_raw_page()`は
    1回のリクエスト内で1回だけ（obasan-qualityレビューWARN対応、2026-09-24。
    N+1が「検索結果内で会社が重複する」ケースで野放しになっていた）。
    """
    pages = [
        _contact_page_with_client(page_id="cnt-1", name="山田太郎", client_ids=["cli-1"]),
        _contact_page_with_client(page_id="cnt-2", name="山田花子", client_ids=["cli-1"]),
    ]
    contact_client = _FakeQueryClient(pages=pages)
    client_master_client = _FakeClientMasterClient(
        raw_pages={"cli-1": _client_master_page(page_id="cli-1", name="サンプルホテル")}
    )
    data_source = _data_source(
        contact_client=contact_client, client_master_client=client_master_client
    )

    result = search_contacts("山田", data_source=data_source)

    assert [c["取引先"][0]["取引先名"] for c in result["contacts"]] == [
        "サンプルホテル",
        "サンプルホテル",
    ]
    assert client_master_client.get_raw_page_calls == ["cli-1"]


def test_search_contacts_caps_distinct_client_name_lookups() -> None:
    """1リクエストで名前解決を試みる異なるclient_idは`_MAX_CLIENT_NAME_LOOKUPS`件まで
    （ChatGPTレビューWARN対応、2026-09-24）。検索20件が20社別々のclient_idを持つ最悪
    ケースで`get_raw_page()`が直列20回になり、Notionのレート制限に対して重すぎるため。
    上限を超えた分は`not_fetched`（`notion_page_id`は返るので360ビューへのボタンは出せる）。
    """
    assert _MAX_CLIENT_NAME_LOOKUPS == 5
    client_count = _MAX_CLIENT_NAME_LOOKUPS + 1
    pages = [
        _contact_page_with_client(
            page_id=f"cnt-{i}", name=f"連絡先{i}", client_ids=[f"cli-{i}"]
        )
        for i in range(client_count)
    ]
    contact_client = _FakeQueryClient(pages=pages)
    client_master_client = _FakeClientMasterClient(
        raw_pages={
            f"cli-{i}": _client_master_page(page_id=f"cli-{i}", name=f"取引先{i}")
            for i in range(client_count)
        }
    )
    data_source = _data_source(
        contact_client=contact_client, client_master_client=client_master_client
    )

    result = search_contacts("連絡先", data_source=data_source)

    assert len(client_master_client.get_raw_page_calls) == _MAX_CLIENT_NAME_LOOKUPS
    statuses = [c["取引先"][0]["取引先名_status"] for c in result["contacts"]]
    assert statuses.count(STATUS_取引先名_解決済み) == _MAX_CLIENT_NAME_LOOKUPS
    assert statuses.count(STATUS_取引先名_未取得) == 1
    # 上限を超えた1件も`notion_page_id`は返る(画面側で360ビューへのボタンを出せる)。
    assert result["contacts"][-1]["取引先"][0]["notion_page_id"] == f"cli-{client_count - 1}"
    assert result["contacts"][-1]["取引先"][0]["取引先名"] is None


def test_search_contacts_resolved_status_with_empty_client_name() -> None:
    """ページ取得自体は成功したが`取引先名`プロパティが未入力のケース。
    `resolved`＋`取引先名: None`のまま返す（状態は増やさない、ChatGPTレビュー対応、
    2026-09-24。UI側で`resolved`かどうかで「取得失敗」と区別する）。
    """
    contact_page = _contact_page_with_client(client_ids=["cli-1"])
    contact_client = _FakeQueryClient(pages=[contact_page])
    empty_name_client_page = {
        "id": "cli-1",
        "properties": {"取引先名": {"type": "title", "title": []}},
    }
    client_master_client = _FakeClientMasterClient(
        raw_pages={"cli-1": empty_name_client_page}
    )
    data_source = _data_source(
        contact_client=contact_client, client_master_client=client_master_client
    )

    result = search_contacts("山田", data_source=data_source)

    assert result["contacts"][0]["取引先"] == [
        {
            "notion_page_id": "cli-1",
            "取引先名": None,
            "取引先名_status": STATUS_取引先名_解決済み,
        }
    ]


# --- get_client_360 ---------------------------------------------------------------------------


def test_get_client_360_returns_none_when_client_not_found() -> None:
    client_master_client = _FakeClientMasterClient(raw_pages={})
    data_source = _data_source(client_master_client=client_master_client)

    assert get_client_360("missing-id", data_source=data_source) is None


def test_get_client_360_reraises_non_404_notion_api_errors() -> None:
    class _FailingClientMasterClient(_FakeClientMasterClient):
        def get_raw_page(self, page_id: str) -> dict[str, Any]:
            raise NotionApiError(500, "internal error")

    data_source = _data_source(client_master_client=_FailingClientMasterClient())

    try:
        get_client_360("cli-1", data_source=data_source)
    except NotionApiError as exc:
        assert exc.status_code == 500
    else:
        raise AssertionError("expected NotionApiError to propagate")


def test_get_client_360_assembles_client_projects_contacts_and_actions() -> None:
    client_page = _client_master_page(page_id="cli-1")
    client_master_client = _FakeClientMasterClient(raw_pages={"cli-1": client_page})
    project_client = _FakeQueryClient(pages=[_project_page()])
    contact_client = _FakeQueryClient(pages=[_contact_page()])
    action_client = _FakeQueryClient(pages=[_action_page()])
    data_source = _data_source(
        client_master_client=client_master_client,
        project_client=project_client,
        contact_client=contact_client,
        action_client=action_client,
    )

    result = get_client_360("cli-1", data_source=data_source)

    assert result is not None
    assert result["client"]["取引先名"] == "サンプルホテル"
    assert [p["notion_page_id"] for p in result["projects"]] == ["proj-1"]
    assert [c["notion_page_id"] for c in result["contacts"]] == ["cnt-1"]
    assert [a["notion_page_id"] for a in result["actions"]] == ["act-1"]
    assert result["actions"][0]["担当営業"] == "田中太郎"


def test_get_client_360_uses_relation_contains_filter_for_projects_and_contacts() -> None:
    client_master_client = _FakeClientMasterClient(raw_pages={"cli-1": _client_master_page()})
    project_client = _FakeQueryClient()
    contact_client = _FakeQueryClient()
    action_client = _FakeQueryClient()
    data_source = _data_source(
        client_master_client=client_master_client,
        project_client=project_client,
        contact_client=contact_client,
        action_client=action_client,
    )

    get_client_360("cli-1", data_source=data_source)

    assert project_client.calls == [
        {
            "page_size": 100,
            "filter": {"property": "取引先マスター", "relation": {"contains": "cli-1"}},
        }
    ]
    assert contact_client.calls == [
        {
            "page_size": 100,
            "filter": {"property": "取引先マスター", "relation": {"contains": "cli-1"}},
        }
    ]


def test_get_client_360_uses_emoji_property_name_for_action_relation_filter() -> None:
    """過去に実データとのプロパティ名不一致（絵文字込みの実プロパティ名）で事故が起きた
    経緯を踏まえ、アクション履歴DBのfilterには`👨‍👩‍👧‍👦 取引先マスター`
    （PROP_取引先マスター_ACTION）が実際に使われることを明示的に確認する。
    """
    client_master_client = _FakeClientMasterClient(raw_pages={"cli-1": _client_master_page()})
    action_client = _FakeQueryClient()
    data_source = _data_source(client_master_client=client_master_client, action_client=action_client)

    get_client_360("cli-1", data_source=data_source)

    assert PROP_取引先マスター_ACTION == "👨‍👩‍👧‍👦 取引先マスター"
    assert action_client.calls == [
        {
            "page_size": 100,
            "filter": {
                "property": "👨‍👩‍👧‍👦 取引先マスター",
                "relation": {"contains": "cli-1"},
            },
        }
    ]


# --- 返信傾向(2026-09-03) --------------------------------------------------------------------


def test_get_client_360_includes_reply_timing_keyed_by_contact_page_id() -> None:
    client_master_client = _FakeClientMasterClient(raw_pages={"cli-1": _client_master_page()})
    contact_client = _FakeQueryClient(pages=[_contact_page()])
    builder = _FakeReplyTimingBuilder({"cnt-1": {"median_lag_label": "57分", "sample_size": 3}})
    data_source = _data_source(
        client_master_client=client_master_client,
        contact_client=contact_client,
        reply_timing_builder=builder,
    )

    result = get_client_360("cli-1", data_source=data_source)

    assert result is not None
    # 連絡先のページIDだけを渡していること(取引先・案件・アクションのIDを混ぜない)。
    assert builder.calls == [["cnt-1"]]
    assert result["reply_timing"]["cnt-1"]["median_lag_label"] == "57分"


def test_get_client_360_keeps_contacts_free_of_computed_values() -> None:
    """`contacts`はNotionの写しのまま。算出値を混ぜるとどれがNotionの値か分からなくなる。"""
    client_master_client = _FakeClientMasterClient(raw_pages={"cli-1": _client_master_page()})
    contact_client = _FakeQueryClient(pages=[_contact_page()])
    builder = _FakeReplyTimingBuilder({"cnt-1": {"median_lag_label": "57分"}})
    data_source = _data_source(
        client_master_client=client_master_client,
        contact_client=contact_client,
        reply_timing_builder=builder,
    )

    result = get_client_360("cli-1", data_source=data_source)

    assert result is not None
    assert "median_lag_label" not in result["contacts"][0]


def test_get_client_360_returns_empty_reply_timing_when_no_contacts() -> None:
    client_master_client = _FakeClientMasterClient(raw_pages={"cli-1": _client_master_page()})
    builder = _FakeReplyTimingBuilder()
    data_source = _data_source(client_master_client=client_master_client, reply_timing_builder=builder)

    result = get_client_360("cli-1", data_source=data_source)

    assert result is not None
    assert result["reply_timing"] == {}
    assert builder.calls == [[]]


# --- 一斉配信向けの連絡先取得（2026-09-03） -------------------------------------------


def test_fetch_client_contacts_は取引先名と連絡先だけを返す() -> None:
    source = _data_source(
        client_master_client=_FakeClientMasterClient(raw_pages={"cli-1": _client_master_page()}),
        contact_client=_FakeQueryClient([_contact_page()]),
    )

    result = source.fetch_client_contacts("cli-1")

    assert result is not None
    assert result["client_name"] == "サンプルホテル"
    assert [c["notion_page_id"] for c in result["contacts"]] == ["cnt-1"]
    assert result["truncated"] is False


def test_fetch_client_contacts_は案件とアクションを取りに行かない() -> None:
    """宛先を作るのに要らないものまでNotionへ取りに行かないこと（取引先10社で呼び出しが4倍になる）。"""
    project_client = _FakeQueryClient([_project_page()])
    action_client = _FakeQueryClient([_action_page()])
    source = _data_source(
        client_master_client=_FakeClientMasterClient(raw_pages={"cli-1": _client_master_page()}),
        contact_client=_FakeQueryClient([_contact_page()]),
        project_client=project_client,
        action_client=action_client,
    )

    source.fetch_client_contacts("cli-1")

    assert project_client.calls == []
    assert action_client.calls == []


def test_fetch_client_contacts_は連絡先が上限まで返ったら打ち切りを申告する() -> None:
    """一斉配信では打ち切りが「送ったつもりで送っていない相手がいる」形で表に出る。"""
    many = [_contact_page(f"cnt-{i}") for i in range(100)]
    source = _data_source(
        client_master_client=_FakeClientMasterClient(raw_pages={"cli-1": _client_master_page()}),
        contact_client=_FakeQueryClient(many),
    )

    result = source.fetch_client_contacts("cli-1")

    assert result is not None
    assert result["truncated"] is True


def test_fetch_client_contacts_は存在しない取引先でNoneを返す() -> None:
    source = _data_source(client_master_client=_FakeClientMasterClient(raw_pages={}))
    assert source.fetch_client_contacts("ない") is None

