"""Notionの「1クエリ1万件」の壁を越える（2026-09-01）。

**Notionは1万件で打ち切るのに `has_more: false` を返す。**
呼び出し側からは「全部取れた」ように見え、エラーも警告も出ない。
実測でIDマッピングDBのclient_masterが10000で頭打ちになり、
分割して数えると10049件あった。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.sync_engine.clients._notion_paging import query_all_with_keyset


def _page(index: int, created: str) -> dict[str, Any]:
    return {"id": f"page-{index}", "created_time": created}


class _FakeNotion:
    """1クエリ1万件で打ち切り、has_more=False を返す本物の振る舞いを再現する。"""

    CAP = 10_000

    def __init__(self, total: int, per_second: int = 4_000) -> None:
        # 作成日時を分散させる（同じ秒に固まると前へ進めないため）。
        self.pages = [
            _page(i, f"2026-09-01T00:00:{i // per_second:02d}.000Z") for i in range(total)
        ]
        self.queries = 0

    def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        self.queries += 1
        rows = self.pages
        conditions = []
        f = body.get("filter") or {}
        conditions = f.get("and", [f]) if f else []
        for cond in conditions:
            after = (cond.get("created_time") or {}).get("on_or_after")
            if after:
                rows = [p for p in rows if p["created_time"] >= after]
        cursor = int(body.get("start_cursor") or 0)
        window = rows[:self.CAP]  # ここが本物の打ち切り
        chunk = window[cursor : cursor + body["page_size"]]
        nxt = cursor + len(chunk)
        return {
            "results": chunk,
            # **打ち切っても has_more は False。** ここが今回の落とし穴。
            "has_more": nxt < len(window),
            "next_cursor": str(nxt) if nxt < len(window) else None,
        }


def test_everything_is_returned_beyond_the_cap() -> None:
    notion = _FakeNotion(total=25_000)

    pages = query_all_with_keyset(notion, label="test")

    assert len(pages) == 25_000


def test_small_sets_take_a_single_round() -> None:
    notion = _FakeNotion(total=250)

    pages = query_all_with_keyset(notion, label="test")

    assert len(pages) == 250
    assert notion.queries == 3  # 100件ずつ、3回で終わる


def test_boundary_records_are_not_duplicated() -> None:
    """境界は`on_or_after`で重ねて取るので、ページIDで重複を除く必要がある。"""
    notion = _FakeNotion(total=12_000)

    pages = query_all_with_keyset(notion, label="test")

    assert len(pages) == 12_000
    assert len({p["id"] for p in pages}) == 12_000


def test_stops_loudly_when_it_cannot_advance(caplog: pytest.LogCaptureFixture) -> None:
    """同じ作成日時が上限ぶん並ぶと前へ進めない。**黙って欠けさせない。**"""
    notion = _FakeNotion(total=15_000, per_second=15_000)  # 全部同じ秒

    with caplog.at_level("ERROR"):
        pages = query_all_with_keyset(notion, label="test")

    assert len(pages) == 10_000
    assert any("取りこぼしています" in r.getMessage() for r in caplog.records)


def test_base_filter_is_preserved_across_rounds() -> None:
    """絞り込み条件が2周目以降も付いたままであること。"""
    notion = _FakeNotion(total=12_000)
    seen_filters: list[Any] = []

    def _spy(body: dict[str, Any]) -> dict[str, Any]:
        seen_filters.append(body.get("filter"))
        return notion(body)

    query_all_with_keyset(
        _spy, base_filter={"property": "db_key", "select": {"equals": "project"}}, label="test"
    )

    def _has_db_key(f: Any) -> bool:
        conds = f.get("and", [f]) if f else []
        return any(c.get("property") == "db_key" for c in conds)

    assert all(_has_db_key(f) for f in seen_filters)
