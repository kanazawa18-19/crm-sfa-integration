"""src/relation_sync/sync.py（Notion→ClientNameIndex同期処理）の検証。

`upsert_client_name`/`upsert_client_names_and_sweep`（実際のPostgres書き込み）は
monkeypatchで差し替え、実際のDB接続は発生させない（tests/project_mirror/test_sync.pyと
同じパターン）。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.relation_sync import sync


def _raw_client_master_page(
    *, page_id: str = "client-1", title: str | list[dict[str, Any]] | None = "テスト商事株式会社"
) -> dict[str, Any]:
    title_items = (
        title if isinstance(title, list) else ([{"plain_text": title}] if title else [])
    )
    return {
        "id": page_id,
        "properties": {
            "取引先名": {"type": "title", "title": title_items},
        },
    }


class _FakeNotionClient:
    def __init__(
        self, *, page: dict[str, Any] | None = None, pages: list[dict[str, Any]] | None = None
    ) -> None:
        self._page = page
        self._pages = pages or []
        self.get_raw_page_calls: list[str] = []
        self.query_all_pages_call_count = 0

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        self.get_raw_page_calls.append(page_id)
        assert self._page is not None
        return self._page

    def query_all_pages(self) -> list[dict[str, Any]]:
        self.query_all_pages_call_count += 1
        return self._pages


class _FakeLockConnection:
    """`try_acquire_refresh_lock()`が返す接続オブジェクトのフェイク。"""


@pytest.fixture
def _bypass_refresh_lock(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    lock_conn = _FakeLockConnection()
    released: list[Any] = []
    monkeypatch.setattr(sync, "try_acquire_refresh_lock", lambda: lock_conn)
    monkeypatch.setattr(sync, "release_refresh_lock", lambda conn: released.append(conn))
    monkeypatch.setattr(sync, "get_client_name_count", lambda: 0)
    return released


# --- sync_client_name_to_index --------------------------------------------------------------


def test_sync_client_name_to_index_refetches_full_page_and_upserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upsert_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sync, "upsert_client_name", lambda record: upsert_calls.append(record))
    notion_client = _FakeNotionClient(page=_raw_client_master_page())

    sync.sync_client_name_to_index({"取引先名": "Webhookのproperties(未使用)"}, "client-1", notion_client=notion_client)

    assert notion_client.get_raw_page_calls == ["client-1"]
    assert len(upsert_calls) == 1
    record = upsert_calls[0]
    assert record["notion_page_id"] == "client-1"
    assert record["raw_name"] == "テスト商事株式会社"
    assert record["normalized_name"] == "テスト商事"  # 法人格表記が除去される


def test_sync_client_name_to_index_skips_upsert_when_title_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upsert_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sync, "upsert_client_name", lambda record: upsert_calls.append(record))
    notion_client = _FakeNotionClient(page=_raw_client_master_page(title=None))

    sync.sync_client_name_to_index({}, "client-1", notion_client=notion_client)

    assert upsert_calls == []


# --- refresh_all_client_names ----------------------------------------------------------------


def test_refresh_all_client_names_fetches_all_pages_before_writing(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    sweep_calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        sync, "upsert_client_names_and_sweep", lambda records: sweep_calls.append(records) or 0
    )
    pages = [
        _raw_client_master_page(page_id="client-1", title="テスト商事"),
        _raw_client_master_page(page_id="client-2", title="サンプルホテル"),
    ]
    notion_client = _FakeNotionClient(pages=pages)

    result = sync.refresh_all_client_names(notion_client=notion_client)

    assert notion_client.query_all_pages_call_count == 1
    assert len(sweep_calls) == 1
    assert [r["notion_page_id"] for r in sweep_calls[0]] == ["client-1", "client-2"]
    assert result == {"synced_count": 2, "deleted_count": 0}


def test_refresh_all_client_names_skips_pages_without_title(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    sweep_calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        sync, "upsert_client_names_and_sweep", lambda records: sweep_calls.append(records) or 0
    )
    pages = [
        _raw_client_master_page(page_id="client-1", title="テスト商事"),
        _raw_client_master_page(page_id="client-2", title=None),
    ]
    notion_client = _FakeNotionClient(pages=pages)

    result = sync.refresh_all_client_names(notion_client=notion_client)

    assert [r["notion_page_id"] for r in sweep_calls[0]] == ["client-1"]
    assert result == {"synced_count": 1, "deleted_count": 0}


def test_refresh_all_client_names_returns_deleted_count_from_sweep(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    monkeypatch.setattr(sync, "upsert_client_names_and_sweep", lambda records: 7)
    notion_client = _FakeNotionClient(pages=[_raw_client_master_page(page_id="client-1")])

    result = sync.refresh_all_client_names(notion_client=notion_client)

    assert result == {"synced_count": 1, "deleted_count": 7}


def test_refresh_all_client_names_sweeps_with_empty_list_when_no_pages(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    sweep_calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        sync, "upsert_client_names_and_sweep", lambda records: sweep_calls.append(records) or 0
    )
    notion_client = _FakeNotionClient(pages=[])

    result = sync.refresh_all_client_names(notion_client=notion_client)

    assert sweep_calls == [[]]
    assert result == {"synced_count": 0, "deleted_count": 0}


# --- refresh_all_client_names: 部分取得によるsweep事故の防止 -------------------------------


def test_refresh_all_client_names_skips_sweep_when_new_count_much_smaller_than_existing(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    monkeypatch.setattr(sync, "get_client_name_count", lambda: 100)
    sweep_calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        sync, "upsert_client_names_and_sweep", lambda records: sweep_calls.append(records) or 0
    )
    slack_calls: list[str] = []
    monkeypatch.setattr(sync, "_notify_slack_alert", lambda message: slack_calls.append(message))
    notion_client = _FakeNotionClient(
        pages=[
            _raw_client_master_page(page_id="client-1"),
            _raw_client_master_page(page_id="client-2"),
        ]
    )

    result = sync.refresh_all_client_names(notion_client=notion_client)

    assert sweep_calls == []
    assert result == {"synced_count": 2, "deleted_count": 0, "skipped": "suspected_partial_fetch"}
    assert len(slack_calls) == 1


def test_refresh_all_client_names_proceeds_when_new_count_close_to_existing(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    monkeypatch.setattr(sync, "get_client_name_count", lambda: 2)
    sweep_calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        sync, "upsert_client_names_and_sweep", lambda records: sweep_calls.append(records) or 0
    )
    notion_client = _FakeNotionClient(
        pages=[
            _raw_client_master_page(page_id="client-1"),
            _raw_client_master_page(page_id="client-2"),
        ]
    )

    result = sync.refresh_all_client_names(notion_client=notion_client)

    assert len(sweep_calls) == 1
    assert result == {"synced_count": 2, "deleted_count": 0}


# --- refresh_all_client_names: 多重実行防止ロック -------------------------------------------


def test_refresh_all_client_names_acquires_and_releases_lock_around_the_work(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    monkeypatch.setattr(sync, "upsert_client_names_and_sweep", lambda records: 0)
    notion_client = _FakeNotionClient(pages=[_raw_client_master_page(page_id="client-1")])

    sync.refresh_all_client_names(notion_client=notion_client)

    assert len(_bypass_refresh_lock) == 1


def test_refresh_all_client_names_skips_when_lock_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync, "try_acquire_refresh_lock", lambda: None)
    release_calls: list[Any] = []
    monkeypatch.setattr(sync, "release_refresh_lock", lambda conn: release_calls.append(conn))
    notion_client = _FakeNotionClient(pages=[_raw_client_master_page(page_id="client-1")])

    result = sync.refresh_all_client_names(notion_client=notion_client)

    assert notion_client.query_all_pages_call_count == 0
    assert release_calls == []
    assert result == {"synced_count": 0, "deleted_count": 0, "skipped": "already_running"}


def test_refresh_all_client_names_releases_lock_even_when_notion_fetch_raises(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    class _FailingNotionClient:
        def query_all_pages(self) -> list[dict[str, Any]]:
            raise RuntimeError("notion api unavailable")

    with pytest.raises(RuntimeError):
        sync.refresh_all_client_names(notion_client=_FailingNotionClient())

    assert len(_bypass_refresh_lock) == 1
