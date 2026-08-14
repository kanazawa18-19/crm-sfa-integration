#!/usr/bin/env python3
"""サービス・商品DBで分裂していた「ILCA（三密代官、HOTEL DX）」を1ページへ統合する
（2026-08-14、金沢さん指示による一度きりの復旧用）。

■ 経緯 -------------------------------------------------------------------------------------
`fix_proposed_services_corruption.py`で発覚した「提案サービス」multi_select汚染の調査中、
既存の正規オプション「ILCA（三密代官、HOTEL DX）」に対応するサービス・商品ページが実は
3つに分裂していたことが判明した:
  - "ILCA（三密代官"  (3b8d8ea8-d4f3-81a9-b7da-df03f0ab7daf、案件管理から参照あり)
  - "HOTEL DX）"       (3b8d8ea8-d4f3-8185-98c6-c7e58b2f98b8、案件管理から参照あり)
  - "三密代官"         (3b8d8ea8-d4f3-81b2-8ac4-dcd8c9fc1a89、案件管理から参照なし・孤児)

金沢さん指示: 「ILCA（三密代官」「HOTEL DX）」「三密代官」は「ILCA（三密代官、HOTEL DX）」
にまとめる。

■ このスクリプトが行うこと -----------------------------------------------------------------
1. "ILCA（三密代官"ページの名前を正式名称「ILCA（三密代官、HOTEL DX）」へ変更
   （3ページのうち唯一案件管理から広く参照されているため、残す側として選ぶ）
2. "HOTEL DX）"を参照している案件管理レコードを洗い出し、そのサービス・商品リレーションから
   "HOTEL DX）"を外して代わりに"ILCA（三密代官"（名前変更後）を追加、「提案サービス」に
   「ILCA（三密代官、HOTEL DX）」を追加する
3. "HOTEL DX）"・"三密代官"の2ページをアーカイブする（削除はしない、Notion側でゴミ箱から
   復元可能）

--dry-runがデフォルト。--executeで実際に書き込む。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db_schema.registry import get_schema
from src.sync_engine.clients.notion_client import HttpNotionClient

_KEEP_ID = "3b8d8ea8-d4f3-81a9-b7da-df03f0ab7daf"  # "ILCA（三密代官" -> リネームして残す
_MERGE_ID = "3b8d8ea8-d4f3-8185-98c6-c7e58b2f98b8"  # "HOTEL DX）" -> KEEP_IDへ差し替えて archive
_ORPHAN_ID = "3b8d8ea8-d4f3-81b2-8ac4-dcd8c9fc1a89"  # "三密代官" -> 参照なし、archiveのみ
_CORRECT_NAME = "ILCA（三密代官、HOTEL DX）"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    project_schema = get_schema("project")
    product_schema = get_schema("product")
    project_client = HttpNotionClient("project", project_schema.notion_database_id)
    product_client = HttpNotionClient("product", product_schema.notion_database_id)

    affected_pages = []
    for page in project_client.query_all_pages():
        rel_prop = page["properties"].get("サービス・商品")
        proposed_prop = page["properties"].get("提案サービス")
        rel_ids = [r["id"] for r in rel_prop.get("relation", [])] if rel_prop else []
        if _MERGE_ID not in rel_ids:
            continue
        proposed_names = [o["name"] for o in proposed_prop.get("multi_select", [])] if proposed_prop else []
        new_rel_ids = sorted({rid for rid in rel_ids if rid != _MERGE_ID} | {_KEEP_ID})
        new_names = list(proposed_names)
        if _CORRECT_NAME not in new_names:
            new_names.append(_CORRECT_NAME)
        affected_pages.append(
            {
                "page_id": page["id"],
                "before_relation": rel_ids,
                "after_relation": new_rel_ids,
                "before_names": proposed_names,
                "after_names": new_names,
            }
        )

    print(f"'{_CORRECT_NAME}'へ統合する対象案件レコード: {len(affected_pages)}件")
    for p in affected_pages:
        print(f"  {p['page_id']}: {p['before_relation']} -> {p['after_relation']}")

    if not args.execute:
        print("\n--dry-run のため書き込みは行っていません。--execute で実行してください。")
        return

    print(f"\n'{_KEEP_ID}'の名前を '{_CORRECT_NAME}' へ変更します...")
    product_client.update_page(_KEEP_ID, {"名前": _CORRECT_NAME})

    for p in affected_pages:
        project_client.update_page(
            p["page_id"],
            {"提案サービス": p["after_names"], "サービス・商品": p["after_relation"]},
        )

    print(f"'{_MERGE_ID}' ({_MERGE_ID}) をアーカイブします...")
    product_client.archive_page(_MERGE_ID)
    print(f"'{_ORPHAN_ID}' (孤児ページ) をアーカイブします...")
    product_client.archive_page(_ORPHAN_ID)

    print("\n統合完了。")


if __name__ == "__main__":
    main()
