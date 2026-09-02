"""scripts/dedupe_spreadsheet_duplicates.py（重複の掃除CLI）の検証。

**このスクリプトは`--apply`でシートの行を削除し、Notionページをアーカイブする。**
取り違えると本番データが消えるので、「何を消すと判定するか」を固定しておく
（クマのレビュー指摘、2026-09-03。それまで無テストのまま複雑化していた）。

実際のGoogle Sheets API・Notion APIへは一切アクセスしない。
"""

from __future__ import annotations

from typing import Any

from scripts import dedupe_spreadsheet_duplicates as script


def _page(
    page_id: str,
    *,
    created: str = "2026-09-02T12:18:00.000Z",
    synced: str | None = None,
    row: int | None = None,
    kintone: str = "",
    zoho: str = "",
) -> dict[str, Any]:
    def _text(value: str) -> dict[str, Any]:
        return {"rich_text": [{"plain_text": value}] if value else []}

    return {
        "id": page_id,
        "created_time": created,
        "properties": {
            "last_synced_at": {"date": {"start": synced} if synced else None},
            "spreadsheet_row": {"number": row},
            "kintone_id": _text(kintone),
            "zoho_id": _text(zoho),
        },
    }


class _FakeStore:
    def __init__(self, pages_by_key: dict[str, list[dict[str, Any]]]) -> None:
        self._pages_by_key = pages_by_key
        self.queried: list[Any] = []

    def _query_all(self, filter_: Any) -> list[dict[str, Any]]:
        self.queried.append(filter_)
        # `_title_equals_filter()`の中身には依存せず、呼ばれた順に返す。
        key = filter_["title"]["equals"] if "title" in filter_ else None
        return self._pages_by_key.get(key, [])


# --- シートの重複検出 -------------------------------------------------------------------


def test_同じ同期キーの行番号をシート上の行番号で返す() -> None:
    """ヘッダを除いた0起点なので、i番目 = シートの i+2 行目。"""
    cells = ["A", "B", "A", "", "C", "B"]

    assert script._duplicate_rows(cells) == {"A": [2, 4], "B": [3, 7]}


def test_空欄は重複に数えない() -> None:
    assert script._duplicate_rows(["", "", "A"]) == {}


# --- 行の中身の比較 ---------------------------------------------------------------------


def test_中身が同じなら差分は空() -> None:
    assert script._row_diff({"取引先名": "A", "備考": ""}, {"取引先名": "A"}) == []


def test_違う列だけを返す() -> None:
    assert script._row_diff({"取引先名": "A", "TEL": "03"}, {"取引先名": "B", "TEL": "03"}) == [
        "取引先名"
    ]


# --- 残すページの選び方 -----------------------------------------------------------------


def test_実際に使われてきた方を残す() -> None:
    """**「作成が古い方を残す」ではない。**片方だけが`spreadsheet_row`を持っていたら、
    それが`_query_first()`に選ばれ続けて更新されてきた方（2026-09-02にdry-runで判明）。"""
    古い = _page("old", created="2026-09-02T12:18:00.000Z", synced="2026-09-02T12:17:00Z")
    行あり = _page(
        "used", created="2026-09-02T12:19:00.000Z", synced="2026-09-02T12:17:00Z", row=100
    )
    store = _FakeStore({"K": [古い, 行あり]})

    残す, *消す = script._mapping_pages(store, "K")

    assert 残す["id"] == "used"
    assert [p["id"] for p in 消す] == ["old"]


def test_同期が新しい方を最優先で残す() -> None:
    新しい = _page("new", created="2026-09-02T12:19:00.000Z", synced="2026-09-02T13:00:00Z")
    行あり = _page(
        "used", created="2026-09-02T12:18:00.000Z", synced="2026-09-02T12:17:00Z", row=100
    )
    store = _FakeStore({"K": [行あり, 新しい]})

    残す, *_ = script._mapping_pages(store, "K")

    assert 残す["id"] == "new"


def test_どちらも行が無ければ作成が古い方を残す() -> None:
    """**行がまだ作られていないレコードの重複**（2026-09-03に足した経路）。
    両方とも`spreadsheet_row`が空で、同期時刻も同じ分に並ぶ。"""
    先 = _page("first", created="2026-09-02T00:20:00.000Z", synced="2026-09-02T00:20:00Z")
    後 = _page("second", created="2026-09-02T00:21:00.000Z", synced="2026-09-02T00:20:00Z")
    store = _FakeStore({"K": [後, 先]})

    残す, *消す = script._mapping_pages(store, "K")

    assert 残す["id"] == "first"
    assert [p["id"] for p in 消す] == ["second"]


# --- 外部IDの読み出し（別レコードの取り違え防止に使う） ---------------------------------


def test_外部IDを読み出す() -> None:
    page = _page("p", kintone="62248")

    assert script._page_text(page, "kintone_id") == "62248"
    assert script._page_text(page, "zoho_id") == ""


def test_プロパティが無くても落ちない() -> None:
    assert script._page_text({"id": "p"}, "kintone_id") == ""


def test_外部IDが食い違うページは別レコードとして見分けられる() -> None:
    """`--apply`はこの食い違いを見つけたら中止する。**重複ではなく別レコード。**"""
    a = _page("a", kintone="62248")
    b = _page("b", kintone="62249")

    外部ID = {(script._page_text(p, "kintone_id"), script._page_text(p, "zoho_id")) for p in (a, b)}

    assert len(外部ID) == 2
