"""バックフィルの結果を実件数で確かめる（読み取りのみ・2026-09-01）。

**「作成=N / 失敗=0」で終わらせない。** スクリプトの自己申告ではなく、
シートの現物を数えて突き合わせる。CLAUDE.md の「rc=0 を信用しない」と同じ考え方。

    python scripts/verify_spreadsheet_backfill.py --db-keys client_master
"""

from __future__ import annotations

import argparse
import os
import json
from pathlib import Path
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `backfill_spreadsheet_all._load_env()` と同じ読み方をする（同じ設定で数えるため）。
from scripts.backfill_spreadsheet_all import _load_env  # noqa: E402

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
    # 診断中に列を作らない。列が曖昧な場合も先頭を勝手に選ばない。
    headers = client._get_header_row(sheet)
    columns_found = [i + 1 for i, name in enumerate(headers) if name == SYNC_KEY_COLUMN]
    if len(columns_found) != 1:
        raise ValueError("同期キー列が無いか複数あります。診断を中止しました（書き込みなし）")
    column = columns_found[0]
    response = client._request(
        "GET",
        f"/values/'{sheet}'!{column_letter(column)}:{column_letter(column)}",
        params={"majorDimension": "COLUMNS", "valueRenderOption": _VALUE_RENDER_OPTION},
    )
    raise_for_error(response, SpreadsheetApiError)
    columns = response.json().get("values") or [[]]
    cells = columns[0] if columns else []
    return [str(c).strip() for c in cells[1:]]  # 1行目はヘッダ


