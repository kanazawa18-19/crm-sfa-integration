"""重複した同期キーを、シートとIDマッピングの**両方**から消す（2026-09-02）。

バックフィルが同一バッチ内で同じ `notion_key` を2回処理し、どちらも「行が無い」と
判定して2行作ってしまった分の後始末（原因側は `57f6053` で修正済み。ここは残骸の掃除）。

**片方だけ消しても足りない。**

```
   シートの行だけ消す      → IdMapping に同じキーのページが2枚残る
                            次にバックフィルを流すと、また2行できる
   IdMapping だけ消す      → シートに重複行が残り、集計が二重になる
```

**既定は dry-run。** 何を消すかを全部出してから、`--apply` で初めて手を入れる
（CLAUDE.md「削除・上書きの前に、対象を必ず見る」）。

    # 消す対象を見るだけ
    python scripts/dedupe_spreadsheet_duplicates.py --db-key client_master

    # 実際に消す
    python scripts/dedupe_spreadsheet_duplicates.py --db-key client_master --apply

**行番号のずれについて。** 行を消すと下の行番号が1つずつ繰り上がるので、
シートの削除は必ず**降順（下から）**に行う。IdMapping が持つ `spreadsheet_row` も
ずれるが、同期は「まずシートの同期キーで引く」（`dispatcher.py` の
`_sync_to_spreadsheet`）ので、ずれていても正しい行に書き直され自己修復する。
それでも読み直しを減らすため、残したページの行番号は削除後に実測して入れ直す。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from collections import Counter
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backfill_spreadsheet_all import _load_env  # noqa: E402

os.environ.update(_load_env())

from src.db_schema.registry import get_schema  # noqa: E402
from src.sync_engine.clients.spreadsheet_client import (  # noqa: E402
    SpreadsheetApiError,
    _VALUE_RENDER_OPTION,
    column_letter,
    raise_for_error,
)
from src.sync_engine.notion_id_mapping import (  # noqa: E402
    NotionIdMappingStore,
    NotionIdMappingStoreApiError,
    _title_equals_filter,
)
from src.sync_engine.sync_targets.spreadsheet_sync import SYNC_KEY_COLUMN  # noqa: E402
from src.sync_engine.production_wiring import (  # noqa: E402
    build_id_mapping_store,
    build_spreadsheet_targets_by_db,
)


def _sync_key_cells(client, sheet: str) -> list[str]:
    """同期キー列を上から順に返す（ヘッダを除く。i番目 = シートの i+2 行目）。"""
    column = client.ensure_sync_key_column(sheet, SYNC_KEY_COLUMN)
    response = client._request(
        "GET",
        f"/values/'{sheet}'!{column_letter(column)}:{column_letter(column)}",
        params={"majorDimension": "COLUMNS", "valueRenderOption": _VALUE_RENDER_OPTION},
    )
    raise_for_error(response, SpreadsheetApiError)
    columns = response.json().get("values") or [[]]
    cells = columns[0] if columns else []
    return [str(c).strip() for c in cells[1:]]


def _duplicate_rows(cells: list[str]) -> dict[str, list[int]]:
    counts = Counter(c for c in cells if c)
    rows_by_key: dict[str, list[int]] = {}
    for i, cell in enumerate(cells):
        if cell and counts[cell] > 1:
            rows_by_key.setdefault(cell, []).append(i + 2)
    return rows_by_key


def _row_diff(a: dict[str, Any] | None, b: dict[str, Any] | None) -> list[str]:
    """2行の中身を比べ、値が違う列名を返す。中身が同じなら空リスト。"""
    a = a or {}
    b = b or {}
    return sorted(
        name
        for name in set(a) | set(b)
        if str(a.get(name) or "").strip() != str(b.get(name) or "").strip()
    )


def _page_last_synced(page: dict[str, Any]) -> str:
    date = ((page.get("properties") or {}).get("last_synced_at") or {}).get("date") or {}
    return date.get("start") or ""


def _mapping_pages(store, key: str) -> list[dict[str, Any]]:
    """同じ `notion_key` を持つIDマッピングのページを、**残すべき順**に返す（先頭が残す）。

    ★ 「作成が古い方を残す」ではない（2026-09-02にdry-runで判明）。重複した2ページは
    作成時刻が同じ分に並び、片方だけが `spreadsheet_row` を持っている。これは
    `_query_first()` が返した方だけが更新され続けてきたということなので、
    **実際に使われてきた方を残す**。中身の新しい方を捨てると、同期が一段古い状態へ戻る。

        1. last_synced_at が新しい
        2. spreadsheet_row を持っている
        3. 作成が古い
    """
    pages = store._query_all(_title_equals_filter(key))
    return sorted(
        pages,
        key=lambda p: (
            _page_last_synced(p),
            ((p.get("properties") or {}).get("spreadsheet_row") or {}).get("number") is not None,
            # 作成は「古い方が先」にしたいので、降順ソートの中では反転させる
            [-ord(c) for c in (p.get("created_time") or "")],
        ),
        reverse=True,
    )


def _page_summary(page: dict[str, Any]) -> str:
    props = page.get("properties") or {}

    def _text(name: str) -> str:
        blocks = (props.get(name) or {}).get("rich_text") or []
        return "".join(b.get("plain_text", "") for b in blocks) or "－"

    row = (props.get("spreadsheet_row") or {}).get("number")
    synced = _page_last_synced(page)[:19] or "－"
    return (
        f"page={page['id'][:8]}… 作成={(page.get('created_time') or '')[:19]} "
        f"同期={synced} 行={row if row is not None else '－'} "
        f"kintone={_text('kintone_id')} zoho={_text('zoho_id')}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-key", required=True)
    parser.add_argument(
        "--apply", action="store_true", help="実際に消す（既定は表示するだけ）"
    )
    parser.add_argument(
        "--backup-dir",
        default=os.environ.get("DEDUPE_BACKUP_DIR", "."),
        help="--apply の前に、消す行とページの中身をJSONで書き出す先",
    )
    args = parser.parse_args(argv)

    db_key = args.db_key
    schema = get_schema(db_key)
    sheet = schema.spreadsheet_sheet_name
    target = build_spreadsheet_targets_by_db()[db_key]
    client = target._client
    store = build_id_mapping_store()
    # 本番のIDマッピングはNotion裏付け。ページID単位でアーカイブするため、
    # SQLite実装が返ってきたら（＝環境変数の取り違え）ここで止める。
    if not isinstance(store, NotionIdMappingStore):
        print(
            "🔴 IDマッピングがNotion実装ではない"
            f"（{type(store).__name__}）。SYNC_ID_MAPPING_BACKEND を確認すること"
        )
        return 1

    cells = _sync_key_cells(client, sheet)
    rows_by_key = _duplicate_rows(cells)
    if not rows_by_key:
        print(f"✅ {db_key}（シート「{sheet}」）に重複はありません")
        return 0

    print(f"=== {db_key}（シート「{sheet}」）重複 {len(rows_by_key)}組 ===\n")

    drop_rows: list[int] = []
    keep_by_key: dict[str, int] = {}
    archive_pages: list[tuple[str, str]] = []  # (notion_key, page_id)
    backup: list[dict[str, Any]] = []
    keep_page_by_key: dict[str, dict[str, Any]] = {}
    unsafe = 0

    for key, rows in sorted(rows_by_key.items(), key=lambda kv: kv[1][0]):
        keep, *drop = rows
        keep_by_key[key] = keep
        drop_rows.extend(drop)
        print(f"● {key}")
        print(f"   シート: 残す={keep}行目 / 消す={', '.join(str(r) for r in drop)}行目")

        # **中身が同じかを必ず見る。** 違うなら「重複」ではなく別レコードの可能性がある。
        keep_row = client.get_row(sheet, keep)
        for r in drop:
            drop_row = client.get_row(sheet, r)
            backup.append({"notion_key": key, "sheet_row": r, "values": drop_row})
            diff = _row_diff(keep_row, drop_row)
            if diff:
                unsafe += 1
                print(f"   ⚠️ {r}行目は残す行と中身が違う: {', '.join(diff[:8])}")
            else:
                print(f"   {r}行目は残す行と中身が完全に一致（消して安全）")

        pages = _mapping_pages(store, key)
        print(f"   IDマッピング: {len(pages)}ページ")
        for i, page in enumerate(pages):
            mark = "残す" if i == 0 else "消す"
            print(f"     [{mark}] {_page_summary(page)}")
        if pages:
            keep_page_by_key[key] = pages[0]
            archive_pages.extend((key, p["id"]) for p in pages[1:])
            backup.append({"notion_key": key, "archived_pages": pages[1:]})
        print()

    drop_rows.sort(reverse=True)
    print("--- まとめ ---")
    print(f"シートから消す行（降順）: {', '.join(str(r) for r in drop_rows)}")
    print(f"IDマッピングでアーカイブするページ: {len(archive_pages)}件")
    if unsafe:
        print(f"\n🔴 中身が一致しない行が{unsafe}件ある。--apply の前に人が見ること")
        if args.apply:
            print("   中身の違う行があるため --apply を中止した")
            return 1

    if not args.apply:
        print("\n（dry-run。実行するには --apply を付ける）")
        return 0

    # 0. 消す前に中身を書き出す。**やり直しが効く形で始める**（CLAUDE.md §3）。
    #    シートの行はAPIでは復元できないので、ここが唯一の控え。
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(args.backup_dir, f"dedupe-{db_key}-{stamp}.json")
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=2)
    print(f"✅ 消す前の中身を書き出した: {backup_path}")

    # 1. シートの行を降順で消す。**上から消すと下の行番号がずれる。**
    sheet_id = client._grid_properties(sheet).get("sheetId")
    requests_body = [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": r - 1,  # 0起点
                    "endIndex": r,
                }
            }
        }
        for r in drop_rows
    ]
    response = client._request(
        "POST", ":batchUpdate", json_body={"requests": requests_body}, idempotent=False
    )
    raise_for_error(response, SpreadsheetApiError)
    print(f"✅ シートから{len(drop_rows)}行を削除した")

    # 2. IDマッピングの余分なページをアーカイブする。
    for key, page_id in archive_pages:
        r = store._request("PATCH", f"/pages/{page_id}", json_body={"archived": True})
        raise_for_error(r, NotionIdMappingStoreApiError)
    print(f"✅ IDマッピングの{len(archive_pages)}ページをアーカイブした")

    # 3. 残したページの行番号を、削除後の**実測値**で入れ直す（計算で出さない）。
    client._sync_key_rows.pop(sheet, None)
    client._trusted_sync_key_sheets.discard(sheet)
    after = _sync_key_cells(client, sheet)
    actual_row = {cell: i + 2 for i, cell in enumerate(after) if cell}
    fixed = 0
    for key, page in keep_page_by_key.items():
        row = actual_row.get(key)
        if row is None:
            print(f"⚠️ {key} が削除後のシートに見つからない（要確認）")
            continue
        current = ((page.get("properties") or {}).get("spreadsheet_row") or {}).get("number")
        if current == row:
            continue
        r = store._request(
            "PATCH",
            f"/pages/{page['id']}",
            json_body={"properties": {"spreadsheet_row": {"number": row}}},
        )
        raise_for_error(r, NotionIdMappingStoreApiError)
        fixed += 1
    print(f"✅ 残したページの行番号を{fixed}件入れ直した")

    remaining = _duplicate_rows(after)
    if remaining:
        print(f"\n🔴 まだ重複が{len(remaining)}組ある: {', '.join(sorted(remaining))}")
        return 1
    print("\n✅ シートの重複は0になった")
    return 0


if __name__ == "__main__":
    sys.exit(main())
