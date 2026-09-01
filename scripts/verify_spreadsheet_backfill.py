"""バックフィルの結果を実件数で確かめる（読み取りのみ・2026-09-01）。

**「作成=N / 失敗=0」で終わらせない。** スクリプトの自己申告ではなく、
シートの現物を数えて突き合わせる。CLAUDE.md の「rc=0 を信用しない」と同じ考え方。

    python scripts/verify_spreadsheet_backfill.py --db-keys client_master
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `backfill_spreadsheet_all._load_env()` と同じ読み方をする（同じ設定で数えるため）。
from scripts.backfill_spreadsheet_all import _load_env  # noqa: E402

os.environ.update(_load_env())

from src.db_schema.registry import get_schema  # noqa: E402
from src.sync_engine.clients.spreadsheet_client import (  # noqa: E402
    SpreadsheetApiError,
    _VALUE_RENDER_OPTION,
    column_letter,
    raise_for_error,
)
from src.sync_engine.sync_targets.spreadsheet_sync import SYNC_KEY_COLUMN  # noqa: E402
from src.sync_engine.production_wiring import (  # noqa: E402
    build_id_mapping_store,
    build_spreadsheet_targets_by_db,
)


def _sync_key_cells(client, sheet: str) -> list[str]:
    column = client.ensure_sync_key_column(sheet, SYNC_KEY_COLUMN)
    response = client._request(
        "GET",
        f"/values/'{sheet}'!{column_letter(column)}:{column_letter(column)}",
        params={"majorDimension": "COLUMNS", "valueRenderOption": _VALUE_RENDER_OPTION},
    )
    raise_for_error(response, SpreadsheetApiError)
    columns = response.json().get("values") or [[]]
    cells = columns[0] if columns else []
    return [str(c).strip() for c in cells[1:]]  # 1行目はヘッダ


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-keys", nargs="+", required=True)
    parser.add_argument(
        "--no-mapping",
        action="store_true",
        help=(
            "IDマッピングの件数照合を省く（シート側だけ数える）。"
            "マッピングの取得はNotionから数万件を引くので数分〜20分かかり、"
            "バックフィルと同時に走らせるとレートを取り合う"
        ),
    )
    args = parser.parse_args(argv)

    targets = build_spreadsheet_targets_by_db()
    store = build_id_mapping_store()

    print(f"{'DB':<14}{'シート行数':>10}{'同期キーあり':>12}{'マッピング':>10}{'重複':>8}{'差分':>8}")
    # 「差分」= マッピング件数 − シートの同期キーの種類数。0でなければ取りこぼしがある。
    print("-" * 62)
    ng = 0
    duplicate_report: list[tuple[str, str, dict[str, list[int]]]] = []
    for db_key in args.db_keys:
        schema = get_schema(db_key)
        target = targets[db_key]
        client = target._client
        sheet = schema.spreadsheet_sheet_name
        rows = client.count_rows([sheet])[sheet] - 1  # ヘッダを除く
        cells = _sync_key_cells(client, sheet)
        keyed = [c for c in cells if c]
        counts = Counter(keyed)
        dup = sum(n - 1 for n in counts.values() if n > 1)
        if dup:
            # 何行目を消せばよいかまで出す。**「重複8件」だけでは手が動かせない。**
            # 行番号は「ヘッダ1行＋0起点」なので +2 でシート上の行番号になる。
            rows_by_key: dict[str, list[int]] = {}
            for i, cell in enumerate(cells):
                if cell and counts[cell] > 1:
                    rows_by_key.setdefault(cell, []).append(i + 2)
            duplicate_report.append((db_key, sheet, rows_by_key))
        if args.no_mapping:
            mapped = diff = 0
        else:
            mapped = len(store.list_by_db(db_key))
            diff = mapped - len(set(keyed))
        if dup or (not args.no_mapping and diff):
            ng += 1
        cells_m = "－" if args.no_mapping else f"{mapped:,}"
        diff_m = "－" if args.no_mapping else f"{diff:,}"
        print(f"{db_key:<14}{rows:>10,}{len(keyed):>12,}{cells_m:>10}{dup:>8,}{diff_m:>8}")

    for db_key, sheet, rows_by_key in duplicate_report:
        print(f"\n=== {db_key}（シート「{sheet}」）の重複 ===")
        print("同期キーごとに、**最初の行を残して後ろを消す**（残す行は先頭）")
        for key, rows_ in sorted(rows_by_key.items(), key=lambda kv: kv[1][0]):
            keep, *drop = rows_
            print(f"  {key}  残す={keep}行目 / 消す={', '.join(str(r) for r in drop)}行目")
        drop_all = sorted(r for rows_ in rows_by_key.values() for r in rows_[1:])
        print(f"  消す行を降順で: {', '.join(str(r) for r in reversed(drop_all))}")
        print("  ★ 上から消すと行番号がずれる。**必ず下（大きい番号）から消すこと**")

    print()
    if ng:
        print("★ 重複または差分がある。上の表を確認すること")
    else:
        print("✅ 重複0・差分0")
    return 1 if ng else 0


if __name__ == "__main__":
    sys.exit(main())
