"""src/project_mirror/sync.py（Notion→ProjectMirror同期処理）の検証。

`upsert_project`/`upsert_projects_and_sweep`（実際のPostgres書き込み）はmonkeypatchで
差し替え、実際のDB接続は発生させない。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.project_mirror import sync


class _FakeUserDirectory:
    def resolve(self, user_id: str) -> str:
        return f"resolved:{user_id}"

    def resolve_many(self, user_ids: list[str]) -> list[str]:
        return [self.resolve(uid) for uid in user_ids]


def _raw_project_page(*, page_id: str = "proj-1", last_edited_time: str | None = "2026-08-17T09:00:00.000Z") -> dict[str, Any]:
    return {
        "id": page_id,
        "last_edited_time": last_edited_time,
        "properties": {
            "案件名": {"type": "title", "title": [{"plain_text": "サンプルホテル"}]},
            "担当メンバー": {
                "type": "people",
                "people": [{"object": "user", "id": "user-1", "name": "田中太郎"}],
            },
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
    """`try_acquire_refresh_lock()`が返す接続オブジェクトのフェイク（中身は使わない、
    identityのみ`release_refresh_lock()`へ渡ったかの確認に使う）。"""


@pytest.fixture
def _bypass_refresh_lock(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """`try_acquire_refresh_lock`/`release_refresh_lock`（実際のPostgresアドバイザリロック）を
    差し替え、常にロック取得成功として扱う。戻り値は`release_refresh_lock`に渡された引数の
    記録（解放されたことの確認に使う）。

    `get_project_count`（実際のPostgres接続）も既定で0を返すよう差し替える
    （部分取得検知の安全装置、2026-08-18、`current_count >= 20`未満では発動しない設計の
    ため、既存テストの挙動には影響しない）。安全装置自体の挙動を検証するテストでは
    個別に上書きする。"""
    lock_conn = _FakeLockConnection()
    released: list[Any] = []
    monkeypatch.setattr(sync, "try_acquire_refresh_lock", lambda: lock_conn)
    monkeypatch.setattr(sync, "release_refresh_lock", lambda conn: released.append(conn))
    monkeypatch.setattr(sync, "get_project_count", lambda: 0)
    return released


# --- sync_project_to_mirror ----------------------------------------------------------------


def test_sync_project_to_mirror_refetches_full_page_and_upserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`properties`引数（SyncEvent.properties相当）は実際には使わず、必ず
    `notion_client.get_raw_page()`でページ全体を再取得することを確認する。"""
    upsert_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sync, "upsert_project", lambda record: upsert_calls.append(record))
    notion_client = _FakeNotionClient(page=_raw_project_page())

    sync.sync_project_to_mirror(
        {"案件名": "Webhookのproperties(未使用)"},
        "proj-1",
        notion_client=notion_client,
        user_directory=_FakeUserDirectory(),
    )

    assert notion_client.get_raw_page_calls == ["proj-1"]
    assert len(upsert_calls) == 1
    record = upsert_calls[0]
    assert record["notion_page_id"] == "proj-1"
    assert record["data"]["案件名"] == "サンプルホテル"
    assert record["data"]["担当メンバー"] == ["田中太郎"]
    assert record["last_edited_at"] is not None


def test_sync_project_to_mirror_sets_last_edited_at_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upsert_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sync, "upsert_project", lambda record: upsert_calls.append(record))
    notion_client = _FakeNotionClient(page=_raw_project_page(last_edited_time=None))

    sync.sync_project_to_mirror(
        {}, "proj-1", notion_client=notion_client, user_directory=_FakeUserDirectory()
    )

    assert upsert_calls[0]["last_edited_at"] is None


# --- refresh_all_projects ------------------------------------------------------------------


def test_refresh_all_projects_fetches_all_pages_before_writing(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    sweep_calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        sync, "upsert_projects_and_sweep", lambda records: sweep_calls.append(records) or 0
    )
    pages = [
        _raw_project_page(page_id="proj-1"),
        _raw_project_page(page_id="proj-2"),
    ]
    notion_client = _FakeNotionClient(pages=pages)

    result = sync.refresh_all_projects(
        notion_client=notion_client, user_directory=_FakeUserDirectory()
    )

    assert notion_client.query_all_pages_call_count == 1
    assert len(sweep_calls) == 1
    assert [r["notion_page_id"] for r in sweep_calls[0]] == ["proj-1", "proj-2"]
    assert result == {"synced_count": 2, "deleted_count": 0}