def inspect_mapping_rows(mappings, cells: list[str]) -> dict:
    """同一シート内の行番号の衝突・ずれを、I/Oなしで照合する。

    行番号が未記録でも、同期キーから一意に見つかれば異常ではない。
    修復案は候補だけ。同期中の変更や他ツールのID競合は別途照合が必要。
    """
    rows_by_key: dict[str, list[int]] = {}
    claims: dict[int, set[str]] = {}
    for row, key in enumerate(cells, start=2):
        if key:
            rows_by_key.setdefault(key, []).append(row)
    mismatches = []
    unregistered = 0
    for mapping in mappings:
        actual = rows_by_key.get(mapping.notion_key, [])
        saved = mapping.spreadsheet_row
        if saved is None:
            unregistered += 1
            continue
        claims.setdefault(saved, set()).add(mapping.notion_key)
        if actual != [saved]:
            mismatches.append({
                "notion_key": mapping.notion_key,
                "saved_row": saved,
                "actual_rows": actual,
                "candidate_row": actual[0] if len(actual) == 1 else None,
            })
    return {
        "row_collisions": [
            {"row": row, "notion_keys": sorted(keys)}
            for row, keys in sorted(claims.items()) if len(keys) > 1
        ],
        "row_mismatches": mismatches,
        "unregistered_rows": unregistered,
    }


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
    parser.add_argument(
        "--report-json", type=Path,
        help="全対象キー・行番号・修復候補をローカルJSONへ新規保存（既存ファイルは上書きしない）",
    )
    args = parser.parse_args(argv)
    # 引数検証と --help は認証情報の読み込みより前に完了する。
    for db_key in args.db_keys:
        get_schema(db_key)
    if args.report_json:
        if args.report_json.exists() or args.report_json.is_symlink():
            parser.error("レポート保存先が既に存在します。別のファイル名を指定してください")
        if not args.report_json.parent.is_dir():
            parser.error("レポート保存先のフォルダがありません。既存のフォルダを指定してください")
    os.environ.update(_load_env())

    targets = build_spreadsheet_targets_by_db()
    store = None if args.no_mapping else build_id_mapping_store()

    print(f"{'DB':<14}{'A列末尾位置':>10}{'同期キーあり':>12}{'マッピング':>10}{'重複':>8}{'不足':>8}")
    # 「不足」= 対応表にはあるが、シートに同期キーが見つからない対象数。
    print("-" * 62)
    ng = 0
    reports = []
    mapping_dup_report: list[tuple[str, dict[str, int]]] = []
    gap_report: list[tuple[str, str, list[str], list[str]]] = []
    duplicate_report: list[tuple[str, str, dict[str, list[int]]]] = []
    for db_key in args.db_keys:
        schema = get_schema(db_key)
        target = targets[db_key]
        client = target._client
        sheet = schema.spreadsheet_sheet_name
        rows = max(0, client.count_rows([sheet])[sheet] - 1)  # A列の末尾位置（空行を含む）
        cells = _sync_key_cells(client, sheet)
        keyed = [c for c in cells if c]
        counts = Counter(keyed)
        dup = sum(n - 1 for n in counts.values() if n > 1)
        rows_by_key: dict[str, list[int]] = {}
        if dup:
            # 全列を確認できるよう、重複している行の位置を出す。
            # 行番号は「ヘッダ1行＋0起点」なので +2 でシート上の行番号になる。
            for i, cell in enumerate(cells):
                if cell and counts[cell] > 1:
                    rows_by_key.setdefault(cell, []).append(i + 2)
            duplicate_report.append((db_key, sheet, rows_by_key))
        missing, orphan, mapping_dup = [], [], {}
        row_report = {"row_collisions": [], "row_mismatches": [], "unregistered_rows": None}
        if args.no_mapping:
            mapped = diff = 0
        else:
            all_mappings = store.list_by_db(db_key)
            row_report = inspect_mapping_rows(all_mappings, cells)
            mapping_counts = Counter(m.notion_key for m in all_mappings)
            mapping_keys = set(mapping_counts)
            mapped = len(mapping_keys)
            # **マッピング側の重複も数える。** シートの重複行は、IdMappingが同じ
            # notion_keyを2つ持っていることの写しであることがある（2026-09-02）。
            # シート側だけ消しても、元が重複したままなら次の同期でまた増える。
            mapping_dup = {k: n for k, n in mapping_counts.items() if n > 1}
            if mapping_dup:
                mapping_dup_report.append((db_key, mapping_dup))
            sheet_keys = set(keyed)
            # **引き算では駄目**（2026-09-02）。「シートに無いマッピング」と
            # 「マッピングに無いシート行」が相殺して 0 に見えることがある。
            missing = sorted(mapping_keys - sheet_keys)   # 同期キーが見つからない
            orphan = sorted(sheet_keys - mapping_keys)    # マッピングの無い行
            diff = len(missing)
            if missing or orphan:
                gap_report.append((db_key, sheet, missing, orphan))
        reports.append({
            "db_key": db_key, "sheet": sheet, "mapping_checked": not args.no_mapping,
            "keyed_rows": len(keyed), "missing_keys": missing, "orphan_keys": orphan,
            "mapping_duplicates": mapping_dup,
            "sheet_duplicates": rows_by_key,
            **row_report,
        })
        if (dup or missing or orphan or mapping_dup
                or row_report["row_collisions"] or row_report["row_mismatches"]):
            ng += 1
        cells_m = "－" if args.no_mapping else f"{mapped:,}"
        diff_m = "－" if args.no_mapping else f"{diff:,}"
        print(f"{db_key:<14}{rows:>10,}{len(keyed):>12,}{cells_m:>10}{dup:>8,}{diff_m:>8}")

    for db_key, mapping_dup in mapping_dup_report:
        print(f"\n=== {db_key}: IDマッピング側の重複 {len(mapping_dup)}件 ===")
        print("★ ここが重複していると、シートの行を消しても次の同期でまた増える")
        for key, n in sorted(mapping_dup.items()):
            print(f"  {key}  ×{n}")

    for db_key, sheet, missing, orphan in gap_report:
        print(f"\n=== {db_key}（シート「{sheet}」）の過不足 ===")
        print(f"  同期キーが見つからない対象: {len(missing):,}件")
        for key in missing[:20]:
            print(f"    {key}")
        if len(missing) > 20:
            print(f"    …ほか{len(missing) - 20:,}件")
        print(f"  マッピングの無いシート行: {len(orphan):,}件")
        for key in orphan[:20]:
            print(f"    {key}")
        if len(orphan) > 20:
            print(f"    …ほか{len(orphan) - 20:,}件")

    for db_key, sheet, rows_by_key in duplicate_report:
        print(f"\n=== {db_key}（シート「{sheet}」）の重複 ===")
        print("重複位置の候補です。各行の内容を比較するまで削除対象は確定しません")
        for key, rows_ in sorted(rows_by_key.items(), key=lambda kv: kv[1][0]):
            keep, *drop = rows_
            print(f"  {key}  先頭={keep}行目 / 後続={', '.join(str(r) for r in drop)}行目")

    for report in reports:
        print(f"\n{report['db_key']}: 行番号衝突={len(report['row_collisions'])}組 / "
              f"記録と実位置の不一致={len(report['row_mismatches'])}件 / "
              f"行番号未記録={report['unregistered_rows'] if report['mapping_checked'] else '未照合'}")
        for collision in report["row_collisions"][:20]:
            print(f"  同じ行番号の主張: {collision}")
        for mismatch in report["row_mismatches"][:20]:
            print(f"  行番号の修復候補（未確定）: {mismatch}")
        if len(report["row_collisions"]) > 20 or len(report["row_mismatches"]) > 20:
            print("  表示は各20件まで。全対象は --report-json で保存してください")
    if args.report_json:
        # x モードで既存の証跡を保持する。本文にはレコードIDが含まれる。
        with args.report_json.open("x", encoding="utf-8") as report_file:
            json.dump({"mapping_checked": not args.no_mapping, "reports": reports},
                      report_file, ensure_ascii=False, indent=2)
            report_file.write("\n")
    print()
    if ng:
        print("★ 不整合があります。上の表と修復候補を確認してください（書き込みなし）")
    else:
        print("✅ シートの同期キー重複0。IDマッピングは未照合" if args.no_mapping
              else "✅ 今回の取得範囲では不整合0（列の内容・同時更新・取得漏れは保証外）")
    return 1 if ng else 0


if __name__ == "__main__":
    sys.exit(main())
