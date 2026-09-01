"""Notionの「1クエリ1万件」の壁を越える（2026-09-01）。

**Notionは1万件で打ち切るのに `has_more: false` を返す。**
呼び出し側からは「全部取れた」ように見え、エラーも警告も出ない。
実測でIDマッピングDBのclient_masterが10000で頭打ちになり、
分割して数えると10049件あった。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.sync_engine.clients._notion_paging import (
    KeysetStalledError,
    query_all_with_keyset,
    query_keyset_slice,
)


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
    """同じ作成日時が上限ぶん並ぶと前へ進めない。**黙って欠けさせない。**

    2026-09-01に「ログを出すだけ」から「例外を送出する」へ変えた（Geminiレビュー指摘）。
    部分的な結果を「全件」として返すと、呼び出し元がそのまま掃除（mark-and-sweep）に
    進み、まだ見ていない行が全部消える。9割欠けたリストを「全件」として下流へ流すのは、
    そもそも今回直した問題そのもの。
    """
    notion = _FakeNotion(total=15_000, per_second=15_000)  # 全部同じ秒

    with caplog.at_level("ERROR"):
        with pytest.raises(KeysetStalledError):
            query_all_with_keyset(notion, label="test")

    assert any("取りこぼしています" in r.getMessage() for r in caplog.records)


def test_a_stalled_slice_is_never_reported_as_completed() -> None:
    """**停滞は completed=True にしない。** ここを間違えると掃除に進んで行が消える。"""
    notion = _FakeNotion(total=15_000, per_second=15_000)

    slice_ = query_keyset_slice(notion, round_limit=10_000, label="test")

    assert slice_.stalled is True
    assert slice_.completed is False, "取りこぼしているのに「取り切った」と伝えている"


def test_a_normal_finish_is_not_marked_stalled() -> None:
    notion = _FakeNotion(total=1_500)

    slice_ = query_keyset_slice(notion, label="test")

    assert slice_.completed is True
    assert slice_.stalled is False
    assert len(slice_.pages) == 1_500


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


# --- 時間予算で中断して再開する（2026-09-01） ---------------------------------------------
#
# 全件取得は約18分かかる。Vercelの実行上限は300秒なので1回では終わらない。
# **「1万件で静かに切れる」を直したら、今度は「時間切れで何もしない」になる。**



def test_round_limit_alone_does_not_stop_the_fetch() -> None:
    """`round_limit`は中断の指示ではなく、区切りの粒度。予算が無ければ最後まで取る。"""
    notion = _FakeNotion(total=5_000, per_second=500)

    got = query_keyset_slice(notion, round_limit=1_000, label="test")

    assert got.completed is True
    assert len(got.pages) == 5_000


def test_resuming_from_the_watermark_covers_everything() -> None:
    """中断と再開を繰り返して、最終的に全件そろうこと。"""
    notion = _FakeNotion(total=5_000, per_second=500)
    seen: dict[str, Any] = {}
    watermark = None

    for _ in range(20):
        got = query_keyset_slice(
            notion,
            watermark=watermark,
            round_limit=1_000,
            time_budget_seconds=0,
            label="test",
        )
        for p in got.pages:
            seen[p["id"]] = p
        watermark = got.watermark
        if got.completed:
            break

    assert len(seen) == 5_000


def test_the_budget_also_applies_inside_a_round() -> None:
    """**周の途中でも時間予算で止まること**（2026-09-01、Gemini Proのレビュー指摘）。

    予算の判定が周の区切りにしか無かったため、1周が長引くと予算を大きく超えて
    Vercelの300秒に突っ込む。そうなるとしおりを保存できず、
    **翌晩も同じ区間をやり直して一巡が進まない。**
    """
    notion = _FakeNotion(total=5_000, per_second=500)

    got = query_keyset_slice(
        notion, round_limit=1_000, time_budget_seconds=0, label="test"
    )

    assert got.completed is False
    assert 0 < len(got.pages) < 1_000, "周を取り切るまで止まらない作りに戻っている"
    assert got.watermark is not None, "しおりを置ける位置で止まること"


def test_the_budget_never_returns_empty_handed() -> None:
    """1件も進んでいないうちは止まらない（しおりを置く場所が無いので前進しない）。"""
    notion = _FakeNotion(total=5_000, per_second=5_000)  # 全部同じ秒

    got = query_keyset_slice(
        notion, round_limit=1_000, time_budget_seconds=0, label="test"
    )

    assert len(got.pages) > 0


# --- Notion が明示する「打ち切った」シグナル（2026-09-01、ChatGPTのレビュー指摘） -----------


def _resp(results, has_more, *, truncated=False):
    body = {"results": results, "has_more": has_more, "next_cursor": None}
    if truncated:
        body["request_status"] = {
            "type": "incomplete",
            "incomplete_reason": "query_result_limit_reached",
        }
    return body


def test_has_more_false_is_not_trusted_when_notion_says_incomplete() -> None:
    """**has_more:false を額面どおり受け取らない。**

    Notionは1万件で打ち切るとき `has_more: false` と一緒に
    `request_status.type = "incomplete"` を返す。本番で実測済み（2026-09-01）。
    has_more だけを見ていたせいで9割欠けていたのが、そもそもの発端。
    """
    calls: list[dict[str, Any]] = []
    pages = [
        {"id": f"p{i}", "created_time": f"2026-09-01T00:{i // 60:02d}:{i % 60:02d}.000Z"}
        for i in range(120)
    ]

    def _post(body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body)
        after = ((body.get("filter") or {}).get("created_time") or {}).get("on_or_after")
        rest = [p for p in pages if not after or p["created_time"] >= after]
        if len(calls) == 1:
            # 60件返して「もう無い」と言いつつ、打ち切ったことも明示している。
            return _resp(rest[:60], False, truncated=True)
        return _resp(rest, False)

    result = query_keyset_slice(_post, round_limit=60, label="test")

    assert result.completed is True
    assert len(calls) > 1, "打ち切りを申告されたのに続きを取りに行っていない"
    assert len(result.pages) == 120, "後半を取りこぼしている"


def test_a_genuinely_complete_response_stops_after_one_round() -> None:
    """打ち切りの申告が無ければ、余計な追加クエリは投げない。"""
    calls: list[dict[str, Any]] = []

    def _post(body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body)
        return _resp([{"id": "p1", "created_time": "2026-09-01T00:00:00.000Z"}], False)

    result = query_keyset_slice(_post, label="test")

    assert result.completed is True
    assert len(calls) == 1
