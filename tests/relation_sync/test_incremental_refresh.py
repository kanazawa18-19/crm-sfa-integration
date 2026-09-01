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
    state: dict[str, Any] = {
        "cursor": None,
        "upserted": {},
        "swept": None,
        "alerts": [],
        "dms": [],
        # 一巡が終わったときの急減チェックが読む件数。既定は「触れた行＝全体」で健全。
        "total_count": 0,
        "touched_count": None,
    }

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
        # テストが「既にN件入っている」と置いた値は保つ（`max`）。
        state["total_count"] = max(state["total_count"], len(state["upserted"]))

    def _count(*, synced_since: datetime | None = None) -> int:
        if synced_since is None:
            return state["total_count"]
        if state["touched_count"] is not None:
            return state["touched_count"]
        return len(state["upserted"])

    monkeypatch.setattr(sync_module, "upsert_client_names", _upsert)
    monkeypatch.setattr(
        sync_module, "sweep_client_names", lambda *, before: state.update(swept=before) or 0
    )
    monkeypatch.setattr(sync_module, "get_client_name_count", _count)
    monkeypatch.setattr(
        sync_module, "_notify_slack_alert", lambda m, **kw: state["alerts"].append(m)
    )
    monkeypatch.setattr(
        sync_module, "_notify_managers_slack_dm", lambda m, **kw: state["dms"].append(m)
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


def test_releases_the_lock_even_when_notion_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """例外でもロックを解放すること（案件ミラー側と対称に、2026-09-01追加）。

    握ったまま落ちると、翌晩以降ずっと`already_running`でスキップされ続け、
    **一巡が永久に進まないのに正常終了に見える。**
    """
    released: list[Any] = []
    monkeypatch.setattr(sync_module, "try_acquire_refresh_lock", lambda: "conn")
    monkeypatch.setattr(sync_module, "release_refresh_lock", lambda c: released.append(c))
    monkeypatch.setattr(
        sync_module, "load_cursor", lambda name: sync_module.SyncCursor(name, None, NOW)
    )

    class _Broken(_FakeNotion):
        def query_raw(self, body: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("Notionに届かない")

    with pytest.raises(RuntimeError):
        sync_module.refresh_client_names_incrementally(notion_client=_Broken(total=10))

    assert released == ["conn"], "例外でもロックを解放すること"


def test_skips_when_another_run_is_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_module, "try_acquire_refresh_lock", lambda: None)

    result = sync_module.refresh_client_names_incrementally(notion_client=_FakeNotion(10))

    assert result["skipped"] == "already_running"


def test_sweep_is_aborted_when_the_pass_touched_far_too_few_rows(store) -> None:
    """一巡で触れた行が既存の半分を切ったら掃除しない（部分取得の疑い、2026-09-01追加）。

    案件ミラー側（`tests/project_mirror/test_incremental_refresh.py`）にはあった
    ガードが、こちらの分割実行版には無かった。同じ形の事故
    （mark-and-sweepでミラーを全消失させた2026-08-25）を両方で防ぐ。
    """
    notion = _FakeNotion(total=100)
    store["total_count"] = 102_000
    store["touched_count"] = 100

    result = sync_module.refresh_client_names_incrementally(notion_client=notion)

    assert result["skipped"] == "suspected_partial_fetch"
    assert store["swept"] is None, "既存データを消してはいけない"
    assert store["cursor"] is None, "しおりを捨てて次回は先頭からやり直す"
    assert store["alerts"], "通知まで上げること（ログ1行では気づけない）"
    assert store["dms"], (
        "**マネージャーDMまで送ること。** SLACK_WEBHOOK_URL_ALERT は本番未設定で、"
        "webhook側だけでは誰にも届かない（2026-09-01のレビュー指摘）"
    )


def test_sweep_is_aborted_when_the_pass_touched_nothing(store) -> None:
    """1件も触れていない一巡で掃除すると、インデックスが全消失する。"""
    notion = _FakeNotion(total=0)
    store["total_count"] = 10
    store["touched_count"] = 0

    result = sync_module.refresh_client_names_incrementally(notion_client=notion)

    assert result["skipped"] == "suspected_partial_fetch"
    assert store["swept"] is None