def test_refresh_all_projects_returns_deleted_count_from_sweep(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    monkeypatch.setattr(sync, "upsert_projects_and_sweep", lambda records: 7)
    notion_client = _FakeNotionClient(pages=[_raw_project_page(page_id="proj-1")])

    result = sync.refresh_all_projects(
        notion_client=notion_client, user_directory=_FakeUserDirectory()
    )

    assert result == {"synced_count": 1, "deleted_count": 7}


def test_refresh_all_projects_sweeps_with_empty_list_when_no_pages(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    """全件取得の結果が0件だった場合も、そのまま`upsert_projects_and_sweep([])`を呼ぶ
    （実際に全件削除してよいかどうかの安全判断は`db.upsert_projects_and_sweep`側の責務）。"""
    sweep_calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        sync, "upsert_projects_and_sweep", lambda records: sweep_calls.append(records) or 0
    )
    notion_client = _FakeNotionClient(pages=[])

    result = sync.refresh_all_projects(
        notion_client=notion_client, user_directory=_FakeUserDirectory()
    )

    assert sweep_calls == [[]]
    assert result == {"synced_count": 0, "deleted_count": 0}


# --- refresh_all_projects: 部分取得によるsweep事故の防止（2026-08-18）---------------------


def test_refresh_all_projects_skips_sweep_when_new_count_much_smaller_than_existing(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    """既存ミラー件数(100件)に対し、新規取得件数(2件)が大幅に少ない場合はsweepを
    中止し、既存データを保護すること（実際に発生した「ミラーが1晩で0件になった」
    事故の再発防止）。"""
    monkeypatch.setattr(sync, "get_project_count", lambda: 100)
    sweep_calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        sync, "upsert_projects_and_sweep", lambda records: sweep_calls.append(records) or 0
    )
    slack_calls: list[str] = []
    monkeypatch.setattr(sync, "_notify_slack_alert", lambda message: slack_calls.append(message))
    notion_client = _FakeNotionClient(
        pages=[_raw_project_page(page_id="proj-1"), _raw_project_page(page_id="proj-2")]
    )

    result = sync.refresh_all_projects(
        notion_client=notion_client, user_directory=_FakeUserDirectory()
    )

    assert sweep_calls == []
    assert result == {"synced_count": 2, "deleted_count": 0, "skipped": "suspected_partial_fetch"}
    assert len(slack_calls) == 1


def test_refresh_all_projects_proceeds_when_new_count_close_to_existing(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    """新規取得件数が既存件数に近い(急減していない)場合は通常通りsweepすること。"""
    monkeypatch.setattr(sync, "get_project_count", lambda: 2)
    sweep_calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        sync, "upsert_projects_and_sweep", lambda records: sweep_calls.append(records) or 0
    )
    notion_client = _FakeNotionClient(
        pages=[_raw_project_page(page_id="proj-1"), _raw_project_page(page_id="proj-2")]
    )

    result = sync.refresh_all_projects(
        notion_client=notion_client, user_directory=_FakeUserDirectory()
    )

    assert len(sweep_calls) == 1
    assert result == {"synced_count": 2, "deleted_count": 0}


def test_refresh_all_projects_partial_fetch_check_skipped_when_existing_count_tiny(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    """既存件数が極端に小さい(20件未満)場合は、新規取得0件でも安全装置は発動せず
    通常通りsweepする（少数データの正当なゼロ件化を誤検知しないため）。"""
    monkeypatch.setattr(sync, "get_project_count", lambda: 3)
    sweep_calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        sync, "upsert_projects_and_sweep", lambda records: sweep_calls.append(records) or 0
    )
    notion_client = _FakeNotionClient(pages=[])

    result = sync.refresh_all_projects(
        notion_client=notion_client, user_directory=_FakeUserDirectory()
    )

    assert sweep_calls == [[]]
    assert result == {"synced_count": 0, "deleted_count": 0}


# --- refresh_all_projects: 多重実行防止ロック（shirokuma-secレビューWARN対応、2026-08-17）---


def test_refresh_all_projects_acquires_and_releases_lock_around_the_work(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    monkeypatch.setattr(sync, "upsert_projects_and_sweep", lambda records: 0)
    notion_client = _FakeNotionClient(pages=[_raw_project_page(page_id="proj-1")])

    sync.refresh_all_projects(notion_client=notion_client, user_directory=_FakeUserDirectory())

    assert len(_bypass_refresh_lock) == 1


def test_refresh_all_projects_skips_when_lock_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既に別プロセスが実行中(ロック取得失敗)の場合、Notion API呼び出し・書き込みを一切
    行わず即座にスキップすること。"""
    monkeypatch.setattr(sync, "try_acquire_refresh_lock", lambda: None)
    release_calls: list[Any] = []
    monkeypatch.setattr(sync, "release_refresh_lock", lambda conn: release_calls.append(conn))
    notion_client = _FakeNotionClient(pages=[_raw_project_page(page_id="proj-1")])

    result = sync.refresh_all_projects(
        notion_client=notion_client, user_directory=_FakeUserDirectory()
    )

    assert notion_client.query_all_pages_call_count == 0
    assert release_calls == []
    assert result == {"synced_count": 0, "deleted_count": 0, "skipped": "already_running"}


def test_refresh_all_projects_releases_lock_even_when_notion_fetch_raises(
    monkeypatch: pytest.MonkeyPatch, _bypass_refresh_lock: list[Any]
) -> None:
    class _FailingNotionClient:
        def query_all_pages(self) -> list[dict[str, Any]]:
            raise RuntimeError("notion api unavailable")

    with pytest.raises(RuntimeError):
        sync.refresh_all_projects(
            notion_client=_FailingNotionClient(), user_directory=_FakeUserDirectory()
        )

    assert len(_bypass_refresh_lock) == 1
