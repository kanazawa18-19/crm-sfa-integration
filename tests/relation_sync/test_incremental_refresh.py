"""取引先名インデックスの分割実行（2026-09-01）。

取引先マスターDBは **102,799件** あり、全件取得だけで約18分かかる。
Vercelの実行上限は300秒なので1回では終わらない。
**「1万件で静かに切れる」を直したら、今度は「時間切れで何もしない」になる。**
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.relation_sync import sync as sync_module

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


class _FakeNotion:
    """1クエリ1万件で打ち切る本物の振る舞い（`_notion_paging`のテストと同じ）。"""

    CAP = 10_000

    def __init__(self, total: int) -> None:
        self.pages = [
            {
                "id": f"page-{i}",
                "created_time": f"2026-09-01T00:00:{i // 500:02d}.000Z",
                "properties": {
                    "取引先名": {"type": "title", "title": [{"plain_text": f"会社{i}"}]}
                },
            }
            for i in range(total)
        ]

    def query_raw(self, body: dict[str, Any]) -> dict[str, Any]:
        rows = self.pages
        f = body.get("filter") or {}
        for cond in f.get("and", [f]) if f else []:
            after = (cond.get("created_time") or {}).get("on_or_after")
            if after:
                rows = [p for p in rows if p["created_time"] >= after]
        cursor = int(body.get("start_cursor") or 0)
        window = rows[: self.CAP]
        chunk = window[cursor : cursor + body["page_size"]]
        nxt = cursor + len(chunk)
        return {
            "results": chunk,
            "has_more": nxt < len(window),
            "next_cursor": str(nxt) if nxt < len(window) else None,
        }

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def query_all_pages(self) -> list[dict[str, Any]]:
        raise NotImplementedError


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """しおりとDB書き込みをメモリ上に置き換える。"""
    state: dict[str, Any] = {"cursor": None, "upserted": {}, "swept": None}

    monkeypatch.setattr(sync_module, "try_acquire_refresh_lock", lambda: object())
    monkeypatch.setattr(sync_module, "release_refresh_lock", lambda conn: None)
    monkeypatch.setattr(
        sync_module,
        "load_cursor",
        lambda name: state["cursor"]
        or sync_module.SyncCursor(name=name, watermark=None, pass_started_at=NOW),
    )
    monkeypatch.setattr(sync_module, "save_cursor", lambda c: state.update(cursor=c))
    monkeypatch.setattr(sync_module, "clear_cursor", lambda name: state.update(cursor=None))

    def _upsert(rows: list[dict[str, Any]], *, synced_at: datetime) -> None:
        for r in rows:
            state["upserted"][r["notion_page_id"]] = r

    monkeypatch.setattr(sync_module, "upsert_client_names", _upsert)
    monkeypatch.setattr(
        sync_module, "sweep_client_names", lambda *, before: state.update(swept=before) or 0
    )
    return state


def test_one_run_stops_within_the_budget_and_leaves_a_bookmark(store) -> None:
    notion = _FakeNotion(total=25_000)

    result = sync_module.refresh_client_names_incrementally(
        notion_client=notion, time_budget_seconds=0
    )

    assert result["completed"] is False
    assert store["cursor"] is not None, "しおりが残っていない"
    assert store["swept"] is None, "**一巡の途中で掃除してはいけない**"
    assert 0 < len(store["upserted"]) < 25_000


def test_repeated_runs_eventually_cover_everything(store) -> None:
    notion = _FakeNotion(total=25_000)

    for _ in range(50):
        result = sync_module.refresh_client_names_incrementally(
            notion_client=notion, time_budget_seconds=0
        )
        if result.get("completed"):
            break

    assert len(store["upserted"]) == 25_000
    assert store["swept"] == NOW, "一巡し終えたら掃除する"
    assert store["cursor"] is None, "一巡し終えたらしおりを捨てる"


def test_sweep_uses_the_time_the_pass_started(store) -> None:
    """掃除の基準は「一巡を始めた時刻」。**途中の時刻を使うと取り込み済みの行まで消える。**"""
    notion = _FakeNotion(total=500)

    sync_module.refresh_client_names_incrementally(notion_client=notion)

    assert store["swept"] == NOW


def test_skips_when_another_run_is_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_module, "try_acquire_refresh_lock", lambda: None)

    result = sync_module.refresh_client_names_incrementally(notion_client=_FakeNotion(10))

    assert result["skipped"] == "already_running"
